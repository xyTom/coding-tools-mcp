from __future__ import annotations

import inspect
import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.oauth import (
    OAUTH_GRANT_TYPES_SUPPORTED,
    OAUTH_RESPONSE_TYPES_SUPPORTED,
    OAuthClientRegistry,
)
from coding_tools_mcp.protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from coding_tools_mcp.server import MCPHandler, Runtime, TOOL_REGISTRY, build_parser
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
        self.assertEqual(registered["grant_types"], ["authorization_code"])
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

    @unittest.skip("Phase 04: replace this skip after the persistent OAuth Store and migrations are ported")
    def test_phase04_oauth_store_reopens_after_idempotent_migration(self) -> None:
        self.fail("Phase 04 must prove reopen persistence and repeatable transactional migration")

    @unittest.skip("Phase 06: replace this skip after Agent-to-Workspace binding occurs at HTTP initialize")
    def test_phase06_http_session_binding_is_immutable_and_fails_closed(self) -> None:
        self.fail("Phase 06 must prove one immutable Workspace binding per MCP HTTP session")


if __name__ == "__main__":
    unittest.main()
