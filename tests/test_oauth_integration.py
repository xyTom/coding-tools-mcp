from __future__ import annotations

import hashlib
import sqlite3
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

from coding_tools_mcp.oauth import PersistentOAuthClientRegistry
from coding_tools_mcp.oauth_store import OAuthAuthorizationStore


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


if __name__ == "__main__":
    unittest.main()
