from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from coding_tools_mcp.oauth import (
    create_access_token,
    create_authorization_grant,
    exchange_refresh_token,
    issue_refresh_token,
    revoke_signing_key,
    rotate_signing_key,
    validate_access_token,
)
from coding_tools_mcp.server import build_persistent_oauth_config


ISSUER = "https://mcp.example"


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


class OAuthPersistenceComplianceTests(unittest.TestCase):
    def test_restart_refresh_rotation_and_key_revocation_fail_closed(self) -> None:
        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-compliance-master-key",
                password="synthetic-authorize-password",
                server_url=ISSUER,
                token_ttl=86_400,
                client_id="compliance-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            grant_id = create_authorization_grant(
                config,
                client_id="compliance-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            old_kid = str(config.signing_kid)
            old_secret = config.signing_keys[old_kid]
            old_access = create_access_token(
                config,
                ISSUER,
                client_id="compliance-agent",
                grant_id=grant_id,
            )
            refresh_token = issue_refresh_token(
                config,
                grant_id=grant_id,
                client_id="compliance-agent",
                scopes="mcp",
            )
            rotated = rotate_signing_key(config)
            new_kid = str(rotated.signing_kid)
            new_secret = rotated.signing_keys[new_kid]

            reopened, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-compliance-master-key",
                password="synthetic-authorize-password",
                server_url=ISSUER,
                token_ttl=86_400,
                client_id="compliance-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            self.assertEqual(reopened.signing_kid, new_kid)
            self.assertTrue(validate_access_token(old_access, reopened, ISSUER))
            exchanged = exchange_refresh_token(
                reopened,
                refresh_token=refresh_token,
                client_id="compliance-agent",
                client_secret="",
                auth_method="none",
                server_url=ISSUER,
            )
            self.assertTrue(
                validate_access_token(
                    str(exchanged["access_token"]),
                    reopened,
                    ISSUER,
                )
            )
            self.assertNotEqual(exchanged["refresh_token"], refresh_token)

            self.assertTrue(revoke_signing_key(reopened, old_kid))
            self.assertFalse(validate_access_token(old_access, reopened, ISSUER))
            self.assertTrue(
                validate_access_token(
                    str(exchanged["access_token"]),
                    reopened,
                    ISSUER,
                )
            )

            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()
            database_bytes = (root / "oauth.sqlite3").read_bytes()
            self.assertNotIn(refresh_token.encode("utf-8"), database_bytes)
            self.assertNotIn(old_secret, database_bytes)
            self.assertNotIn(new_secret, database_bytes)


if __name__ == "__main__":
    unittest.main()
