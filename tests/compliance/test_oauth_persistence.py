from __future__ import annotations

import json
import os
import io
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import urllib.parse
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.oauth_store import OAuthAuthorizationStore
from coding_tools_mcp.server import (
    MCPHandler,
    OAuthConfig,
    Runtime,
    RuntimeHTTPServer,
    _create_oauth_token,
    _decode_oauth_token,
    _oauth_client_id_allowed,
    _oauth_effective_scope,
    _resolve_oauth_authorization_password,
    _resolve_oauth_refresh_pepper,
    _resolve_oauth_token_secret,
)
from coding_tools_mcp.settings_store import ServerSettingsStore
from coding_tools_mcp.secret_vault import SecretVault
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry
from tests.compliance.mcp_client import MCPClient


class OAuthAuthorizationStoreTests(unittest.TestCase):
    def test_refresh_rotation_rejects_reuse_and_revokes_the_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OAuthAuthorizationStore(Path(tmp) / "oauth.sqlite3", pepper=b"p" * 32)
            store.upsert_client("agent-a", display_name="Agent A", redirect_uri="http://127.0.0.1/callback", scopes="mcp")
            grant_id = store.create_grant("agent-a", "mcp")
            family_id, refresh_token = store.issue_refresh_token(grant_id, "agent-a", "mcp", expires_at=4_000_000_000)

            replacement = store.rotate_refresh_token(refresh_token, expires_at=4_000_000_000)
            self.assertIsNotNone(replacement)
            self.assertFalse(store.refresh_family_is_revoked(family_id))

            self.assertIsNone(store.rotate_refresh_token(refresh_token, expires_at=4_000_000_000))
            self.assertTrue(store.refresh_family_is_revoked(family_id))

    def test_revoked_access_token_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OAuthAuthorizationStore(Path(tmp) / "oauth.sqlite3", pepper=b"p" * 32)
            store.upsert_client("agent-a", display_name="Agent A", redirect_uri="http://127.0.0.1/callback", scopes="mcp")
            grant_id = store.create_grant("agent-a", "mcp")
            store.record_access_token("token-a", grant_id, "agent-a", "key-a", "mcp", issued_at=1, expires_at=4_000_000_000)
            self.assertTrue(store.access_token_is_active("token-a", now=2))
            store.revoke_access_token("token-a", reason="administrator")
            self.assertFalse(store.access_token_is_active("token-a", now=2))


    def test_refresh_token_families_are_listed_for_admin_management(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OAuthAuthorizationStore(Path(tmp) / "oauth.sqlite3", pepper=b"p" * 32)
            store.upsert_client("agent-a", display_name="Agent A", redirect_uri="http://127.0.0.1/callback", scopes="mcp")
            grant_id = store.create_grant("agent-a", "mcp")
            family_id, _token = store.issue_refresh_token(grant_id, "agent-a", "mcp", expires_at=4_000_000_000)

            families = store.list_refresh_token_families("agent-a")

            self.assertEqual([item["family_id"] for item in families], [family_id])
            self.assertNotIn("token_hash", families[0])

    def test_external_clients_are_downscoped_and_admin_console_remains_available(self) -> None:
        cfg = OAuthConfig("restricted-client", None, "password", "https://mcp.example.com", b"s" * 32)

        self.assertTrue(_oauth_client_id_allowed("admin-console", cfg))
        self.assertEqual(
            _oauth_effective_scope(
                "mcp admin",
                cfg,
                client_id="restricted-client",
                redirect_uri="https://chatgpt.com/connector/callback",
                server_url="https://mcp.example.com",
            ),
            "mcp",
        )
        self.assertEqual(
            _oauth_effective_scope(
                "admin",
                cfg,
                client_id="admin-console",
                redirect_uri="https://mcp.example.com/admin",
                server_url="https://mcp.example.com",
            ),
            "admin",
        )

    def test_plaintext_refresh_pepper_migrates_to_secret_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "server-settings.json"
            settings: dict[str, object] = {"oauth_refresh_token_pepper": "33" * 32}
            ServerSettingsStore(settings_path).write(settings)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")

            pepper = _resolve_oauth_refresh_pepper(settings, settings_path, secret_vault=vault)
            persisted = ServerSettingsStore(settings_path).read()

            self.assertEqual(pepper, bytes.fromhex("33" * 32))
            self.assertNotIn("oauth_refresh_token_pepper", persisted)
            self.assertEqual(persisted["oauth_refresh_token_pepper_secret_ref"], "oauth-refresh/pepper")
            self.assertEqual(vault.get_secret("oauth-refresh/pepper"), "33" * 32)

    def test_oauth_metadata_advertises_refresh_and_external_resource_scope_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=b"p" * 32)
            cfg = OAuthConfig(None, None, "password", None, b"s" * 32, store=store)
            runtime = Runtime(root, oauth_config=cfg)
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                metadata = json.loads(urllib.request.urlopen(f"{base}/.well-known/oauth-authorization-server", timeout=5).read())
                resource = json.loads(urllib.request.urlopen(f"{base}/.well-known/oauth-protected-resource", timeout=5).read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertIn("refresh_token", metadata["grant_types_supported"])
            self.assertEqual(resource["scopes_supported"], ["mcp"])

    def test_oauth_persistence_status_reports_locked_vault_path_and_plaintext_migration_need(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "server-settings.json"
            settings = {"oauth_token_secret": "11" * 32, "oauth_refresh_token_pepper": "22" * 32}
            vault = SecretVault(root / "oauth-secrets.json", None)
            cfg = OAuthConfig(None, None, "password", None, b"s" * 32, secret_vault=vault)
            runtime = Runtime(root, oauth_config=cfg, settings_path=settings_path, startup_settings=settings)

            payload = runtime.oauth_persistence_payload(base_url="https://mcp.example.com")

            self.assertFalse(payload["vault"]["enabled"])
            self.assertEqual(payload["vault"]["path"], str(root / "oauth-secrets.json"))
            self.assertEqual(payload["signing_key_storage"], "plaintext_settings")
            self.assertEqual(payload["refresh_pepper_storage"], "plaintext_settings")
            self.assertEqual(payload["mcp_url"], "https://mcp.example.com/mcp")

    def test_http_sessions_use_isolated_active_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "marker.txt").write_text("first", encoding="utf-8")
            (second / "marker.txt").write_text("second", encoding="utf-8")
            catalog = WorkspaceCatalog([
                WorkspaceEntry("first", "First", first.resolve(), default=True),
                WorkspaceEntry("second", "Second", second.resolve()),
            ], "first")
            runtime = Runtime(first, workspace_catalog=catalog)
            first_session = runtime.create_http_session()
            second_session = runtime.create_http_session()
            runtime.set_http_session_workspace(second_session, "second")

            self.assertEqual(runtime.call_tool("read_file", {"path": "marker.txt"}, session_id=first_session)["structuredContent"]["content"], "first")
            self.assertEqual(runtime.call_tool("read_file", {"path": "marker.txt"}, session_id=second_session)["structuredContent"]["content"], "second")

    def test_workspace_switch_immediately_updates_http_session_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            catalog = WorkspaceCatalog([
                WorkspaceEntry("first", "First", first.resolve(), default=True),
                WorkspaceEntry("second", "Second", second.resolve()),
            ], "first")
            runtime = Runtime(first, workspace_catalog=catalog)
            session_id = runtime.create_http_session()
            runtime.record_mcp_http_access(
                session_id=session_id,
                method="POST",
                path="/mcp",
                rpc_method="tools/call",
                status=200,
                remote_addr="127.0.0.1",
                user_agent="same-client",
                protocol_version="2025-06-18",
            )

            runtime.set_http_session_workspace(session_id, "second")

            displayed = runtime.http_sessions_payload()[0]
            self.assertEqual(displayed["workspace_id"], "second")
            self.assertEqual(displayed["workspace"], str(second.resolve()))
            self.assertEqual(displayed["default_cwd"], str(second.resolve()))
            self.assertEqual(displayed["default_cwd_display"], ".")

    def test_agent_can_list_and_switch_its_own_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            disabled = root / "disabled"
            first.mkdir()
            second.mkdir()
            disabled.mkdir()
            (first / "marker.txt").write_text("first", encoding="utf-8")
            (second / "marker.txt").write_text("second", encoding="utf-8")
            catalog = WorkspaceCatalog([
                WorkspaceEntry("first", "First", first.resolve(), default=True),
                WorkspaceEntry("second", "Second", second.resolve()),
                WorkspaceEntry("disabled", "Disabled", disabled.resolve(), enabled=False),
            ], "first")
            runtime = Runtime(first, workspace_catalog=catalog)
            first_session = runtime.create_http_session()
            second_session = runtime.create_http_session()

            listed = runtime.call_tool("list_workspaces", {}, session_id=second_session)["structuredContent"]
            switched = runtime.call_tool(
                "select_workspace",
                {"workspace_id": "second"},
                session_id=second_session,
            )["structuredContent"]
            denied = runtime.call_tool(
                "select_workspace",
                {"workspace_id": "disabled"},
                session_id=second_session,
            )

            self.assertEqual(listed["active_workspace_id"], "first")
            self.assertEqual([item["id"] for item in listed["workspaces"]], ["first", "second"])
            self.assertEqual(switched["active_workspace_id"], "second")
            self.assertEqual(switched["workspace"]["root"], str(second.resolve()))
            self.assertTrue(denied["isError"])
            self.assertEqual(denied["structuredContent"]["error"]["code"], "INVALID_WORKSPACE")
            self.assertEqual(runtime.workspace_id_for_session(first_session), "first")
            self.assertEqual(runtime.workspace_id_for_session(second_session), "second")
            self.assertEqual(runtime.call_tool("read_file", {"path": "marker.txt"}, session_id=first_session)["structuredContent"]["content"], "first")
            self.assertEqual(runtime.call_tool("read_file", {"path": "marker.txt"}, session_id=second_session)["structuredContent"]["content"], "second")

    def test_select_workspace_requires_an_http_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))

            result = runtime.call_tool(
                "select_workspace",
                {"workspace_id": runtime.workspace_catalog.default_id},
            )

            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["error"]["code"], "SESSION_REQUIRED")

    def test_http_agent_selects_only_its_own_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "marker.txt").write_text("first", encoding="utf-8")
            (second / "marker.txt").write_text("second", encoding="utf-8")
            catalog = WorkspaceCatalog([
                WorkspaceEntry("first", "First", first.resolve(), default=True),
                WorkspaceEntry("second", "Second", second.resolve()),
            ], "first")
            runtime = Runtime(first, workspace_catalog=catalog)
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/mcp"
            try:
                with MCPClient(first, url=endpoint) as first_client, MCPClient(first, url=endpoint) as second_client:
                    listed = second_client.call_tool("list_workspaces", {})["structuredContent"]
                    switched = second_client.call_tool("select_workspace", {"workspace_id": "second"})["structuredContent"]
                    second_identity = second_client.call_tool("workspace_identity", {})["structuredContent"]
                    second_marker = second_client.call_tool("read_file", {"path": "marker.txt"})["structuredContent"]
                    first_identity = first_client.call_tool("workspace_identity", {})["structuredContent"]
                    first_marker = first_client.call_tool("read_file", {"path": "marker.txt"})["structuredContent"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(listed["active_workspace_id"], "first")
            self.assertEqual(switched["active_workspace_id"], "second")
            self.assertEqual(second_identity["workspace_id"], "second")
            self.assertEqual(second_marker["content"], "second")
            self.assertEqual(first_identity["workspace_id"], "first")
            self.assertEqual(first_marker["content"], "first")

    def test_saved_settings_response_never_echoes_secret_canary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root, settings_path=root / "server-settings.json")
            response = runtime.save_startup_settings({"oauth_token_secret": "COMPLIANCE_SHOULD_NOT_LEAK"})
            self.assertTrue(response["ok"])
            self.assertNotIn("COMPLIANCE_SHOULD_NOT_LEAK", str(response))
            self.assertTrue(response["settings"]["oauth_token_secret_configured"])
            self.assertEqual(ServerSettingsStore(root / "server-settings.json").read()["oauth_token_secret"], "COMPLIANCE_SHOULD_NOT_LEAK")

    def test_tracked_jwt_is_rejected_immediately_after_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OAuthAuthorizationStore(Path(tmp) / "oauth.sqlite3", pepper=b"p" * 32)
            store.upsert_client("agent-a", display_name="Agent A", redirect_uri="http://127.0.0.1/callback", scopes="mcp")
            grant_id = store.create_grant("agent-a", "mcp")
            store.register_signing_key("key-a", "fingerprint")
            cfg = OAuthConfig(None, None, "password", "http://127.0.0.1:8765", b"s" * 32, store=store, signing_kid="key-a")
            token = _create_oauth_token(cfg, "http://127.0.0.1:8765", client_id="agent-a", grant_id=grant_id)
            claims = _decode_oauth_token(token, cfg, "http://127.0.0.1:8765")
            self.assertIsNotNone(claims)
            store.revoke_access_token(str(claims["jti"]))
            self.assertIsNone(_decode_oauth_token(token, cfg, "http://127.0.0.1:8765"))

    def test_normal_signing_key_rotation_keeps_old_tokens_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            vault.set_secret("oauth-signing/key-old", (b"o" * 32).hex())
            store = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=b"p" * 32)
            store.upsert_client("agent-a", display_name="Agent A", redirect_uri="http://127.0.0.1/callback", scopes="mcp")
            grant_id = store.create_grant("agent-a", "mcp")
            store.register_signing_key("key-old", "old-fingerprint", secret_ref="oauth-signing/key-old")
            cfg = OAuthConfig(None, None, "password", "http://127.0.0.1:8765", b"o" * 32, store=store, signing_kid="key-old", signing_keys={"key-old": b"o" * 32}, secret_vault=vault)
            runtime = Runtime(root, oauth_config=cfg, settings_path=root / "server-settings.json")
            old_token = _create_oauth_token(runtime.oauth_config, "http://127.0.0.1:8765", client_id="agent-a", grant_id=grant_id)

            rotated = runtime.rotate_oauth_signing_key()

            self.assertEqual(rotated["status"], "active")
            self.assertIsNotNone(_decode_oauth_token(old_token, runtime.oauth_config, "http://127.0.0.1:8765"))
            self.assertEqual(store.list_signing_keys()[0]["status"], "active")

    def test_webui_refuses_new_oauth_secret_without_secret_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(
                root,
                oauth_config=OAuthConfig(None, None, "password", None, b"s" * 32),
                settings_path=root / "server-settings.json",
            )
            response = runtime.save_startup_settings({"oauth_token_secret": "COMPLIANCE_SHOULD_NOT_LEAK"})
            self.assertFalse(response["ok"])
            self.assertNotIn("COMPLIANCE_SHOULD_NOT_LEAK", str(response))

    def test_secret_vault_migrates_signing_key_without_plaintext_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "server-settings.json"
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            settings: dict[str, object] = {}
            first = _resolve_oauth_token_secret(settings, settings_path, secret_vault=vault)
            persisted = ServerSettingsStore(settings_path).read()

            self.assertNotIn("oauth_token_secret", persisted)
            self.assertTrue(persisted["oauth_active_key_secret_ref"])
            self.assertEqual(vault.get_secret(str(persisted["oauth_active_key_secret_ref"])), first.hex())
            restarted = _resolve_oauth_token_secret(persisted, settings_path, secret_vault=vault)
            self.assertEqual(restarted, first)

    def test_authorization_password_is_generated_once_and_survives_three_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "server-settings.json"
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            passwords: list[str] = []
            fingerprints: list[str] = []
            sources: list[str] = []

            with patch.dict(os.environ, {"CODING_TOOLS_MCP_OAUTH_PASSWORD": ""}, clear=False):
                for _ in range(3):
                    settings = ServerSettingsStore(settings_path).read() if settings_path.exists() else {}
                    password, source = _resolve_oauth_authorization_password(
                        settings,
                        settings_path,
                        secret_vault=vault,
                    )
                    cfg = OAuthConfig(
                        None,
                        None,
                        password,
                        None,
                        b"s" * 32,
                        secret_vault=vault,
                        authorization_password_source=source,
                        authorization_password_created_at=str(settings.get("oauth_authorization_password_created_at") or "") or None,
                    )
                    status = Runtime(root, oauth_config=cfg, startup_settings=settings).oauth_authorization_password_status()
                    passwords.append(password)
                    fingerprints.append(str(status["fingerprint"]))
                    sources.append(source)

            self.assertEqual(len(set(passwords)), 1)
            self.assertEqual(len(set(fingerprints)), 1)
            self.assertEqual(sources, ["generated", "vault", "vault"])
            persisted = ServerSettingsStore(settings_path).read()
            self.assertNotIn("oauth_password", persisted)
            self.assertEqual(
                persisted["oauth_authorization_password_secret_ref"],
                "oauth_authorization_password",
            )
            self.assertEqual(vault.get_secret("oauth_authorization_password"), passwords[0])

    def test_authorization_password_environment_overrides_vault_and_disables_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            vault.set_secret("oauth_authorization_password", "vault-value-that-is-long-enough")
            settings: dict[str, object] = {
                "oauth_authorization_password_secret_ref": "oauth_authorization_password"
            }

            with patch.dict(
                os.environ,
                {"CODING_TOOLS_MCP_OAUTH_PASSWORD": "environment-value-that-wins"},
                clear=False,
            ):
                password, source = _resolve_oauth_authorization_password(
                    settings,
                    root / "server-settings.json",
                    secret_vault=vault,
                )
            cfg = OAuthConfig(
                None,
                None,
                password,
                None,
                b"s" * 32,
                secret_vault=vault,
                authorization_password_source=source,
            )
            status = Runtime(root, oauth_config=cfg).oauth_authorization_password_status()

            self.assertEqual(password, "environment-value-that-wins")
            self.assertEqual(status["source"], "environment")
            self.assertTrue(status["managed_externally"])
            self.assertFalse(status["can_rotate"])

    def test_plaintext_authorization_password_migrates_to_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "server-settings.json"
            settings: dict[str, object] = {"oauth_password": "legacy-password-that-must-migrate"}
            ServerSettingsStore(settings_path).write(settings)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")

            with patch.dict(os.environ, {"CODING_TOOLS_MCP_OAUTH_PASSWORD": ""}, clear=False):
                password, source = _resolve_oauth_authorization_password(
                    settings,
                    settings_path,
                    secret_vault=vault,
                )
                persisted = ServerSettingsStore(settings_path).read()
                restarted, restarted_source = _resolve_oauth_authorization_password(
                    persisted,
                    settings_path,
                    secret_vault=vault,
                )

            self.assertEqual(password, "legacy-password-that-must-migrate")
            self.assertEqual(source, "legacy_settings")
            self.assertNotIn("oauth_password", persisted)
            self.assertEqual(vault.get_secret("oauth_authorization_password"), password)
            self.assertEqual((restarted, restarted_source), (password, "vault"))

    def test_authorization_password_generation_requires_enabled_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"CODING_TOOLS_MCP_OAUTH_PASSWORD": ""}, clear=False):
                with self.assertRaisesRegex(ValueError, "CODING_TOOLS_MCP_SECRETS_KEY"):
                    _resolve_oauth_authorization_password(
                        {},
                        root / "server-settings.json",
                        secret_vault=SecretVault(root / "oauth-secrets.json", None),
                    )

    def test_password_rotation_preserves_access_and_refresh_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            vault.set_secret("oauth_authorization_password", "old-authorization-password")
            store = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=b"p" * 32)
            store.upsert_client("agent-a", display_name="Agent A", redirect_uri="http://127.0.0.1/callback", scopes="mcp")
            grant_id = store.create_grant("agent-a", "mcp")
            _family_id, refresh_token = store.issue_refresh_token(grant_id, "agent-a", "mcp", expires_at=4_000_000_000)
            store.register_signing_key("key-a", "fingerprint")
            cfg = OAuthConfig(
                None,
                None,
                "old-authorization-password",
                None,
                b"s" * 32,
                store=store,
                signing_kid="key-a",
                secret_vault=vault,
                authorization_password_source="vault",
            )
            runtime = Runtime(root, oauth_config=cfg, settings_path=root / "server-settings.json")
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            access_token = _create_oauth_token(cfg, base, client_id="agent-a", grant_id=grant_id)
            try:
                rotated = runtime.rotate_oauth_authorization_password()
                mcp_request = urllib.request.Request(
                    f"{base}/mcp",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                mcp_status = urllib.request.urlopen(mcp_request, timeout=5).status
                refresh_request = urllib.request.Request(
                    f"{base}/oauth/token",
                    data=urllib.parse.urlencode({
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": "agent-a",
                    }).encode("ascii"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                refreshed = json.loads(urllib.request.urlopen(refresh_request, timeout=5).read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertNotEqual(rotated["password"], "old-authorization-password")
            self.assertEqual(mcp_status, 200)
            self.assertTrue(refreshed["access_token"])
            self.assertTrue(refreshed["refresh_token"])
            self.assertEqual(len(store.list_clients()), 1)
            self.assertEqual(len(store.list_grants()), 1)

    def test_password_rotation_immediately_rejects_old_authorize_password(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            vault.set_secret("oauth_authorization_password", "old-authorization-password")
            cfg = OAuthConfig(
                None,
                None,
                "old-authorization-password",
                None,
                b"s" * 32,
                secret_vault=vault,
                authorization_password_source="vault",
            )
            runtime = Runtime(root, oauth_config=cfg, settings_path=root / "server-settings.json")
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            new_password = runtime.rotate_oauth_authorization_password()["password"]
            opener = urllib.request.build_opener(NoRedirect)

            def authorize(password: str) -> int:
                request = urllib.request.Request(
                    f"{base}/oauth/authorize",
                    data=urllib.parse.urlencode({
                        "client_id": "agent-a",
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code_challenge": "challenge",
                        "code_challenge_method": "S256",
                        "state": "state-a",
                        "scope": "mcp",
                        "password": password,
                    }).encode("ascii"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    return opener.open(request, timeout=5).status
                except urllib.error.HTTPError as exc:
                    return exc.code

            try:
                old_status = authorize("old-authorization-password")
                new_status = authorize(str(new_password))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(old_status, 401)
            self.assertEqual(new_status, 302)

    def test_password_admin_api_returns_plaintext_once_and_status_never_does(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            cfg = OAuthConfig(
                None,
                None,
                "",
                None,
                b"s" * 32,
                secret_vault=vault,
                authorization_password_source="vault",
            )
            runtime = Runtime(
                root,
                admin_token="admin-token",
                oauth_config=cfg,
                settings_path=root / "server-settings.json",
            )
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            headers = {"Authorization": "Bearer admin-token", "Content-Type": "application/json"}
            try:
                generate_request = urllib.request.Request(
                    f"{base}/api/admin/oauth/password/generate",
                    data=b"{}",
                    headers=headers,
                    method="POST",
                )
                generated = json.loads(urllib.request.urlopen(generate_request, timeout=5).read())
                status_request = urllib.request.Request(
                    f"{base}/api/admin/oauth/password",
                    headers={"Authorization": "Bearer admin-token"},
                )
                status = json.loads(urllib.request.urlopen(status_request, timeout=5).read())
                admin_status_request = urllib.request.Request(
                    f"{base}/api/admin/status",
                    headers={"Authorization": "Bearer admin-token"},
                )
                admin_status = json.loads(urllib.request.urlopen(admin_status_request, timeout=5).read())
                settings_request = urllib.request.Request(
                    f"{base}/api/admin/settings",
                    headers={"Authorization": "Bearer admin-token"},
                )
                settings_status = json.loads(urllib.request.urlopen(settings_request, timeout=5).read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertTrue(generated["password"])
            self.assertTrue(generated["persisted"])
            self.assertNotIn("password", status)
            self.assertEqual(status["fingerprint"], generated["fingerprint"])
            self.assertNotIn(generated["password"], json.dumps(status))
            self.assertNotIn(generated["password"], json.dumps(admin_status))
            self.assertNotIn(generated["password"], json.dumps(settings_status))

    def test_password_admin_api_rejects_disabled_vault_and_environment_rotation(self) -> None:
        def post_status(runtime: Runtime, path: str) -> tuple[int, dict[str, object]]:
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}{path}",
                data=b"{}",
                headers={"Authorization": "Bearer admin-token", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                return caught.exception.code, json.loads(caught.exception.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            disabled_cfg = OAuthConfig(
                None,
                None,
                "",
                None,
                b"s" * 32,
                secret_vault=SecretVault(root / "disabled.json", None),
                authorization_password_source="vault",
            )
            disabled_status, disabled_body = post_status(
                Runtime(root, admin_token="admin-token", oauth_config=disabled_cfg),
                "/api/admin/oauth/password/generate",
            )
            environment_cfg = OAuthConfig(
                None,
                None,
                "environment-password-value",
                None,
                b"s" * 32,
                secret_vault=SecretVault(root / "enabled.json", "test-master-key"),
                authorization_password_source="environment",
            )
            environment_status, environment_body = post_status(
                Runtime(root, admin_token="admin-token", oauth_config=environment_cfg),
                "/api/admin/oauth/password/rotate",
            )

        self.assertEqual(disabled_status, 503)
        self.assertIn("CODING_TOOLS_MCP_SECRETS_KEY", str(disabled_body["error"]))
        self.assertEqual(environment_status, 409)
        self.assertIn("CODING_TOOLS_MCP_OAUTH_PASSWORD", str(environment_body["error"]))

    def test_password_and_tokens_survive_reconstructed_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            vault.set_secret("oauth_authorization_password", "restart-stable-password")
            store_a = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=b"p" * 32)
            store_a.upsert_client("agent-a", display_name="Agent A", redirect_uri="http://127.0.0.1/callback", scopes="mcp")
            grant_id = store_a.create_grant("agent-a", "mcp")
            _family_id, refresh_token = store_a.issue_refresh_token(grant_id, "agent-a", "mcp", expires_at=4_000_000_000)
            store_a.register_signing_key("key-a", "fingerprint")
            cfg_a = OAuthConfig(
                None,
                None,
                "restart-stable-password",
                "http://127.0.0.1:8765",
                b"s" * 32,
                store=store_a,
                signing_kid="key-a",
                secret_vault=vault,
                authorization_password_source="vault",
            )
            runtime_a = Runtime(root, oauth_config=cfg_a)
            access_token = _create_oauth_token(cfg_a, "http://127.0.0.1:8765", client_id="agent-a", grant_id=grant_id)
            fingerprint_a = runtime_a.oauth_authorization_password_status()["fingerprint"]

            store_b = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=b"p" * 32)
            cfg_b = OAuthConfig(
                None,
                None,
                vault.get_secret("oauth_authorization_password"),
                "http://127.0.0.1:8765",
                b"s" * 32,
                store=store_b,
                signing_kid="key-a",
                secret_vault=vault,
                authorization_password_source="vault",
            )
            runtime_b = Runtime(root, oauth_config=cfg_b)

            self.assertEqual(runtime_b.oauth_authorization_password_status()["fingerprint"], fingerprint_a)
            self.assertIsNotNone(_decode_oauth_token(access_token, cfg_b, "http://127.0.0.1:8765"))
            self.assertIsNotNone(store_b.rotate_refresh_token(refresh_token, expires_at=4_000_000_000))

    def test_password_operations_do_not_log_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = SecretVault(root / "oauth-secrets.json", "test-master-key")
            settings: dict[str, object] = {"oauth_password": "log-canary-password-value"}
            captured = io.StringIO()
            with patch.dict(os.environ, {"CODING_TOOLS_MCP_OAUTH_PASSWORD": ""}, clear=False):
                with redirect_stderr(captured):
                    password, _source = _resolve_oauth_authorization_password(
                        settings,
                        root / "server-settings.json",
                        secret_vault=vault,
                    )
            cfg = OAuthConfig(
                None,
                None,
                password,
                None,
                b"s" * 32,
                secret_vault=vault,
                authorization_password_source="vault",
            )
            with redirect_stderr(captured):
                Runtime(root, oauth_config=cfg, settings_path=root / "server-settings.json").rotate_oauth_authorization_password()

            self.assertNotIn("log-canary-password-value", captured.getvalue())
            self.assertNotIn(vault.get_secret("oauth_authorization_password"), captured.getvalue())
