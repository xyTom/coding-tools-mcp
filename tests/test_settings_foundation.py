from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import secret_vault as secret_vault_module
from coding_tools_mcp import settings_store as settings_store_module
from coding_tools_mcp.secret_vault import SecretVault, SecretVaultError
from coding_tools_mcp.settings_definition import (
    LEGACY_TOOL_PROFILE_WARNING,
    SettingsValidationError,
    normalize_startup_settings_with_warnings,
    pending_restart_fields,
    schema_payload,
)
from coding_tools_mcp.settings_store import (
    SETTINGS_SCHEMA_VERSION,
    ServerSettingsStore,
    SettingsStoreError,
    default_settings_dir,
    sanitize_settings,
)
from coding_tools_mcp.workspace_catalog import (
    WorkspaceCatalog,
    WorkspaceCatalogError,
    WorkspaceEntry,
)


class SettingsStoreTests(unittest.TestCase):
    def test_default_settings_dir_honors_package_config_override(self) -> None:
        with TemporaryDirectory() as tmp:
            configured = Path(tmp) / "configured"
            with patch.dict(
                os.environ,
                {"CODING_TOOLS_MCP_CONFIG_DIR": str(configured)},
                clear=False,
            ):
                self.assertEqual(default_settings_dir(), configured)

    def test_settings_round_trip_and_schema_version(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "server-settings.json"
            store = ServerSettingsStore(path)
            warnings = store.write(
                {
                    "host": "127.0.0.1",
                    "port": 8765,
                    "oauth_active_key_secret_ref": "oauth-signing/key-a",
                }
            )

            self.assertEqual(warnings, ())
            persisted = store.read()
            self.assertEqual(persisted["schema_version"], SETTINGS_SCHEMA_VERSION)
            self.assertEqual(persisted["port"], 8765)
            self.assertEqual(
                persisted["oauth_active_key_secret_ref"],
                "oauth-signing/key-a",
            )

    def test_legacy_schema_and_tool_profile_are_migrated_in_memory(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "server-settings.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 0,
                        "host": "127.0.0.1",
                        "tool_profile": "read-only",
                    }
                ),
                encoding="utf-8",
            )

            result = ServerSettingsStore(path).read_result()

            self.assertTrue(result.migrated)
            self.assertEqual(result.warnings, (LEGACY_TOOL_PROFILE_WARNING,))
            self.assertNotIn("tool_profile", result.settings)
            self.assertEqual(result.settings["schema_version"], SETTINGS_SCHEMA_VERSION)

    def test_next_successful_write_omits_legacy_tool_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "server-settings.json"
            store = ServerSettingsStore(path)

            warnings = store.write({"host": "localhost", "tool_profile": "unknown-profile"})
            raw = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(warnings, (LEGACY_TOOL_PROFILE_WARNING,))
            self.assertNotIn("tool_profile", raw)

    def test_atomic_replace_failure_preserves_previous_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "server-settings.json"
            store = ServerSettingsStore(path)
            store.write({"port": 8000})
            original = path.read_bytes()

            with patch.object(
                settings_store_module.os,
                "replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaises(SettingsStoreError):
                    store.write({"port": 9000})

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(store.read()["port"], 8000)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_plaintext_secrets_are_rejected_and_sanitized(self) -> None:
        settings = {
            "oauth_token_secret": "secret-canary",
            "oauth_active_key_secret_ref": "oauth-signing/key-a",
        }
        sanitized = sanitize_settings(settings)

        self.assertNotIn("secret-canary", repr(sanitized))
        self.assertTrue(sanitized["oauth_token_secret_configured"])
        self.assertEqual(
            sanitized["oauth_active_key_secret_ref"],
            {"configured": True},
        )
        with TemporaryDirectory() as tmp:
            with self.assertRaises(SettingsStoreError):
                ServerSettingsStore(Path(tmp) / "server-settings.json").write(settings)


class SettingsDefinitionTests(unittest.TestCase):
    def test_normalization_ignores_tool_profile_and_reports_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            normalized, warnings = normalize_startup_settings_with_warnings(
                {"tool_profile": "compat-readonly-all", "permission_mode": "safe"},
                {
                    "tool_profile": "read-only",
                    "host": "LOCALHOST",
                    "port": "8765",
                    "workspace": str(workspace),
                },
                workspace,
            )

        self.assertEqual(warnings, (LEGACY_TOOL_PROFILE_WARNING,))
        self.assertNotIn("tool_profile", normalized)
        self.assertEqual(normalized["host"], "localhost")
        self.assertEqual(normalized["port"], 8765)
        self.assertNotIn("tool_profile", schema_payload())
        self.assertNotIn("tool_profile", schema_payload()["restart_fields"])

    def test_oauth_client_workspace_bindings_require_enabled_catalog_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            updates = {
                "workspace_catalog": [
                    {
                        "id": "first",
                        "name": "First",
                        "root": str(first),
                        "enabled": True,
                        "default": True,
                    },
                    {
                        "id": "second",
                        "name": "Second",
                        "root": str(second),
                        "enabled": False,
                        "default": False,
                    },
                ],
                "default_workspace_id": "first",
                "oauth_client_workspace_bindings": {"agent-a": "first"},
            }
            normalized, _warnings = normalize_startup_settings_with_warnings(
                {}, updates, first
            )
            self.assertEqual(
                normalized["oauth_client_workspace_bindings"],
                {"agent-a": "first"},
            )
            self.assertIn(
                "oauth_client_workspace_bindings",
                schema_payload()["restart_fields"],
            )

            updates["oauth_client_workspace_bindings"] = {"agent-a": "second"}
            with self.assertRaisesRegex(SettingsValidationError, "unknown or disabled"):
                normalize_startup_settings_with_warnings({}, updates, first)

    def test_pending_restart_fields_compares_only_active_settings(self) -> None:
        active = {"port": 8000, "permission_mode": "safe", "tool_profile": "read-only"}
        persisted = {"port": 9000, "permission_mode": "safe"}

        self.assertEqual(pending_restart_fields(active, persisted), ["port"])


class WorkspaceCatalogTests(unittest.TestCase):
    def test_catalog_canonicalizes_one_default_and_hides_disabled_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            catalog = WorkspaceCatalog(
                [
                    WorkspaceEntry("first", " First ", first),
                    WorkspaceEntry("second", "Second", second, enabled=False),
                ],
                "first",
            )

            self.assertTrue(catalog.default().default)
            self.assertEqual(catalog.default().name, "First")
            self.assertEqual([item.id for item in catalog.enabled_entries()], ["first"])
            self.assertEqual([item["id"] for item in catalog.payload()["workspaces"]], ["first"])
            with self.assertRaises(WorkspaceCatalogError):
                catalog.get("second")

    def test_duplicate_ids_and_multiple_defaults_are_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            with self.assertRaisesRegex(WorkspaceCatalogError, "IDs must be unique"):
                WorkspaceCatalog(
                    [
                        WorkspaceEntry("same", "First", first),
                        WorkspaceEntry("same", "Second", second),
                    ],
                    "same",
                )
            with self.assertRaisesRegex(WorkspaceCatalogError, "only one default"):
                WorkspaceCatalog(
                    [
                        WorkspaceEntry("first", "First", first, default=True),
                        WorkspaceEntry("second", "Second", second, default=True),
                    ],
                    "first",
                )

    def test_disabled_default_nested_roots_and_missing_paths_are_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent"
            child = parent / "child"
            parent.mkdir()
            child.mkdir()
            with self.assertRaisesRegex(WorkspaceCatalogError, "default workspace must be enabled"):
                WorkspaceCatalog(
                    [WorkspaceEntry("parent", "Parent", parent, enabled=False)],
                    "parent",
                )
            with self.assertRaisesRegex(WorkspaceCatalogError, "Nested workspace roots"):
                WorkspaceCatalog(
                    [
                        WorkspaceEntry("parent", "Parent", parent),
                        WorkspaceEntry("child", "Child", child),
                    ],
                    "parent",
                )
            with self.assertRaisesRegex(WorkspaceCatalogError, "cannot be resolved"):
                WorkspaceCatalog.single(root / "missing")


class SecretVaultTests(unittest.TestCase):
    def test_round_trip_is_encrypted_and_wrong_key_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "oauth-secrets.json"
            vault = SecretVault(path, "test-master-key")
            vault.set_secret("oauth-signing/key-a", "secret-canary-value")

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-canary-value", raw)
            self.assertEqual(vault.list_names(), ["oauth-signing/key-a"])
            self.assertEqual(vault.get_secret("oauth-signing/key-a"), "secret-canary-value")
            with self.assertRaisesRegex(SecretVaultError, "incorrect|modified"):
                SecretVault(path, "wrong-master-key").get_secret("oauth-signing/key-a")
            self.assertTrue(vault.delete_secret("oauth-signing/key-a"))
            self.assertFalse(vault.delete_secret("oauth-signing/key-a"))

    def test_atomic_replace_failure_preserves_previous_vault(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "oauth-secrets.json"
            vault = SecretVault(path, "test-master-key")
            vault.set_secret("stable", "old-value")
            original = path.read_bytes()

            with patch.object(
                secret_vault_module.os,
                "replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaises(SecretVaultError):
                    vault.set_secret("new", "new-value")

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(vault.get_secret("stable"), "old-value")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_unsupported_vault_version_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "oauth-secrets.json"
            path.write_text('{"version": 99, "secrets": {}}\n', encoding="utf-8")
            with self.assertRaisesRegex(SecretVaultError, "unsupported version"):
                SecretVault(path, "test-master-key").list_names()


if __name__ == "__main__":
    unittest.main()
