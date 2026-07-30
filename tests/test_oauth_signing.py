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


class SigningKeyLifecycleTests(unittest.TestCase):
    def test_rotation_reopen_and_emergency_revoke_preserve_key_boundaries(self) -> None:
        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=ISSUER,
                token_ttl=86_400,
                client_id="signing-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            grant_id = create_authorization_grant(
                config,
                client_id="signing-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            old_kid = str(config.signing_kid)
            old_secret = config.signing_keys[old_kid]
            old_token = create_access_token(
                config,
                ISSUER,
                client_id="signing-agent",
                grant_id=grant_id,
            )

            rotated = rotate_signing_key(config)
            new_kid = str(rotated.signing_kid)
            new_secret = rotated.signing_keys[new_kid]
            self.assertNotEqual(new_kid, old_kid)
            new_token = create_access_token(
                rotated,
                ISSUER,
                client_id="signing-agent",
                grant_id=grant_id,
            )
            self.assertTrue(validate_access_token(old_token, rotated, ISSUER))
            self.assertTrue(validate_access_token(new_token, rotated, ISSUER))
            states = {
                item["kid"]: item["status"]
                for item in rotated.store.list_signing_keys()
            }
            self.assertEqual(states[old_kid], "retired")
            self.assertEqual(states[new_kid], "active")

            reopened, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=ISSUER,
                token_ttl=86_400,
                client_id="signing-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            self.assertEqual(reopened.signing_kid, new_kid)
            self.assertEqual(set(reopened.signing_keys), {old_kid, new_kid})
            self.assertTrue(validate_access_token(old_token, reopened, ISSUER))
            self.assertTrue(validate_access_token(new_token, reopened, ISSUER))

            self.assertTrue(revoke_signing_key(reopened, old_kid))
            self.assertFalse(validate_access_token(old_token, reopened, ISSUER))
            self.assertTrue(validate_access_token(new_token, reopened, ISSUER))
            refs = {
                item["kid"]: item["secret_ref"]
                for item in reopened.store.list_signing_keys()
            }
            self.assertTrue(refs[old_kid].startswith("oauth/signing/"))
            self.assertTrue(refs[new_kid].startswith("oauth/signing/"))
            self.assertNotIn(old_secret.hex(), str(reopened.store.list_signing_keys()))
            self.assertNotIn(new_secret.hex(), str(reopened.store.list_signing_keys()))

            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()
            database_bytes = (root / "oauth.sqlite3").read_bytes()
            self.assertNotIn(old_secret, database_bytes)
            self.assertNotIn(new_secret, database_bytes)


if __name__ == "__main__":
    unittest.main()
