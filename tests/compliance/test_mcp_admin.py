from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.admin import (
    AdminConflictError,
    AdminNotFoundError,
    AdminService,
    AdminServiceError,
    AdminUnavailableError,
    document_revision,
    gateway_file_revision,
)
from coding_tools_mcp.oauth import OAUTH_PASSWORD_SECRET, OAuthAuthorizationPassword
from coding_tools_mcp.oauth_store import OAuthAuthorizationStore, OAuthStoreError
from coding_tools_mcp.runner.credentials import RunnerCredentialStore
from coding_tools_mcp.secret_vault import SecretVault
from coding_tools_mcp.server import (
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    configure_allowed_origins,
    is_allowed_origin,
    upstream_secret_resolver,
)
from coding_tools_mcp.settings_store import ServerSettingsStore
from coding_tools_mcp.transcript import TranscriptStore
from coding_tools_mcp.upstream import (
    UpstreamConfigSnapshot,
    UpstreamManager,
    UpstreamServerConfig,
)
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


def _cleanup_temporary_directory(temp: TemporaryDirectory[str]) -> None:
    for attempt in range(10):
        try:
            temp.cleanup()
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 145} or attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


class AdminServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace_a = self.root / "workspace-a"
        self.workspace_b = self.root / "workspace-b"
        self.workspace_a.mkdir()
        self.workspace_b.mkdir()
        self.settings_path = self.root / "server-settings.json"
        self.gateway_path = self.root / "mcp-servers.json"
        self.vault = SecretVault(self.root / "server-secrets.json", "admin-test-master-key")
        self.oauth_vault = SecretVault(
            self.root / "oauth-secrets.json", "admin-test-master-key"
        )
        self.oauth_password = OAuthAuthorizationPassword("initial-authorize-password")
        self.store = ServerSettingsStore(self.settings_path)
        catalog = WorkspaceCatalog(
            [WorkspaceEntry("a", "A", self.workspace_a, enabled=True, default=True)],
            "a",
        )
        initial = {
            **catalog.settings_payload(),
            "workspace": str(self.workspace_a),
            "host": "127.0.0.1",
            "port": 8000,
            "permission_mode": "safe",
            "shell_env_inherit": "core",
            "allowed_origins": ["https://admin.example"],
            "admin_token_secret_ref": "admin/token",
        }
        self.store.write(initial)
        self.active = dict(initial)
        self.active["port"] = 7000
        self.oauth = OAuthAuthorizationStore(
            self.root / "oauth.sqlite3",
            pepper=b"admin-pepper" * 4,
        )
        self.oauth.upsert_client(
            "agent-a",
            redirect_uri="http://127.0.0.1/callback",
            scopes="mcp",
            token_endpoint_auth_method="client_secret_post",
            client_secret_digest=hashlib.sha256(b"client-secret-canary").hexdigest(),
            workspace_id="a",
        )
        self.oauth.register_signing_key(
            "kid-a",
            "fingerprint-a",
            secret_ref="oauth/signing/kid-a",
        )
        self.grant_id = self.oauth.create_grant("agent-a", "mcp")
        self.oauth.record_access_token(
            "jti-a",
            self.grant_id,
            "agent-a",
            "kid-a",
            "mcp",
            issued_at=time.time(),
            expires_at=time.time() + 3600,
        )
        self.family_id, _refresh = self.oauth.issue_refresh_token(
            self.grant_id,
            "agent-a",
            "mcp",
            expires_at=time.time() + 7200,
        )
        self.active_gateway_status = {
            "enabled": True,
            "tool_count": 1,
            "servers": [{"alias": "active", "initialized": True}],
        }
        self.service = AdminService(
            settings_store=self.store,
            active_settings=self.active,
            fallback_workspace=self.workspace_a,
            gateway_path=self.gateway_path,
            active_gateway_revision=gateway_file_revision(self.gateway_path),
            secret_vault=self.vault,
            oauth_store=self.oauth,
            oauth_secret_vault=self.oauth_vault,
            oauth_password=self.oauth_password,
            active_gateway_status=lambda: self.active_gateway_status,
        )

    def tearDown(self) -> None:
        _cleanup_temporary_directory(self.temp)

    def test_status_reports_only_privacy_safe_telemetry_mode_and_docs(self) -> None:
        with patch("coding_tools_mcp.admin.telemetry_mode", return_value="debug") as mode:
            payload = self.service.status_payload()

        mode.assert_called_once_with()
        self.assertEqual(
            payload["telemetry"],
            {"mode": "debug", "docs": "docs/telemetry.md"},
        )
        serialized = json.dumps(payload["telemetry"], sort_keys=True)
        for forbidden in (
            "workspace_id",
            "agent_id",
            "client_id",
            "command",
            "arguments",
            "file_content",
            "path",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_admin_gateway_status_redacts_stdio_argv_values_and_flags(self) -> None:
        config = UpstreamServerConfig(
            alias="remote",
            transport="stdio",
            enabled=False,
            command="uvx",
            args=("--token", "SUPER-SECRET", "--verbose"),
        )
        manager = UpstreamManager((config,))
        try:
            self.service.active_gateway_status = manager.status_payload
            payload = self.service.gateway_payload()
            serialized = json.dumps(payload, sort_keys=True)
            target = payload["active_status"]["servers"][0]["target"]
            self.assertEqual(target, "uvx (3 args)")
            self.assertNotIn("--token", serialized)
            self.assertNotIn("SUPER-SECRET", serialized)
            self.assertNotIn("--verbose", serialized)
        finally:
            manager.close()

    def test_settings_separate_active_persisted_pending_and_reject_stale_revision(self) -> None:
        payload = self.service.settings_payload()
        self.assertEqual(payload["active"]["port"], 7000)
        self.assertEqual(payload["persisted"]["port"], 8000)
        self.assertIn("port", payload["pending_restart"])
        self.assertEqual(payload["persisted"]["admin_token_secret_ref"], {"configured": True})
        revision = payload["persisted_revision"]

        saved = self.service.save_settings(
            {
                "expected_revision": revision,
                "updates": {"port": 9000, "allowed_origins": ["https://ops.example/"]},
            }
        )
        self.assertEqual(saved["persisted"]["port"], 9000)
        self.assertEqual(saved["persisted"]["allowed_origins"], ["https://ops.example"])
        with self.assertRaises(AdminConflictError):
            self.service.save_settings(
                {"expected_revision": revision, "updates": {"port": 9001}}
            )

    def test_settings_report_effective_defaults_and_desktop_override_without_false_restart(self) -> None:
        persisted = self.store.read()
        persisted["port"] = 7000
        persisted["host"] = "0.0.0.0"
        persisted.pop("execution_fs_mode", None)
        self.store.write(persisted)
        self.service.active_settings["host"] = "127.0.0.1"
        self.service.active_settings["execution_fs_mode"] = "normal"
        self.service.active_sources.update(
            {
                "host": "desktop_cli",
                "execution_fs_mode": "default",
            }
        )
        self.service.launcher = "desktop"

        payload = self.service.settings_payload()

        host = payload["field_status"]["host"]
        execution = payload["field_status"]["execution_fs_mode"]
        self.assertEqual(host["source"], "desktop_cli")
        self.assertEqual(host["state"], "overridden")
        self.assertFalse(host["restart_actionable"])
        self.assertNotIn("host", payload["pending_restart"])
        self.assertIn("host", payload["managed_fields"])
        self.assertEqual(payload["launcher"], "desktop")
        self.assertEqual(execution["active"], "normal")
        self.assertIsNone(execution["persisted"])
        self.assertEqual(execution["effective_persisted"], "normal")
        self.assertEqual(execution["state"], "in_sync_default")
        self.assertNotIn("execution_fs_mode", payload["pending_restart"])

    def test_settings_and_http_cors_use_the_same_origin_validator(self) -> None:
        validated = self.service.validate_settings(
            {"updates": {"allowed_origins": ["HTTPS://Example.COM:443/"]}}
        )
        origins = validated["normalized"]["allowed_origins"]
        configure_allowed_origins(origins)
        self.assertTrue(is_allowed_origin("https://example.com:443"))
        self.assertFalse(is_allowed_origin("https://example.com/path"))
        with self.assertRaisesRegex(ValueError, "Unsupported allowed origin"):
            self.service.validate_settings({"updates": {"allowed_origins": ["*"]}})

    def test_gateway_save_is_redacted_restart_only_and_never_hot_reloads(self) -> None:
        self.vault.set_secret("github/token", "upstream-secret-canary")
        payload = self.service.gateway_payload()
        saved = self.service.save_gateway(
            {
                "expected_revision": payload["persisted_revision"],
                "document": {
                    "servers": {
                        "github": {
                            "transport": "stdio",
                            "command": "npx",
                            "args": ["-y", "example-mcp"],
                            "enabled": True,
                            "env": {"GITHUB_TOKEN": {"secret_ref": "github/token"}},
                            "expose_mode": "broker",
                            "pinned_tools": ["search"],
                            "tags": ["code", "remote"],
                            "tool_policy": {"create_issue": "readonly"},
                        }
                    },
                    "tool_search": {
                        "custom_synonyms": {"仓库": ["search", "repository"]}
                    },
                },
            }
        )
        self.assertTrue(saved["restart_required"])
        self.assertFalse(saved["dynamic_reload"])
        self.assertFalse(saved["list_changed"])
        self.assertEqual(saved["activation"], "new_mcp_session_or_service_restart")
        self.assertEqual(saved["new_server_defaults"], {"expose_mode": "broker"})
        self.assertEqual(saved["active_status"], self.active_gateway_status)
        self.assertNotIn("github/token", json.dumps(saved))
        persisted_text = self.gateway_path.read_text(encoding="utf-8")
        self.assertNotIn("upstream-secret-canary", persisted_text)
        persisted = json.loads(persisted_text)
        self.assertEqual(persisted["servers"]["github"]["expose_mode"], "broker")
        self.assertEqual(persisted["servers"]["github"]["pinned_tools"], ["search"])
        self.assertEqual(
            persisted["tool_search"]["custom_synonyms"]["仓库"],
            ["search", "repository"],
        )
        self.assertEqual(self.active_gateway_status["tool_count"], 1)
        with self.assertRaisesRegex(AdminServiceError, "Sensitive Gateway headers"):
            self.service.save_gateway(
                {
                    "expected_revision": saved["persisted_revision"],
                    "document": {
                        "servers": {
                            "remote": {
                                "transport": "streamable_http",
                                "url": "http://127.0.0.1:9000/mcp",
                                "headers": {"X-API-Key": "plaintext-canary"},
                            }
                        }
                    },
                }
            )

    def test_gateway_rejects_invalid_exposure_and_search_configuration(self) -> None:
        payload = self.service.gateway_payload()
        with self.assertRaisesRegex(AdminServiceError, "expose_mode"):
            self.service.save_gateway(
                {
                    "expected_revision": payload["persisted_revision"],
                    "document": {
                        "servers": {
                            "remote": {
                                "transport": "streamable_http",
                                "url": "http://127.0.0.1:9000/mcp",
                                "expose_mode": "live",
                            }
                        }
                    },
                }
            )
        with self.assertRaisesRegex(AdminServiceError, "custom_synonyms"):
            self.service.save_gateway(
                {
                    "expected_revision": payload["persisted_revision"],
                    "document": {
                        "servers": {},
                        "tool_search": {
                            "custom_synonyms": {"核磁": ["nmr", "nmr"]}
                        },
                    },
                }
            )

    def test_gateway_server_form_updates_are_atomic_and_preserve_hidden_credentials(self) -> None:
        self.vault.set_secret("chemistry/token", "secret-canary")
        created = self.service.save_gateway_server(
            "chemistry",
            {
                "expected_revision": self.service.gateway_payload()["persisted_revision"],
                "config": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "chemistry-mcp"],
                    "enabled": True,
                    "env": {"API_TOKEN": {"secret_ref": "chemistry/token"}},
                    "expose_mode": "broker",
                    "pinned_tools": ["search"],
                },
            },
        )
        updated = self.service.save_gateway_server(
            "chemistry",
            {
                "expected_revision": created["persisted_revision"],
                "config": {
                    "enabled": False,
                    "expose_mode": "direct",
                    "pinned_tools": [],
                },
            },
        )
        raw = json.loads(self.gateway_path.read_text(encoding="utf-8"))
        server = raw["servers"]["chemistry"]
        self.assertFalse(server["enabled"])
        self.assertEqual(server["expose_mode"], "direct")
        self.assertEqual(server["command"], "npx")
        self.assertEqual(
            server["env"]["API_TOKEN"],
            {"secret_ref": "chemistry/token"},
        )
        self.assertNotIn("chemistry/token", json.dumps(updated))
        self.assertTrue(updated["restart_required"])

        deleted = self.service.delete_gateway_server(
            "chemistry",
            {"expected_revision": updated["persisted_revision"]},
        )
        self.assertNotIn("chemistry", deleted["persisted"]["servers"])
        self.assertEqual(deleted["affected_count"], 1)
        with self.assertRaises(AdminConflictError):
            self.service.save_gateway_server(
                "chemistry",
                {
                    "expected_revision": created["persisted_revision"],
                    "config": {"enabled": True},
                },
            )

    def test_gateway_server_routes_support_webui_management(self) -> None:
        revision = self.service.gateway_payload()["persisted_revision"]
        saved = self.service.dispatch(
            "PUT",
            "/admin/api/gateway/servers/remote",
            {
                "expected_revision": revision,
                "config": {
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1:9000/mcp",
                    "enabled": True,
                    "expose_mode": "broker",
                },
            },
            {},
        )
        deleted = self.service.dispatch(
            "DELETE",
            "/admin/api/gateway/servers/remote",
            {"expected_revision": saved["persisted_revision"]},
            {},
        )
        self.assertEqual(deleted["affected_count"], 1)

    def test_gateway_secret_resolver_is_vault_backed_and_fails_closed(self) -> None:
        snapshot = UpstreamConfigSnapshot(
            configs=(
                UpstreamServerConfig(
                    alias="remote",
                    transport="stdio",
                    command="uvx",
                    args=("remote-mcp",),
                    env={"TOKEN": {"secret_ref": "remote/token"}},
                ),
            )
        )
        self.vault.set_secret("remote/token", "resolved-secret-canary")
        resolver = upstream_secret_resolver(snapshot, self.vault)
        self.assertIsNotNone(resolver)
        assert resolver is not None
        self.assertEqual(resolver("remote/token"), "resolved-secret-canary")
        with self.assertRaisesRegex(ValueError, "Gateway secret_ref requires"):
            upstream_secret_resolver(
                snapshot,
                SecretVault(self.root / "no-key.json", None),
            )

    def test_gateway_secret_ref_fails_closed_without_vault(self) -> None:
        service = AdminService(
            settings_store=self.store,
            active_settings=self.active,
            fallback_workspace=self.workspace_a,
            gateway_path=self.gateway_path,
            active_gateway_revision=gateway_file_revision(self.gateway_path),
            secret_vault=SecretVault(self.root / "disabled-vault.json", None),
        )
        with self.assertRaises(AdminUnavailableError):
            service.secrets_payload()
        with self.assertRaises(AdminUnavailableError):
            service.save_gateway(
                {
                    "expected_revision": service.gateway_payload()["persisted_revision"],
                    "document": {
                        "servers": {
                            "remote": {
                                "transport": "stdio",
                                "command": "uvx",
                                "args": ["remote-mcp"],
                                "env": {"TOKEN": {"secret_ref": "remote/token"}},
                            }
                        }
                    },
                }
            )

    def test_oauth_lists_are_redacted_and_actions_are_idempotent(self) -> None:
        clients = self.service.oauth_payload("clients", {})
        encoded = json.dumps(clients)
        self.assertNotIn("client-secret-canary", encoded)
        self.assertNotIn("client_secret_digest", encoded)
        keys = self.service.oauth_payload("signing-keys", {})
        self.assertNotIn("secret_ref", json.dumps(keys))

        first = self.service.oauth_action("tokens", "jti-a", "revoke")
        second = self.service.oauth_action("tokens", "jti-a", "revoke")
        self.assertEqual(first["affected_count"], 1)
        self.assertIsNotNone(first["audit_event_id"])
        self.assertEqual(second["affected_count"], 0)
        self.assertIsNone(second["audit_event_id"])

        family = self.service.oauth_action("refresh-families", self.family_id, "revoke")
        grant = self.service.oauth_action("grants", self.grant_id, "revoke")
        client = self.service.oauth_action("clients", "agent-a", "disable")
        key = self.service.oauth_action("signing-keys", "kid-a", "revoke")
        self.assertEqual(
            [family["affected_count"], grant["affected_count"], client["affected_count"], key["affected_count"]],
            [1, 1, 1, 1],
        )
        cannot_reactivate = self.service.oauth_action(
            "signing-keys", "kid-a", "activate"
        )
        self.assertEqual(cannot_reactivate["affected_count"], 0)
        self.assertIsNone(cannot_reactivate["audit_event_id"])

    def test_workspace_actions_reuse_catalog_validation_and_check_only_known_ids(self) -> None:
        payload = self.service.workspaces_payload()
        added = self.service.workspace_add(
            {
                "expected_revision": payload["persisted_revision"],
                "workspace": {
                    "id": "b",
                    "name": "B",
                    "root": str(self.workspace_b),
                    "enabled": True,
                },
            }
        )
        made_default = self.service.workspace_default(
            "b", {"expected_revision": added["persisted_revision"]}
        )
        self.assertEqual(made_default["default_workspace_id"], "b")
        checked = self.service.workspace_check("b")
        self.assertTrue(checked["check"]["is_directory"])
        disabled = self.service.workspace_disable(
            "a", {"expected_revision": made_default["persisted_revision"]}
        )
        self.assertFalse(next(item for item in disabled["workspace_catalog"] if item["id"] == "a")["enabled"])
        with self.assertRaisesRegex(ValueError, "not present"):
            self.service.workspace_check(str(self.root / "unmanaged"))

    def test_secret_api_never_returns_secret_values(self) -> None:
        result = self.service.set_secret("service/key", {"value": "vault-value-canary"})
        self.assertNotIn("vault-value-canary", json.dumps(result))
        self.assertTrue(result["created"])
        overwritten = self.service.set_secret(
            "service/key", {"value": "replacement-vault-canary"}
        )
        self.assertFalse(overwritten["created"])
        self.assertEqual(overwritten["affected_count"], 1)
        self.assertNotIn("replacement-vault-canary", json.dumps(overwritten))
        listed = self.service.secrets_payload()
        self.assertIn({"name": "service/key", "configured": True}, listed["secrets"])
        self.assertNotIn("vault-value-canary", json.dumps(listed))

    def test_oauth_password_secret_rotates_active_authorization_immediately(self) -> None:
        result = self.service.dispatch(
            "PUT",
            "/admin/api/secrets/oauth%2Fauthorization-password",
            {"value": "rotated-authorize-password"},
            {},
        )

        self.assertEqual(self.oauth_password.current(), "rotated-authorize-password")
        self.assertEqual(
            self.oauth_vault.get_secret(OAUTH_PASSWORD_SECRET),
            "rotated-authorize-password",
        )
        self.assertNotIn(OAUTH_PASSWORD_SECRET, self.vault.list_names())
        self.assertTrue(result["oauth_applied_immediately"])
        self.assertNotIn("rotated-authorize-password", json.dumps(result))
        self.assertIn(
            {
                "name": OAUTH_PASSWORD_SECRET,
                "configured": True,
                "usage": "oauth_authorization_password",
                "takes_effect": "immediate",
            },
            self.service.secrets_payload()["secrets"],
        )

    def test_client_authorize_password_overrides_global_and_can_reset_to_fallback(self) -> None:
        configured = self.service.dispatch(
            "PUT",
            "/admin/api/oauth/clients/agent-a/authorization-password",
            {"value": "agent-a-authorize-password"},
            {},
        )

        self.assertEqual(
            self.oauth_password.current("agent-a"),
            "agent-a-authorize-password",
        )
        self.assertEqual(
            self.oauth_password.current("unconfigured-client"),
            "initial-authorize-password",
        )
        self.assertTrue(configured["authorize_login"]["configured"])
        self.assertNotIn("agent-a-authorize-password", json.dumps(configured))
        client = next(
            item
            for item in self.service.oauth_payload("clients", {})["items"]
            if item["client_id"] == "agent-a"
        )
        self.assertEqual(
            client["authorize_login"],
            {"configured": True, "mode": "client"},
        )
        viewed = self.service.dispatch(
            "GET",
            "/admin/api/oauth/clients/agent-a/authorization-password",
            {},
            {},
        )
        self.assertEqual(viewed["value"], "agent-a-authorize-password")
        self.assertEqual(
            viewed["authorize_login"],
            {"configured": True, "mode": "client"},
        )

        reset = self.service.dispatch(
            "DELETE",
            "/admin/api/oauth/clients/agent-a/authorization-password",
            {},
            {},
        )
        self.assertEqual(reset["affected_count"], 1)
        self.assertEqual(
            self.oauth_password.current("agent-a"),
            "initial-authorize-password",
        )
        self.assertEqual(
            reset["authorize_login"],
            {"configured": False, "mode": "global"},
        )
        with self.assertRaises(AdminNotFoundError):
            self.service.dispatch(
                "GET",
                "/admin/api/oauth/clients/agent-a/authorization-password",
                {},
                {},
            )

    def test_oauth_client_workspace_allowlist_applies_immediately(self) -> None:
        self.oauth.upsert_client(
            "unbound-agent",
            redirect_uri="http://127.0.0.1/callback",
            scopes="mcp",
            workspace_id=None,
        )
        self.service.active_settings = {
            **WorkspaceCatalog(
                [
                    WorkspaceEntry("a", "A", self.workspace_a, enabled=True, default=True),
                    WorkspaceEntry("b", "B", self.workspace_b, enabled=True),
                ],
                "a",
            ).settings_payload(),
            "workspace": str(self.workspace_a),
        }

        result = self.service.dispatch(
            "PUT",
            "/admin/api/oauth/clients/unbound-agent/workspaces",
            {"workspace_ids": ["a", "b"]},
            {},
        )

        self.assertEqual(
            result["workspace_access"],
            {"configured": True, "workspace_ids": ["a", "b"]},
        )
        self.assertTrue(result["applied_immediately"])
        self.assertEqual(
            self.oauth.get_client("unbound-agent")["workspace_ids"], ["a", "b"]
        )
        grant_id = self.oauth.create_grant(
            "unbound-agent", "mcp", workspace_id="a"
        )
        self.assertEqual(self.oauth.get_grant(grant_id)["workspace_id"], "a")

        updated = self.service.dispatch(
            "PUT",
            "/admin/api/oauth/clients/unbound-agent/workspaces",
            {"workspace_ids": ["b"]},
            {},
        )
        self.assertEqual(updated["workspace_access"]["workspace_ids"], ["b"])
        with self.assertRaisesRegex(OAuthStoreError, "not authorized"):
            self.oauth.create_grant("unbound-agent", "mcp", workspace_id="a")
        second_grant = self.oauth.create_grant("unbound-agent", "mcp")
        self.assertEqual(self.oauth.get_grant(second_grant)["workspace_id"], "b")
        self.assertEqual(self.oauth.get_grant(grant_id)["workspace_id"], "a")

        with self.assertRaisesRegex(AdminServiceError, "unknown or disabled"):
            self.service.dispatch(
                "PUT",
                "/admin/api/oauth/clients/unbound-agent/workspaces",
                {"workspace_ids": ["missing"]},
                {},
            )

    def test_handler_source_contains_no_sql_or_gateway_reload(self) -> None:
        source = inspect.getsource(MCPHandler)
        self.assertNotRegex(source, r"\b(?:SELECT|INSERT|UPDATE|DELETE FROM|PRAGMA)\b")
        admin_source = inspect.getsource(AdminService)
        self.assertNotIn("reload_upstream", admin_source)
        self.assertNotIn("start_server", admin_source)
        self.assertNotIn("stop_server", admin_source)

    def test_remote_workspace_metadata_is_valid_but_filesystem_scope_fails_closed(self) -> None:
        current = self.store.read()
        catalog = WorkspaceCatalog(
            [
                WorkspaceEntry("a", "A", self.workspace_a, enabled=True, default=True),
                WorkspaceEntry(
                    "remote",
                    "Remote",
                    r"G:\\repo",
                    enabled=True,
                    default=False,
                    target="runner",
                    runner_id="home-win",
                ),
            ],
            "a",
        )
        current.update(catalog.settings_payload())
        self.store.write(current)

        entry = self.service._workspace_entry("remote")
        self.assertEqual(entry.target, "runner")
        self.assertEqual(entry.root, r"G:\\repo")
        with self.assertRaisesRegex(AdminServiceError, "Runner Data Plane"):
            self.service._workspace_scope("remote")


