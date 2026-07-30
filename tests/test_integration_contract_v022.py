from __future__ import annotations

import inspect
import json
import re
import sqlite3
import unittest

from coding_tools_mcp.admin import AdminService
from pathlib import Path
from contextlib import closing
from tempfile import TemporaryDirectory

from coding_tools_mcp import codex_sessions as codex_sessions_module
from coding_tools_mcp import transcript as transcript_module
from coding_tools_mcp import upstream as upstream_module
from coding_tools_mcp.oauth import (
    OAUTH_GRANT_TYPES_SUPPORTED,
    OAUTH_RESPONSE_TYPES_SUPPORTED,
    OAuthClientRegistry,
    OAuthIdentity,
)
from coding_tools_mcp.protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from coding_tools_mcp.oauth_store import OAuthAuthorizationStore
from coding_tools_mcp.server import MCPHandler, Runtime, TOOL_REGISTRY, build_parser
from coding_tools_mcp.upstream import UpstreamManager
from coding_tools_mcp.workspace_binding import WorkspaceBindingError, WorkspaceBindingResolver
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry
from tests.test_oauth_store import oauth_root

from coding_tools_mcp.settings_definition import (
    LEGACY_TOOL_PROFILE_WARNING,
    migrate_persisted_settings,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "integration-contract-v0.2.2.md"
CONTRACT_PATTERN = re.compile(
    r"<!-- integration-contract-json:start -->\s*```json\s*(\{.*?\})\s*```\s*"
    r"<!-- integration-contract-json:end -->",
    re.DOTALL,
)


def load_contract() -> dict[str, object]:
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    match = CONTRACT_PATTERN.search(text)
    if match is None:
        raise AssertionError("integration contract JSON block is missing")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise AssertionError("integration contract JSON must be an object")
    return payload


class IntegrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = load_contract()

    def test_protocol_target_matches_upstream_runtime(self) -> None:
        protocol = self.contract["protocol"]
        self.assertIsInstance(protocol, dict)
        self.assertEqual(protocol["target"], PROTOCOL_VERSION)
        self.assertEqual(
            [protocol["target"], *protocol["compatible"]],
            list(SUPPORTED_PROTOCOL_VERSIONS),
        )
        self.assertEqual(self.contract["version"], {"integration": "0.2.2"})

    def test_catalog_is_fixed_and_legacy_profiles_are_migration_only(self) -> None:
        catalog = self.contract["tool_catalog"]
        self.assertIsInstance(catalog, dict)
        self.assertEqual(catalog["strategy"], "fixed")
        self.assertEqual(catalog["source"], "coding_tools_mcp.server.TOOL_REGISTRY")
        self.assertIs(catalog["legacy_tool_profile_controls_catalog"], False)
        self.assertNotIn("--tool-profile", build_parser().format_help())

        with TemporaryDirectory() as tmp:
            truthful = Runtime(Path(tmp), permission_mode="dangerous")
            compatibility = Runtime(
                Path(tmp),
                permission_mode="dangerous",
                fake_readonly_annotations=True,
            )
            try:
                truthful_tools = truthful.list_tools()["tools"]
                compatibility_tools = compatibility.list_tools()["tools"]
            finally:
                truthful.close()
                compatibility.close()

        self.assertEqual(
            {tool["name"] for tool in truthful_tools},
            {tool["name"] for tool in compatibility_tools},
        )
        self.assertEqual({tool["name"] for tool in truthful_tools}, set(TOOL_REGISTRY))
        fake_policy = catalog["fake_readonly"]
        self.assertIs(fake_policy["security_boundary"], False)
        self.assertIs(fake_policy["changes_catalog"], False)
        self.assertIs(fake_policy["changes_handlers"], False)
        for tool in compatibility_tools:
            annotations = tool["annotations"]
            self.assertIs(annotations["readOnlyHint"], True)
            self.assertIs(annotations["destructiveHint"], False)
            self.assertIs(annotations["openWorldHint"], False)

    def test_legacy_tool_profile_migration_inputs_and_outputs_are_explicit(self) -> None:
        migration = self.contract["legacy_tool_profile_migration"]
        self.assertIsInstance(migration, dict)
        self.assertIs(migration["persist_on_next_write"], False)
        self.assertEqual(migration["unknown_value"], "ignore_with_warning")

        cases = migration["cases"]
        self.assertEqual(
            {case["input"]["tool_profile"] for case in cases},
            {"full", "read-only", "compat-readonly-all"},
        )
        for case in cases:
            with self.subTest(tool_profile=case["input"]["tool_profile"]):
                output = case["output"]
                self.assertIsNone(output["tool_profile"])
                self.assertEqual(output["catalog"], "fixed")
                self.assertEqual(output["warning"], migration["warning_code"])

    def test_oauth_advertising_uses_the_upstream_constant_sources(self) -> None:
        oauth = self.contract["oauth"]
        self.assertEqual(oauth["advertised_grant_types"], list(OAUTH_GRANT_TYPES_SUPPORTED))
        self.assertEqual(oauth["advertised_response_types"], list(OAUTH_RESPONSE_TYPES_SUPPORTED))
        self.assertEqual(oauth["grant_types_source"], "coding_tools_mcp.oauth.OAUTH_GRANT_TYPES_SUPPORTED")
        self.assertEqual(oauth["response_types_source"], "coding_tools_mcp.oauth.OAUTH_RESPONSE_TYPES_SUPPORTED")

        registry = OAuthClientRegistry()
        registered = registry.register(
            {
                "redirect_uris": ["http://127.0.0.1/callback"],
                "grant_types": ["refresh_token", "authorization_code"],
                "response_types": ["code", "token"],
            }
        )
        self.assertEqual(registered["grant_types"], list(OAUTH_GRANT_TYPES_SUPPORTED))
        self.assertEqual(registered["response_types"], ["code"])

        metadata_source = inspect.getsource(MCPHandler.handle_oauth_as_metadata)
        token_source = inspect.getsource(MCPHandler.handle_oauth_token)
        self.assertIn("OAUTH_GRANT_TYPES_SUPPORTED", metadata_source)
        self.assertIn("OAUTH_RESPONSE_TYPES_SUPPORTED", metadata_source)
        self.assertIn("OAUTH_GRANT_TYPE_AUTHORIZATION_CODE", token_source)

    def test_later_phase_boundaries_are_machine_readable(self) -> None:
        oauth = self.contract["oauth"]
        workspace = self.contract["workspace_binding"]
        telemetry = self.contract["telemetry"]
        secret_stores = self.contract["secret_stores"]

        self.assertEqual(oauth["persistent_store_phase"], 4)
        self.assertEqual(oauth["http_integration_phase"], 5)
        self.assertEqual(oauth["migration"], "idempotent_transactional")
        self.assertEqual(workspace["phase"], 6)
        self.assertEqual(workspace["point"], "http_initialize_runtime_factory")
        self.assertIs(workspace["immutable_per_session"], True)
        self.assertIs(workspace["ordinary_tool_switching"], False)
        self.assertEqual(workspace["invalid_mapping"], "fail_closed")
        self.assertEqual(telemetry, {"default_policy": "upstream_v0.2.2", "change_during_integration": False})
        self.assertIs(secret_stores["shared"], False)

    def test_phase08_admin_contract_is_machine_readable(self) -> None:
        admin = self.contract["admin_api"]
        self.assertEqual(admin["phase"], 8)
        self.assertEqual(admin["authentication"], "dedicated_admin_token")
        self.assertIs(admin["ordinary_mcp_bearer_is_admin"], False)
        self.assertIs(admin["handler_sql"], False)
        self.assertIs(admin["responses_redacted"], True)
        self.assertEqual(
            admin["settings_views"],
            ["active", "persisted", "pending_restart"],
        )
        self.assertEqual(admin["stale_update"], "revision_conflict")
        self.assertEqual(admin["gateway_change"], "persist_and_restart_only")
        self.assertIs(admin["gateway_dynamic_reload"], False)
        self.assertEqual(
            admin["allowed_origins_source"],
            "coding_tools_mcp.settings_definition.normalize_allowed_origins",
        )
        source = inspect.getsource(AdminService)
        self.assertNotRegex(source, r"\b(?:SELECT|INSERT|UPDATE|DELETE FROM|PRAGMA)\b")
        self.assertNotIn("reload_upstream", source)

    def test_phase10_webui_contract_is_machine_readable(self) -> None:
        webui = self.contract["webui"]
        self.assertEqual(webui["phase"], 10)
        self.assertEqual(webui["source_root"], "webui/src")
        self.assertEqual(webui["dist"], "build_generated_only")
        self.assertEqual(webui["authentication"], "dedicated_admin_token")
        self.assertEqual(webui["admin_token_storage"], "page_memory_only")
        self.assertIs(webui["tool_profile_controls"], False)
        self.assertIs(webui["safe_mode_hides_mutation_tools"], False)
        self.assertIs(webui["fake_readonly_security_boundary"], False)
        self.assertEqual(
            webui["settings_stale_update"],
            "preserve_draft_refresh_revision_conflict",
        )
        self.assertEqual(webui["gateway_change"], "persist_and_restart_only")
        self.assertIs(webui["gateway_dynamic_reload"], False)
        self.assertIs(webui["secret_material_displayed"], False)
        self.assertEqual(webui["conversation_list"], "summary_only")
        self.assertEqual(webui["conversation_detail"], "explicit_paginated")
        self.assertEqual(webui["untrusted_rendering"], "dom_text_content")

    def test_phase11_admin_telemetry_status_keeps_the_privacy_boundary(self) -> None:
        telemetry = self.contract["telemetry"]
        self.assertEqual(
            telemetry,
            {"default_policy": "upstream_v0.2.2", "change_during_integration": False},
        )
        source = inspect.getsource(AdminService.status_payload)
        self.assertIn("telemetry_mode", source)
        self.assertIn('"docs": "docs/telemetry.md"', source)
        for forbidden in (
            "workspace_id",
            "agent_id",
            "client_id",
            "command",
            "arguments",
            "file_content",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_phase09_chat_persistence_contract_is_machine_readable(self) -> None:
        chat = self.contract["chat_persistence"]
        self.assertEqual(chat["phase"], 9)
        self.assertIs(chat["workspace_keyed"], True)
        self.assertEqual(chat["ordinary_scope"], "immutable_workspace_service")
        self.assertEqual(chat["global_operations_authentication"], "dedicated_admin_token")
        self.assertEqual(chat["scan_roots"], "registered_workspace_relative_only")
        self.assertEqual(
            chat["scan_limits"],
            ["depth", "files", "file_bytes", "total_bytes", "messages"],
        )
        self.assertEqual(chat["malformed_record"], "item_error_continue")
        self.assertEqual(chat["list_default"], "summary_paginated")
        self.assertEqual(chat["full_content"], "explicit_detail_only")
        self.assertIs(chat["telemetry_content"], False)
        source = inspect.getsource(transcript_module) + inspect.getsource(codex_sessions_module)
        self.assertNotIn("telemetry", source.lower())
        self.assertIn("workspace_id", source)

    def test_phase03_settings_migration_drops_tool_profile(self) -> None:
        migration = self.contract["legacy_tool_profile_migration"]
        for case in migration["cases"]:
            value = case["input"]["tool_profile"]
            migrated, warnings = migrate_persisted_settings({"tool_profile": value})
            with self.subTest(tool_profile=value):
                self.assertNotIn("tool_profile", migrated)
                self.assertEqual(warnings, (LEGACY_TOOL_PROFILE_WARNING,))
                self.assertEqual(case["output"]["catalog"], "fixed")
                self.assertEqual(
                    case["output"]["warning"],
                    LEGACY_TOOL_PROFILE_WARNING,
                )

    def test_phase04_oauth_store_reopens_after_idempotent_migration(self) -> None:
        oauth = self.contract["oauth"]
        self.assertEqual(oauth["persistent_store_phase"], 4)
        self.assertEqual(oauth["migration"], "idempotent_transactional")
        with oauth_root() as root:
            path = root / "oauth.sqlite3"
            first = OAuthAuthorizationStore(path, pepper=b"contract-pepper" * 2)
            first.upsert_client(
                "contract-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
                workspace_id="contract-workspace",
            )
            first.register_signing_key(
                "contract-key",
                "contract-fingerprint",
                secret_ref="oauth-signing/contract-key",
            )
            grant_id = first.create_grant("contract-agent", "mcp")

            second = OAuthAuthorizationStore(path, pepper=b"contract-pepper" * 2)
            third = OAuthAuthorizationStore(path, pepper=b"contract-pepper" * 2)
            self.assertEqual(second.get_client("contract-agent")["client_id"], "contract-agent")
            self.assertEqual(third.get_grant(grant_id)["client_id"], "contract-agent")
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()

    def test_phase07_gateway_snapshot_contract_is_machine_readable(self) -> None:
        gateway = self.contract["gateway"]
        self.assertEqual(gateway["phase"], 7)
        self.assertEqual(gateway["namespace"], "{alias}__{remote_name}")
        self.assertIs(gateway["local_names_reserved"], True)
        self.assertEqual(gateway["collision"], "fail_closed")
        self.assertEqual(gateway["snapshot_point"], "runtime_initialize")
        self.assertIs(gateway["immutable_per_runtime"], True)
        self.assertIs(gateway["list_changed"], False)
        self.assertIs(gateway["config_before_initialize"], True)
        self.assertEqual(gateway["schema"], "preserve_except_public_name")
        self.assertEqual(gateway["annotations"], "preserve_real")
        self.assertEqual(gateway["structured_content"], "preserve")
        self.assertIs(gateway["remote_workspace_boundary_claim"], False)
        self.assertIs(gateway["session_identity_mutation"], False)
        self.assertIs(gateway["tool_profile_controls"], False)
        self.assertFalse(hasattr(UpstreamManager, "start_server"))
        self.assertFalse(hasattr(UpstreamManager, "stop_server"))
        self.assertNotIn("tool_profile", inspect.getsource(upstream_module))

    def test_phase06_http_session_binding_is_immutable_and_fails_closed(self) -> None:
        binding = self.contract["workspace_binding"]
        self.assertEqual(binding["phase"], 6)
        self.assertEqual(binding["point"], "http_initialize_runtime_factory")
        self.assertTrue(binding["immutable_per_session"])
        self.assertEqual(binding["invalid_mapping"], "fail_closed")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            resolver = WorkspaceBindingResolver(
                WorkspaceCatalog(
                    [
                        WorkspaceEntry("first", "First", first, enabled=True, default=True),
                        WorkspaceEntry("second", "Second", second, enabled=True),
                    ],
                    "first",
                )
            )
            identity = OAuthIdentity("agent", "grant", "second", "jti")
            resolved = resolver.resolve_http("oauth", identity)
            self.assertEqual(resolved.workspace_id, "second")
            resolver.update_catalog(
                WorkspaceCatalog(
                    [
                        WorkspaceEntry("first", "First", first, enabled=True, default=True),
                        WorkspaceEntry("second", "Second", second, enabled=False),
                    ],
                    "first",
                )
            )
            self.assertEqual(resolved.workspace_id, "second")
            with self.assertRaises(WorkspaceBindingError):
                resolver.resolve_http("oauth", identity)


if __name__ == "__main__":
    unittest.main()
