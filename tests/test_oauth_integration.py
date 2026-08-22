from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
import sqlite3
import unittest
from contextlib import closing, contextmanager, redirect_stderr
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from coding_tools_mcp.admin import AdminService, document_revision
from coding_tools_mcp.oauth import (
    OAUTH_PASSWORD_SECRET,
    PersistentOAuthClientRegistry,
    authenticate_access_token,
    create_access_token,
    create_authorization_grant,
    oauth_signing_kid,
    validate_access_token,
)
from coding_tools_mcp.oauth_store import OAuthAuthorizationStore, OAuthStoreError
from coding_tools_mcp.secret_vault import SecretVault
from coding_tools_mcp.server import (
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    build_persistent_oauth_config,
)
from coding_tools_mcp.settings_store import ServerSettingsStore
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


PEPPER = b"phase-05-registry-pepper" * 2


@contextmanager
def oauth_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp())
    try:
        yield root
    finally:
        database = root / "oauth.sqlite3"
        if database.exists():
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()
        for attempt in range(20):
            try:
                shutil.rmtree(root)
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                retryable = os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 145}
                if not retryable or attempt == 19:
                    raise
                time.sleep(0.05)


class PersistentOAuthClientRegistryTests(unittest.TestCase):
    def test_preregistered_clients_reopen_with_exact_redirect_and_auth_method(self) -> None:
        with oauth_root() as root:
            path = root / "oauth.sqlite3"
            registry = PersistentOAuthClientRegistry(
                OAuthAuthorizationStore(path, pepper=PEPPER)
            )
            registry.add_preregistered(
                "public-agent",
                ("http://127.0.0.1/callback",),
                client_secret=None,
            )
            registry.add_preregistered(
                "confidential-agent",
                ("https://agent.example/callback",),
                client_secret="synthetic-client-secret",
            )

            reopened = PersistentOAuthClientRegistry(
                OAuthAuthorizationStore(path, pepper=PEPPER)
            )
            self.assertTrue(
                reopened.accepts_redirect(
                    "public-agent", "http://127.0.0.1/callback"
                )
            )
            self.assertFalse(
                reopened.accepts_redirect(
                    "public-agent", "http://127.0.0.1/other"
                )
            )
            self.assertTrue(reopened.authenticates("public-agent", "", "none"))
            self.assertTrue(
                reopened.authenticates(
                    "confidential-agent",
                    "synthetic-client-secret",
                    "client_secret_post",
                )
            )
            self.assertFalse(
                reopened.authenticates(
                    "confidential-agent", "wrong", "client_secret_post"
                )
            )
            record = reopened.store.get_client("confidential-agent")
            self.assertEqual(
                record["client_secret_digest"],
                hashlib.sha256(b"synthetic-client-secret").hexdigest(),
            )
            self.assertNotIn("synthetic-client-secret", str(record))


class PersistentAccessTokenTests(unittest.TestCase):
    def test_jti_kid_and_revocation_state_are_enforced(self) -> None:
        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url="https://mcp.example",
                token_ttl=86_400,
                client_id="token-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            grant_id = create_authorization_grant(
                config,
                client_id="token-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            kid = oauth_signing_kid(config)
            config.store.register_signing_key(
                kid,
                hashlib.sha256(config.token_secret).hexdigest(),
                secret_ref="oauth/token-secret",
            )
            token = create_access_token(
                config,
                "https://mcp.example",
                client_id="token-agent",
                grant_id=grant_id,
            )
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(
                token,
                config.token_secret,
                algorithms=["HS256"],
                audience="https://mcp.example",
                issuer="https://mcp.example",
            )
            self.assertEqual(header["kid"], kid)
            self.assertEqual(claims["client_id"], "token-agent")
            self.assertEqual(claims["grant_id"], grant_id)
            self.assertEqual(claims["sub"], grant_id)
            self.assertTrue(claims["jti"])
            persisted = config.store.list_access_tokens("token-agent")
            self.assertEqual(persisted[0]["jti"], claims["jti"])
            self.assertNotIn(token, str(persisted))
            identity = authenticate_access_token(token, config, "https://mcp.example")
            self.assertIsNotNone(identity)
            self.assertEqual(identity.client_id, "token-agent")
            self.assertEqual(identity.grant_id, grant_id)
            self.assertEqual(identity.workspace_id, "default")
            self.assertEqual(identity.jti, claims["jti"])
            self.assertTrue(validate_access_token(token, config, "https://mcp.example"))

            config.store.revoke_access_token(claims["jti"], reason="test")
            self.assertFalse(validate_access_token(token, config, "https://mcp.example"))

            second_grant = create_authorization_grant(
                config,
                client_id="token-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            second = create_access_token(
                config,
                "https://mcp.example",
                client_id="token-agent",
                grant_id=second_grant,
            )
            self.assertTrue(validate_access_token(second, config, "https://mcp.example"))
            config.store.revoke_grant(second_grant, reason="test")
            self.assertFalse(validate_access_token(second, config, "https://mcp.example"))

            third_grant = create_authorization_grant(
                config,
                client_id="token-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            third = create_access_token(
                config,
                "https://mcp.example",
                client_id="token-agent",
                grant_id=third_grant,
            )
            self.assertTrue(validate_access_token(third, config, "https://mcp.example"))
            config.store.set_client_enabled("token-agent", False, reason="test")
            self.assertFalse(validate_access_token(third, config, "https://mcp.example"))