class AdminHTTPAuthenticationTests(unittest.TestCase):
    def test_admin_browser_session_exchange_requires_csrf_and_logout_revokes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ServerSettingsStore(root / "settings.json")
            workspace = root / "workspace"
            workspace.mkdir()
            settings.write({"workspace": str(workspace), "port": 8000})
            vault = SecretVault(root / "vault.json", "key")
            service = AdminService(
                settings_store=settings,
                active_settings={"workspace": str(workspace), "port": 8000},
                fallback_workspace=workspace,
                gateway_path=root / "gateway.json",
                active_gateway_revision=document_revision({"servers": {}}),
                secret_vault=vault,
            )
            runtime = Runtime(workspace, transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda _context: Runtime(workspace, transport="http"),
                admin_service=service,
                admin_token="dedicated-admin-token",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}/admin/api"
            try:
                login = urllib.request.Request(
                    f"{base}/session",
                    data=json.dumps({"admin_token": "dedicated-admin-token"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Forwarded-Proto": "https",
                    },
                    method="POST",
                )
                with patch.dict(
                    "os.environ",
                    {"CODING_TOOLS_MCP_TRUST_PROXY_HEADERS": "1"},
                    clear=False,
                ):
                    with urllib.request.urlopen(login, timeout=5) as response:
                        payload = json.loads(response.read())
                        set_cookie = response.headers.get("Set-Cookie", "")
                self.assertEqual(response.status, 201)
                self.assertIn("csrf_token", payload)
                self.assertNotIn("dedicated-admin-token", json.dumps(payload))
                self.assertIn("coding_tools_mcp_admin_session=", set_cookie)
                self.assertIn("Path=/admin/api", set_cookie)
                self.assertIn("HttpOnly", set_cookie)
                self.assertIn("SameSite=Strict", set_cookie)
                self.assertIn("Secure", set_cookie)
                self.assertNotIn("dedicated-admin-token", set_cookie)
                cookie = set_cookie.split(";", 1)[0]

                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base}/status",
                        headers={"Cookie": cookie},
                    ),
                    timeout=5,
                ) as response:
                    status_payload = json.loads(response.read())
                self.assertTrue(status_payload["ok"])

                current_revision = service.settings_payload()["persisted_revision"]
                without_csrf = urllib.request.Request(
                    f"{base}/settings",
                    data=json.dumps(
                        {
                            "expected_revision": current_revision,
                            "updates": {"port": 8100},
                        }
                    ).encode("utf-8"),
                    headers={"Cookie": cookie, "Content-Type": "application/json"},
                    method="PUT",
                )
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(without_csrf, timeout=5)
                self.assertEqual(denied.exception.code, 403)
                self.assertEqual(
                    json.loads(denied.exception.read())["error"]["code"],
                    "admin_csrf_required",
                )

                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base}/settings",
                        data=json.dumps(
                            {
                                "expected_revision": current_revision,
                                "updates": {"port": 8100},
                            }
                        ).encode("utf-8"),
                        headers={
                            "Cookie": cookie,
                            "Content-Type": "application/json",
                            "X-Admin-CSRF": payload["csrf_token"],
                        },
                        method="PUT",
                    ),
                    timeout=5,
                ) as response:
                    saved = json.loads(response.read())
                self.assertEqual(saved["persisted"]["port"], 8100)

                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base}/session",
                        headers={
                            "Cookie": cookie,
                            "X-Admin-CSRF": payload["csrf_token"],
                        },
                        method="DELETE",
                    ),
                    timeout=5,
                ) as response:
                    logout = json.loads(response.read())
                    cleared_cookie = response.headers.get("Set-Cookie", "")
                self.assertTrue(logout["revoked"])
                self.assertIn("Max-Age=0", cleared_cookie)

                with self.assertRaises(urllib.error.HTTPError) as revoked:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"{base}/status",
                            headers={"Cookie": cookie},
                        ),
                        timeout=5,
                    )
                self.assertEqual(revoked.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_admin_shell_is_public_but_api_requires_dedicated_token(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ServerSettingsStore(root / "settings.json")
            workspace = root / "workspace"
            workspace.mkdir()
            settings.write({"workspace": str(workspace)})
            vault = SecretVault(root / "vault.json", "key")
            runner_credentials = RunnerCredentialStore(vault)
            service = AdminService(
                settings_store=settings,
                active_settings={"workspace": str(workspace)},
                fallback_workspace=workspace,
                gateway_path=root / "gateway.json",
                active_gateway_revision=document_revision({"servers": {}}),
                secret_vault=vault,
                runner_credentials=runner_credentials,
                transcript_store=TranscriptStore(root / "transcripts.sqlite3"),
            )
            runtime = Runtime(workspace, auth_token="ordinary-mcp-token", transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda _context: Runtime(workspace, transport="http"),
                admin_service=service,
                admin_token="dedicated-admin-token",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/admin/api/status"
            try:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            url,
                            headers={"Authorization": "Bearer ordinary-mcp-token"},
                        ),
                        timeout=5,
                    )
                self.assertEqual(denied.exception.code, 401)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/admin",
                    timeout=5,
                ) as response:
                    page = response.read().decode("utf-8")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/app",
                    timeout=5,
                ) as response:
                    operator_page = response.read().decode("utf-8")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/wiki",
                    timeout=5,
                ) as response:
                    wiki_page = response.read().decode("utf-8")
                self.assertIn('data-build-source="i18n.js"', page)
                self.assertIn('data-build-source="admin.js"', page)
                self.assertIn('data-build-source="app/model.js"', operator_page)
                self.assertIn('data-build-source="app/api-client.js"', operator_page)
                self.assertIn('data-build-source="app/app.js"', operator_page)
                self.assertIn("/api/app", operator_page)
                self.assertNotIn("/admin/api", operator_page)
                self.assertIn("不是 GPT/ChatGPT 网页", operator_page)
                self.assertIn("Coding Tools MCP SQLite 与 Codex thread store", operator_page)
                self.assertIn("Coding Tools MCP 使用 Wiki", wiki_page)
                self.assertIn("CODING_TOOLS_MCP_AUTH_TOKEN", wiki_page)
                self.assertIn('href="/app"', wiki_page)
                before = service.settings_payload()["persisted_revision"]
                with self.assertRaises(urllib.error.HTTPError) as write_denied:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"http://127.0.0.1:{server.server_address[1]}/admin/api/settings",
                            data=json.dumps(
                                {
                                    "expected_revision": before,
                                    "updates": {"port": 9999},
                                }
                            ).encode("utf-8"),
                            headers={
                                "Authorization": "Bearer ordinary-mcp-token",
                                "Content-Type": "application/json",
                            },
                            method="PUT",
                        ),
                        timeout=5,
                    )
                self.assertEqual(write_denied.exception.code, 401)
                self.assertEqual(service.settings_payload()["persisted_revision"], before)
                with self.assertRaises(urllib.error.HTTPError) as delete_denied:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"http://127.0.0.1:{server.server_address[1]}/admin/api/chat/messages/ws-any/message-any",
                            headers={"Authorization": "Bearer ordinary-mcp-token"},
                            method="DELETE",
                        ),
                        timeout=5,
                    )
                self.assertEqual(delete_denied.exception.code, 401)
                runner_url = (
                    f"http://127.0.0.1:{server.server_address[1]}"
                    "/admin/api/runners/home-win/credential"
                )
                with self.assertRaises(urllib.error.HTTPError) as runner_denied:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            runner_url,
                            data=b"{}",
                            headers={
                                "Authorization": "Bearer ordinary-mcp-token",
                                "Content-Type": "application/json",
                            },
                            method="POST",
                        ),
                        timeout=5,
                    )
                self.assertEqual(runner_denied.exception.code, 401)
                with urllib.request.urlopen(
                    urllib.request.Request(
                        runner_url,
                        data=b"{}",
                        headers={
                            "Authorization": "Bearer dedicated-admin-token",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    ),
                    timeout=5,
                ) as response:
                    issued_runner = json.loads(response.read())
                self.assertEqual(issued_runner["runner_id"], "home-win")
                self.assertTrue(issued_runner["credential"])
                with urllib.request.urlopen(
                    urllib.request.Request(
                        runner_url,
                        headers={"Authorization": "Bearer dedicated-admin-token"},
                    ),
                    timeout=5,
                ) as response:
                    runner_metadata = json.loads(response.read())
                self.assertNotIn("credential", runner_metadata)
                self.assertEqual(runner_metadata["fingerprint"], issued_runner["fingerprint"])
                with urllib.request.urlopen(
                    urllib.request.Request(
                        runner_url,
                        headers={"Authorization": "Bearer dedicated-admin-token"},
                        method="DELETE",
                    ),
                    timeout=5,
                ) as response:
                    revoked_runner = json.loads(response.read())
                self.assertTrue(revoked_runner["revoked"])
                with urllib.request.urlopen(
                    urllib.request.Request(
                        url,
                        headers={"Authorization": "Bearer dedicated-admin-token"},
                    ),
                    timeout=5,
                ) as response:
                    payload = json.loads(response.read())
                self.assertTrue(payload["ok"])
                self.assertIn('data-language-toggle', page)
                self.assertIn('McpI18n', page)
                self.assertNotIn('src="./i18n.js"', page)
                self.assertNotIn('src="./admin.js"', page)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
