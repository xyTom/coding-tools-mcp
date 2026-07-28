from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_tools_mcp.oauth_store import OAuthAuthorizationStore
from coding_tools_mcp.server import OAuthConfig, Runtime, _create_oauth_token, _decode_oauth_token, _resolve_oauth_token_secret
from coding_tools_mcp.settings_store import ServerSettingsStore
from coding_tools_mcp.secret_vault import SecretVault
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


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