class BearerFailClosedTests(unittest.TestCase):
    def test_store_failure_denies_bearer_without_logging_token(self) -> None:
        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=None,
                token_ttl=86_400,
                client_id="bearer-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            grant_id = create_authorization_grant(
                config,
                client_id="bearer-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            kid = oauth_signing_kid(config)
            config.store.register_signing_key(
                kid,
                hashlib.sha256(config.token_secret).hexdigest(),
                secret_ref="oauth/token-secret",
            )
            runtime = Runtime(root, oauth_config=config, transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda: Runtime(root, oauth_config=config, transport="http"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            token = create_access_token(
                config,
                base,
                client_id="bearer-agent",
                grant_id=grant_id,
            )

            def ping() -> int:
                request = urllib.request.Request(
                    f"{base}/mcp",
                    data=b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}',
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "MCP-Protocol-Version": "2025-06-18",
                    },
                    method="POST",
                )
                for attempt in range(3):
                    try:
                        return urllib.request.urlopen(request, timeout=5).status
                    except (ConnectionAbortedError, ConnectionResetError):
                        if attempt == 2:
                            raise
                        time.sleep(0.05 * (attempt + 1))
                raise AssertionError("unreachable HTTP retry state")

            try:
                self.assertEqual(ping(), 200)
                captured = io.StringIO()
                with patch.object(
                    config.store,
                    "active_access_token_identity",
                    side_effect=OAuthStoreError("synthetic database failure"),
                ), redirect_stderr(captured):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        ping()
                self.assertEqual(caught.exception.code, 401)
                self.assertIn("validation unavailable", captured.getvalue())
                self.assertNotIn(token, captured.getvalue())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class PersistentOAuthCompositionTests(unittest.TestCase):
    def test_dcr_authorization_selects_multiple_workspaces_and_one_initial_workspace(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                return None

        with oauth_root() as root:
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            catalog = WorkspaceCatalog(
                [
                    WorkspaceEntry(
                        "workspace-a",
                        "Workspace A",
                        workspace_a,
                        enabled=True,
                        default=True,
                    ),
                    WorkspaceEntry(
                        "workspace-b",
                        "Workspace B",
                        workspace_b,
                        enabled=True,
                    ),
                ],
                "workspace-a",
            )
            config, _created = build_persistent_oauth_config(
                root,
                master_key="dcr-workspace-selection-master-key",
                password="dcr-workspace-selection-password",
                server_url=None,
                token_ttl=86_400,
                registration_workspace_id=None,
            )
            runtime = Runtime(root, oauth_config=config, transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda: Runtime(root, oauth_config=config, transport="http"),
                workspace_catalog=catalog,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            opener = urllib.request.build_opener(NoRedirect)
            try:
                registration_request = urllib.request.Request(
                    f"{base}/oauth/register",
                    data=json.dumps(
                        {
                            "client_name": "DCR Workspace Agent",
                            "redirect_uris": ["http://127.0.0.1/callback"],
                            "grant_types": ["authorization_code", "refresh_token"],
                            "response_types": ["code"],
                            "token_endpoint_auth_method": "none",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(registration_request, timeout=5) as response:
                    registered = json.loads(response.read())
                client_id = str(registered["client_id"])

                query = urllib.parse.urlencode(
                    {
                        "response_type": "code",
                        "client_id": client_id,
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code_challenge": "A" * 43,
                        "code_challenge_method": "S256",
                        "state": "state-dcr-workspace-selection",
                        "resource": base,
                    }
                )
                with urllib.request.urlopen(
                    f"{base}/oauth/authorize?{query}", timeout=5
                ) as response:
                    login_page = response.read().decode("utf-8")
                self.assertIn("name='workspace_ids' multiple", login_page)
                self.assertIn("workspace-a", login_page)
                self.assertIn("workspace-b", login_page)

                authorization_body = urllib.parse.urlencode(
                    [
                        ("client_id", client_id),
                        ("redirect_uri", "http://127.0.0.1/callback"),
                        ("code_challenge", "A" * 43),
                        ("code_challenge_method", "S256"),
                        ("state", "state-dcr-workspace-selection"),
                        ("resource", base),
                        ("workspace_selection", "1"),
                        ("workspace_ids", "workspace-a"),
                        ("workspace_ids", "workspace-b"),
                        ("workspace_id", "workspace-b"),
                        ("password", "dcr-workspace-selection-password"),
                    ]
                ).encode("ascii")
                authorization_request = urllib.request.Request(
                    f"{base}/oauth/authorize",
                    data=authorization_body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    opener.open(authorization_request, timeout=5)
                self.assertEqual(caught.exception.code, 302)

                assert config.store is not None
                self.assertEqual(
                    config.store.get_client(client_id)["workspace_ids"],
                    ["workspace-a", "workspace-b"],
                )
                grants = config.store.list_grants(client_id)
                self.assertEqual(len(grants), 1)
                self.assertEqual(grants[0]["workspace_id"], "workspace-b")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_authorize_selects_one_workspace_from_live_client_allowlist(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                return None

        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="workspace-selection-master-key",
                password="workspace-selection-password",
                server_url=None,
                token_ttl=86_400,
                registration_workspace_id=None,
            )
            config.registry.add_preregistered(
                "multi-workspace-agent",
                ("http://127.0.0.1/callback",),
                client_secret=None,
                workspace_id="workspace-a",
            )
            assert config.store is not None
            config.store.set_client_workspaces(
                "multi-workspace-agent",
                ["workspace-a", "workspace-b"],
            )
            runtime = Runtime(root, oauth_config=config, transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda: Runtime(root, oauth_config=config, transport="http"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            opener = urllib.request.build_opener(NoRedirect)

            def authorize(workspace_id: str = "") -> tuple[int, str]:
                body = urllib.parse.urlencode(
                    {
                        "client_id": "multi-workspace-agent",
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code_challenge": "A" * 43,
                        "code_challenge_method": "S256",
                        "state": "state-workspace-selection",
                        "resource": base,
                        "workspace_id": workspace_id,
                        "password": "workspace-selection-password",
                    }
                ).encode("ascii")
                request = urllib.request.Request(
                    f"{base}/oauth/authorize",
                    data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    with opener.open(request, timeout=5) as response:
                        return response.status, response.read().decode("utf-8")
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read().decode("utf-8")

            try:
                query = urllib.parse.urlencode(
                    {
                        "response_type": "code",
                        "client_id": "multi-workspace-agent",
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code_challenge": "A" * 43,
                        "code_challenge_method": "S256",
                        "state": "state-workspace-selection",
                        "resource": base,
                    }
                )
                with urllib.request.urlopen(
                    f"{base}/oauth/authorize?{query}", timeout=5
                ) as response:
                    login_page = response.read().decode("utf-8")
                self.assertIn("name='workspace_id'", login_page)
                self.assertIn("workspace-a", login_page)
                self.assertIn("workspace-b", login_page)

                missing_status, missing_page = authorize()
                self.assertEqual(missing_status, 400)
                self.assertIn("Select a Workspace", missing_page)
                self.assertEqual(authorize("workspace-b")[0], 302)
                first_grant = config.store.list_grants("multi-workspace-agent")[0]
                self.assertEqual(first_grant["workspace_id"], "workspace-b")

                config.store.set_client_workspaces(
                    "multi-workspace-agent", ["workspace-a"]
                )
                self.assertEqual(authorize("workspace-b")[0], 400)
                self.assertEqual(authorize("workspace-a")[0], 302)
                grants = config.store.list_grants("multi-workspace-agent")
                self.assertEqual(grants[0]["workspace_id"], "workspace-a")
                self.assertEqual(grants[1]["workspace_id"], "workspace-b")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_secret_vault_http_update_rotates_live_authorize_password(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                return None

        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="initial-authorize-password",
                server_url=None,
                token_ttl=86_400,
                client_id="live-rotation-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            config.registry.add_preregistered(
                "global-fallback-agent",
                ("http://127.0.0.1/callback",),
                client_secret=None,
            )
            assert config.store is not None
            config.store.upsert_client(
                "workspace-binding-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
                workspace_id=None,
            )
            settings = ServerSettingsStore(root / "server-settings.json")
            settings.write({"workspace": str(root)})
            active_workspace_id = WorkspaceCatalog.from_settings(
                {"workspace": str(root)}, root
            ).default_id
            server_vault = SecretVault(root / "server-secrets.json", "synthetic-master-key")
            assert config.authorization_password is not None
            service = AdminService(
                settings_store=settings,
                active_settings={"workspace": str(root)},
                fallback_workspace=root,
                gateway_path=root / "mcp-servers.json",
                active_gateway_revision=document_revision({"servers": {}}),
                secret_vault=server_vault,
                oauth_store=config.store,
                oauth_secret_vault=config.secret_vault,
                oauth_password=config.authorization_password,
            )
            runtime = Runtime(root, oauth_config=config, transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda: Runtime(root, oauth_config=config, transport="http"),
                admin_service=service,
                admin_token="dedicated-admin-token",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            opener = urllib.request.build_opener(NoRedirect)

            def authorize(
                password: str,
                client_id: str = "live-rotation-agent",
            ) -> int:
                body = urllib.parse.urlencode(
                    {
                        "client_id": client_id,
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code_challenge": "A" * 43,
                        "code_challenge_method": "S256",
                        "state": "state-live-rotation",
                        "resource": base,
                        "password": password,
                    }
                ).encode("ascii")
                request = urllib.request.Request(
                    f"{base}/oauth/authorize",
                    data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    opener.open(request, timeout=5)
                except urllib.error.HTTPError as exc:
                    return exc.code
                raise AssertionError("OAuth authorize unexpectedly returned without redirect or error.")

            try:
                self.assertEqual(
                    authorize(
                        "initial-authorize-password",
                        "workspace-binding-agent",
                    ),
                    409,
                )
                workspace_client_id = urllib.parse.quote(
                    "workspace-binding-agent", safe=""
                )
                workspace_request = urllib.request.Request(
                    f"{base}/admin/api/oauth/clients/{workspace_client_id}/workspaces",
                    data=json.dumps({"workspace_ids": [active_workspace_id]}).encode(
                        "utf-8"
                    ),
                    headers={
                        "Authorization": "Bearer dedicated-admin-token",
                        "Content-Type": "application/json",
                    },
                    method="PUT",
                )
                with urllib.request.urlopen(workspace_request, timeout=5) as response:
                    workspace_binding = json.loads(response.read())
                self.assertTrue(workspace_binding["applied_immediately"])
                self.assertEqual(
                    authorize(
                        "initial-authorize-password",
                        "workspace-binding-agent",
                    ),
                    302,
                )

                self.assertEqual(authorize("initial-authorize-password"), 302)
                secret_name = urllib.parse.quote(OAUTH_PASSWORD_SECRET, safe="")
                rotate_request = urllib.request.Request(
                    f"{base}/admin/api/secrets/{secret_name}",
                    data=json.dumps({"value": "rotated-authorize-password"}).encode("utf-8"),
                    headers={
                        "Authorization": "Bearer dedicated-admin-token",
                        "Content-Type": "application/json",
                    },
                    method="PUT",
                )
                with urllib.request.urlopen(rotate_request, timeout=5) as response:
                    rotated = json.loads(response.read())
                self.assertTrue(rotated["oauth_applied_immediately"])
                self.assertEqual(authorize("initial-authorize-password"), 401)
                self.assertEqual(authorize("rotated-authorize-password"), 302)

                client_id = urllib.parse.quote("live-rotation-agent", safe="")
                client_password_request = urllib.request.Request(
                    f"{base}/admin/api/oauth/clients/{client_id}/authorization-password",
                    data=json.dumps({"value": "client-only-authorize-password"}).encode(
                        "utf-8"
                    ),
                    headers={
                        "Authorization": "Bearer dedicated-admin-token",
                        "Content-Type": "application/json",
                    },
                    method="PUT",
                )
                with urllib.request.urlopen(client_password_request, timeout=5) as response:
                    client_password = json.loads(response.read())
                self.assertEqual(
                    client_password["authorize_login"],
                    {"configured": True, "mode": "client"},
                )
                view_request = urllib.request.Request(
                    f"{base}/admin/api/oauth/clients/{client_id}/authorization-password",
                    headers={"Authorization": "Bearer dedicated-admin-token"},
                    method="GET",
                )
                with urllib.request.urlopen(view_request, timeout=5) as response:
                    viewed = json.loads(response.read())
                self.assertEqual(viewed["value"], "client-only-authorize-password")
                self.assertEqual(
                    viewed["authorize_login"],
                    {"configured": True, "mode": "client"},
                )
                self.assertEqual(authorize("rotated-authorize-password"), 401)
                self.assertEqual(authorize("client-only-authorize-password"), 302)
                self.assertEqual(
                    authorize(
                        "rotated-authorize-password",
                        "global-fallback-agent",
                    ),
                    302,
                )

                reopened_with_override, _ = build_persistent_oauth_config(
                    root,
                    master_key="synthetic-master-key",
                    password="rotated-authorize-password",
                    server_url=None,
                    token_ttl=86_400,
                    client_id="live-rotation-agent",
                    redirect_uris=("http://127.0.0.1/callback",),
                )
                self.assertEqual(
                    reopened_with_override.current_authorization_password(
                        "live-rotation-agent"
                    ),
                    "client-only-authorize-password",
                )

                reset_request = urllib.request.Request(
                    f"{base}/admin/api/oauth/clients/{client_id}/authorization-password",
                    headers={"Authorization": "Bearer dedicated-admin-token"},
                    method="DELETE",
                )
                with urllib.request.urlopen(reset_request, timeout=5) as response:
                    reset = json.loads(response.read())
                self.assertEqual(
                    reset["authorize_login"],
                    {"configured": False, "mode": "global"},
                )
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(view_request, timeout=5)
                self.assertEqual(missing.exception.code, 404)
                self.assertEqual(authorize("rotated-authorize-password"), 302)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            reopened, password_created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password=None,
                server_url=None,
                token_ttl=86_400,
                client_id="live-rotation-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            self.assertFalse(password_created)
            self.assertEqual(
                reopened.current_authorization_password(),
                "rotated-authorize-password",
            )

    def test_dcr_client_persists_across_runtime_rebuild_with_supported_grants(self) -> None:
        with oauth_root() as root:
            config, created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=None,
                token_ttl=86_400,
            )
            self.assertFalse(created)
            runtime = Runtime(root, oauth_config=config, transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda: Runtime(root, oauth_config=config, transport="http"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            body = json.dumps(
                {
                    "client_name": "Persistent DCR Agent",
                    "redirect_uris": ["http://127.0.0.1/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"{base}/oauth/register",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    registered = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(
                registered["grant_types"],
                ["authorization_code", "refresh_token"],
            )
            self.assertEqual(registered["response_types"], ["code"])
            reopened, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=None,
                token_ttl=86_400,
            )
            stored = reopened.registry.get(str(registered["client_id"]))
            self.assertIsNotNone(stored)
            self.assertEqual(stored.client_name, "Persistent DCR Agent")
            self.assertEqual(
                stored.redirect_uris,
                ("http://127.0.0.1/callback",),
            )

    def test_authorization_approval_persists_grant_before_issuing_code(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                return None

        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=None,
                token_ttl=86_400,
                client_id="grant-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            runtime = Runtime(root, oauth_config=config, transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda: Runtime(root, oauth_config=config, transport="http"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            body = urllib.parse.urlencode(
                {
                    "client_id": "grant-agent",
                    "redirect_uri": "http://127.0.0.1/callback",
                    "code_challenge": "A" * 43,
                    "code_challenge_method": "S256",
                    "state": "state-a",
                    "resource": base,
                    "password": "synthetic-authorize-password",
                }
            ).encode("ascii")
            request = urllib.request.Request(
                f"{base}/oauth/authorize",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            opener = urllib.request.build_opener(NoRedirect)
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    opener.open(request, timeout=5)
                self.assertEqual(caught.exception.code, 302)
                self.assertIn("code=", caught.exception.headers["Location"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            grants = config.store.list_grants("grant-agent")
            self.assertEqual(len(grants), 1)
            self.assertEqual(grants[0]["scopes"], "mcp")
            reopened, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=None,
                token_ttl=86_400,
                client_id="grant-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            self.assertEqual(reopened.store.list_grants("grant-agent"), grants)

    def test_persistent_oauth_config_requires_secret_vault_key(self) -> None:
        with oauth_root() as root:
            with self.assertRaisesRegex(ValueError, "SECRETS_KEY"):
                build_persistent_oauth_config(
                    root,
                    master_key=None,
                    password="synthetic-authorize-password",
                    server_url=None,
                    token_ttl=86_400,
                )


if __name__ == "__main__":
    unittest.main()
