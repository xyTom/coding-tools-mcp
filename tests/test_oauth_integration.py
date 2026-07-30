from __future__ import annotations

import hashlib
import json
import threading
import urllib.request
import sqlite3
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

from coding_tools_mcp.oauth import PersistentOAuthClientRegistry
from coding_tools_mcp.oauth_store import OAuthAuthorizationStore
from coding_tools_mcp.server import (
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    build_persistent_oauth_config,
)


PEPPER = b"phase-05-registry-pepper" * 2


@contextmanager
def oauth_root() -> Iterator[Path]:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            yield root
        finally:
            database = root / "oauth.sqlite3"
            if database.exists():
                with closing(sqlite3.connect(database)) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("PRAGMA journal_mode=DELETE")
                    conn.commit()


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


class PersistentOAuthCompositionTests(unittest.TestCase):
    def test_dcr_client_persists_across_runtime_rebuild_without_refresh_advertising(self) -> None:
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

            self.assertEqual(registered["grant_types"], ["authorization_code"])
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
