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

from coding_tools_mcp.oauth import (
    PersistentOAuthClientRegistry,
    authenticate_access_token,
    create_access_token,
    create_authorization_grant,
    oauth_signing_kid,
    validate_access_token,
)
from coding_tools_mcp.oauth_store import OAuthAuthorizationStore, OAuthStoreError
from coding_tools_mcp.server import (
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    build_persistent_oauth_config,
)


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
                return urllib.request.urlopen(request, timeout=5).status

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
