from __future__ import annotations

import unittest

from coding_tools_mcp.server import TOOL_REGISTRY
from coding_tools_mcp.upstream import (
    MAX_TAG_CHARS,
    MAX_TAG_ITEMS,
    MAX_TOOL_FILTER_ITEMS,
    MAX_TOOL_POLICY_ITEMS,
    UpstreamConfigError,
    UpstreamServerConfig,
    parse_server_config,
)
from tests.compliance.test_upstream_gateway import FakeUpstreamClient, build_manager


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


if __name__ == "__main__":
    unittest.main()
