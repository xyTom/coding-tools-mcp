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
    AdminService,
    AdminServiceError,
    AdminUnavailableError,
    document_revision,
    gateway_file_revision,
)
from coding_tools_mcp.oauth_store import OAuthAuthorizationStore
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
from coding_tools_mcp.upstream import UpstreamConfigSnapshot, UpstreamServerConfig
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


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
            active_gateway_status=lambda: self.active_gateway_status,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

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
                        }
                    }
                },
            }
        )
        self.assertTrue(saved["restart_required"])
        self.assertFalse(saved["dynamic_reload"])
        self.assertEqual(saved["active_status"], self.active_gateway_status)
        self.assertNotIn("github/token", json.dumps(saved))
        self.assertNotIn("upstream-secret-canary", self.gateway_path.read_text(encoding="utf-8"))
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

    def test_handler_source_contains_no_sql_or_gateway_reload(self) -> None:
        source = inspect.getsource(MCPHandler)
        self.assertNotRegex(source, r"\b(?:SELECT|INSERT|UPDATE|DELETE FROM|PRAGMA)\b")
        admin_source = inspect.getsource(AdminService)
        self.assertNotIn("reload_upstream", admin_source)
        self.assertNotIn("start_server", admin_source)
        self.assertNotIn("stop_server", admin_source)


class AdminHTTPAuthenticationTests(unittest.TestCase):
    def test_ordinary_mcp_bearer_is_not_admin_authentication(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ServerSettingsStore(root / "settings.json")
            workspace = root / "workspace"
            workspace.mkdir()
            settings.write({"workspace": str(workspace)})
            service = AdminService(
                settings_store=settings,
                active_settings={"workspace": str(workspace)},
                fallback_workspace=workspace,
                gateway_path=root / "gateway.json",
                active_gateway_revision=document_revision({"servers": {}}),
                secret_vault=SecretVault(root / "vault.json", "key"),
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
                with self.assertRaises(urllib.error.HTTPError) as page_denied:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"http://127.0.0.1:{server.server_address[1]}/admin",
                            headers={"Authorization": "Bearer ordinary-mcp-token"},
                        ),
                        timeout=5,
                    )
                self.assertEqual(page_denied.exception.code, 401)
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
                with urllib.request.urlopen(
                    urllib.request.Request(
                        url,
                        headers={"Authorization": "Bearer dedicated-admin-token"},
                    ),
                    timeout=5,
                ) as response:
                    payload = json.loads(response.read())
                self.assertTrue(payload["ok"])
                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{server.server_address[1]}/admin",
                        headers={"Authorization": "Bearer dedicated-admin-token"},
                    ),
                    timeout=5,
                ) as response:
                    page = response.read().decode("utf-8")
                self.assertIn('data-build-source="admin.js"', page)
                self.assertNotIn('src="./admin.js"', page)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
