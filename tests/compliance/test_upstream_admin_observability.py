from __future__ import annotations

import json
import unittest
from types import MappingProxyType
from unittest.mock import patch

from coding_tools_mcp import upstream as upstream_module
from coding_tools_mcp.server import TOOL_REGISTRY
from coding_tools_mcp.upstream import (
    MAX_TAG_CHARS,
    MAX_TAG_ITEMS,
    MAX_TOOL_FILTER_ITEMS,
    MAX_TOOL_POLICY_ITEMS,
    UpstreamCatalogTemplate,
    UpstreamConfigError,
    UpstreamRegistryState,
    UpstreamServerConfig,
    UpstreamStatusTemplate,
    parse_server_config,
)
from coding_tools_mcp.upstream_resilience import BackoffPolicy, UpstreamResilienceCoordinator
from tests.compliance.test_upstream_gateway import FakeUpstreamClient, build_manager
from tests.test_upstream_resilience import ScriptedHttpClient


class UpstreamAdminObservabilityTests(unittest.TestCase):
    def test_legacy_config_defaults_to_direct(self) -> None:
        config = parse_server_config(
            "legacy",
            {
                "transport": "streamable_http",
                "url": "http://127.0.0.1/mcp",
            },
        )
        self.assertEqual(config.expose_mode, "direct")

    def test_exposure_configuration_limits_fail_closed(self) -> None:
        base = {
            "transport": "streamable_http",
            "url": "http://127.0.0.1/mcp",
        }
        cases = (
            ({**base, "include_tools": [f"tool-{index}" for index in range(MAX_TOOL_FILTER_ITEMS + 1)]}, "include_tools"),
            ({**base, "exclude_tools": [f"tool-{index}" for index in range(MAX_TOOL_FILTER_ITEMS + 1)]}, "exclude_tools"),
            ({**base, "pinned_tools": [f"tool-{index}" for index in range(MAX_TOOL_FILTER_ITEMS + 1)]}, "pinned_tools"),
            ({**base, "tags": [f"tag-{index}" for index in range(MAX_TAG_ITEMS + 1)]}, "tags"),
            ({**base, "tags": ["x" * (MAX_TAG_CHARS + 1)]}, "tags"),
            ({**base, "tool_policy": {f"tool-{index}": "readonly" for index in range(MAX_TOOL_POLICY_ITEMS + 1)}}, "tool_policy"),
        )
        for document, field in cases:
            with self.subTest(field=field), self.assertRaisesRegex(UpstreamConfigError, field):
                parse_server_config("remote", document)

    def test_status_reports_upstream_only_context_and_exposure(self) -> None:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode="broker",
            pinned_tools=("search",),
            tags=("remote",),
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client], reserved_names=set(TOOL_REGISTRY))
        try:
            status = manager.status_payload()
            report = status["exposure_report"]
            self.assertEqual(report["scope"], "upstream_only")
            self.assertTrue(report["excludes_local_and_admin_definitions"])
            self.assertEqual(report["direct"]["count"], 1)
            self.assertGreater(report["direct"]["definition_bytes"], 0)
            self.assertEqual(report["catalog"], {"count": 2, "broker_only_count": 1})
            self.assertEqual(report["servers"][0]["alias"], "remote")
            self.assertEqual(report["servers"][0]["direct_count"], 1)
            self.assertEqual(report["servers"][0]["broker_only_count"], 1)
            self.assertGreater(report["servers"][0]["definition_bytes"], 0)
            self.assertNotIn("tools", report["servers"][0])
            self.assertNotIn("server_info", repr(report))
            largest = report["largest_public_definitions"]
            self.assertTrue(largest)
            self.assertTrue(all(item["name"].startswith("remote__") for item in largest))
        finally:
            manager.close()

    def test_status_uses_shared_aggregate_when_this_runtime_has_no_local_client(self) -> None:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode="broker",
        )
        clock_value = [100.0]
        shared = UpstreamResilienceCoordinator(
            policy=BackoffPolicy(initial_seconds=1.0, maximum_seconds=8.0, jitter_ratio=0.0),
            clock=lambda: clock_value[0],
            random_value=lambda: 0.5,
        )
        live = ScriptedHttpClient(config, shared)
        live.initialize()
        live.call_errors.append(
            upstream_module.UpstreamError(
                "UPSTREAM_HTTP_ERROR",
                "synthetic 502",
                category="upstream",
                retryable=True,
                details={"status": 502},
            )
        )
        with self.assertRaises(upstream_module.UpstreamError):
            live.call_tool_raw("read", {})
        live._set_session_id("session-secret")

        template = UpstreamCatalogTemplate(
            configs=(config,),
            custom_synonyms=MappingProxyType({}),
            registry_state=UpstreamRegistryState(
                all_tools={},
                direct_tool_names=(),
                catalog={},
                search_index=None,
                clients={},
            ),
            statuses=(
                UpstreamStatusTemplate(
                    alias="remote",
                    transport="streamable_http",
                    enabled=True,
                    initialized=False,
                    tool_count=0,
                    error=None,
                    target="http://127.0.0.1/mcp",
                ),
            ),
            reserved_names=frozenset(TOOL_REGISTRY),
        )
        with patch.object(upstream_module, "DEFAULT_UPSTREAM_RESILIENCE_COORDINATOR", shared):
            manager = upstream_module.UpstreamManager.from_template(template)
        try:
            self.assertEqual(manager.live_client_count(), 0)
            status = manager.status_payload()
            server = status["servers"][0]
            self.assertEqual(server["state"], "BACKING_OFF")
            self.assertEqual(server["live_client_count"], 1)
            self.assertEqual(server["active_client_sessions"], 1)
            self.assertEqual(server["consecutive_failures"], 1)
            serialized = json.dumps(status, allow_nan=False, separators=(",", ":"))
            self.assertNotIn("session-secret", serialized)
            self.assertNotIn("Authorization", serialized)
            self.assertNotIn("principal", serialized)
            self.assertNotIn("workspace", serialized)
        finally:
            manager.close()
            with patch(
                "coding_tools_mcp.upstream.urllib.request.urlopen",
                side_effect=upstream_module.urllib.error.HTTPError(
                    "http://127.0.0.1/mcp", 404, "gone", {}, None
                ),
            ):
                live.close()

    def test_http_target_redacts_url_userinfo_from_status_and_resilience_key(self) -> None:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="https://user:SUPER-SECRET@example.test:8443/mcp?token=ignored",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client], reserved_names=set(TOOL_REGISTRY))
        try:
            payload = manager.status_payload()
            target = payload["servers"][0]["target"]
            resilience_key = upstream_module._resilience_key_for_config(config)
            self.assertEqual(target, "https://example.test:8443/mcp")
            self.assertEqual(resilience_key, "remote|https://example.test:8443/mcp")
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn("user", serialized)
            self.assertNotIn("SUPER-SECRET", serialized)
            self.assertNotIn("user", resilience_key)
            self.assertNotIn("SUPER-SECRET", resilience_key)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
