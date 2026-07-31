from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import sqlite3
import tempfile
import time
import threading
import unittest
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.oauth_store import (
    OAuthAuthorizationStore,
    OAuthStoreError,
    RefreshTokenClientMismatchError,
    RefreshTokenResult,
)


PEPPER = b"phase-04-test-pepper" * 2
FUTURE = 4_000_000_000.0


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


def prepared_store(
    root: Path,
    *,
    store_type: type[OAuthAuthorizationStore] = OAuthAuthorizationStore,
) -> tuple[OAuthAuthorizationStore, str]:
    store = store_type(root / "oauth.sqlite3", pepper=PEPPER)
    store.upsert_client(
        "agent-a",
        display_name="Agent A",
        redirect_uri="http://127.0.0.1/callback",
        scopes="mcp",
        workspace_id="workspace-a",
    )
    store.register_signing_key(
        "key-a",
        "fingerprint-a",
        secret_ref="oauth-signing/key-a",
    )
    grant_id = store.create_grant("agent-a", "mcp")
    return store, grant_id


class OAuthStoreTests(unittest.TestCase):
    def test_schema_contains_every_required_metadata_table_and_reopens(self) -> None:
        expected = {
            "oauth_clients",
            "oauth_grants",
            "oauth_access_tokens",
            "oauth_refresh_token_families",
            "oauth_refresh_tokens",
            "oauth_signing_keys",
            "oauth_audit_events",
        }
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            store.record_access_token(
                "jti-a",
                grant_id,
                "agent-a",
                "key-a",
                "mcp",
                issued_at=1,
                expires_at=FUTURE,
            )
            family_id, _token = store.issue_refresh_token(
                grant_id,
                "agent-a",
                "mcp",
                expires_at=FUTURE,
            )

            reopened = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=PEPPER)
            self.assertEqual(reopened.get_client("agent-a")["display_name"], "Agent A")
            self.assertEqual(reopened.get_grant(grant_id)["client_id"], "agent-a")
            self.assertEqual(reopened.list_access_tokens()[0]["jti"], "jti-a")
            self.assertEqual(
                reopened.list_refresh_token_families()[0]["family_id"],
                family_id,
            )
            self.assertEqual(reopened.list_signing_keys()[0]["secret_ref"], "oauth-signing/key-a")
            self.assertTrue(reopened.list_audit_events())

            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'oauth_%'"
                    )
                }
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(tables, expected)
            self.assertEqual(version, OAuthAuthorizationStore.SCHEMA_VERSION)

    def test_v1_migration_is_repeatable_and_preserves_existing_rows(self) -> None:
        with oauth_root() as root:
            path = root / "oauth.sqlite3"
            with closing(sqlite3.connect(path, isolation_level=None)) as conn:
                conn.execute("BEGIN")
                for statement in OAuthAuthorizationStore._schema_v1_statements():
                    conn.execute(statement)
                conn.execute(
                    """
                    INSERT INTO oauth_clients(
                        client_id, display_name, redirect_uri, allowed_scopes,
                        created_at, updated_at
                    ) VALUES('legacy-agent','Legacy Agent','http://127.0.0.1/callback','mcp',1,1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO oauth_signing_keys(
                        kid, algorithm, fingerprint, status, created_at, activated_at
                    ) VALUES('legacy-key','HS256','legacy-fingerprint','active',1,1)
                    """
                )
                conn.execute("PRAGMA user_version = 1")
                conn.commit()

            first = OAuthAuthorizationStore(path, pepper=PEPPER)
            second = OAuthAuthorizationStore(path, pepper=PEPPER)
            self.assertEqual(first.list_signing_keys()[0]["kid"], "legacy-key")
            self.assertIsNone(first.list_signing_keys()[0]["secret_ref"])
            self.assertEqual(second.list_signing_keys(), first.list_signing_keys())
            legacy_client = first.get_client("legacy-agent")
            self.assertEqual(
                legacy_client["redirect_uris"],
                ["http://127.0.0.1/callback"],
            )
            self.assertEqual(legacy_client["token_endpoint_auth_method"], "none")
            self.assertIsNone(legacy_client["client_secret_digest"])
            with closing(sqlite3.connect(path)) as conn:
                key_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(oauth_signing_keys)")
                }
                client_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(oauth_clients)")
                }
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertIn("secret_ref", key_columns)
            self.assertIn("redirect_uris_json", client_columns)
            self.assertIn("token_endpoint_auth_method", client_columns)
            self.assertIn("client_secret_digest", client_columns)
            self.assertEqual(version, OAuthAuthorizationStore.SCHEMA_VERSION)

    def test_workspace_binding_migration_is_explicit_and_grants_freeze_it(self) -> None:
        with oauth_root() as root:
            path = root / "oauth.sqlite3"
            store = OAuthAuthorizationStore(path, pepper=PEPPER)
            store.upsert_client(
                "workspace-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            with self.assertRaisesRegex(OAuthStoreError, "no authorized Workspace binding"):
                store.create_grant("workspace-agent", "mcp")
            self.assertTrue(store.set_client_workspace("workspace-agent", "workspace-a"))
            grant_id = store.create_grant("workspace-agent", "mcp")
            self.assertEqual(store.get_client("workspace-agent")["workspace_id"], "workspace-a")
            self.assertEqual(store.get_grant(grant_id)["workspace_id"], "workspace-a")

            self.assertTrue(store.set_client_workspace("workspace-agent", "workspace-b"))
            second_grant = store.create_grant("workspace-agent", "mcp")
            self.assertEqual(store.get_grant(grant_id)["workspace_id"], "workspace-a")
            self.assertEqual(store.get_grant(second_grant)["workspace_id"], "workspace-b")
            with closing(sqlite3.connect(path)) as conn:
                client_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(oauth_clients)")
                }
                grant_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(oauth_grants)")
                }
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertIn("workspace_id", client_columns)
            self.assertIn("workspace_id", grant_columns)
            self.assertEqual(version, OAuthAuthorizationStore.SCHEMA_VERSION)

    def test_confidential_client_metadata_round_trips_without_plaintext_secret(self) -> None:
        with oauth_root() as root:
            store = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=PEPPER)
            raw_secret = "client-secret-must-not-be-stored"
            digest = hashlib.sha256(raw_secret.encode()).hexdigest()
            redirects = (
                "https://agent.example/callback",
                "http://127.0.0.1/callback",
            )
            store.upsert_client(
                "confidential-agent",
                display_name="Confidential Agent",
                scopes="mcp",
                redirect_uris=redirects,
                client_type="confidential",
                token_endpoint_auth_method="client_secret_basic",
                client_secret_digest=digest,
            )
            reopened = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=PEPPER)
            client = reopened.get_client("confidential-agent")
            self.assertEqual(client["redirect_uris"], list(redirects))
            self.assertEqual(
                client["token_endpoint_auth_method"],
                "client_secret_basic",
            )
            self.assertEqual(client["client_secret_digest"], digest)
            self.assertNotIn(raw_secret, str(client))
            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()
            persisted = b"".join(
                path.read_bytes()
                for path in root.iterdir()
                if path.name.startswith("oauth.sqlite3")
            )
            self.assertNotIn(raw_secret.encode(), persisted)

    def test_failed_migration_rolls_back_all_schema_changes(self) -> None:
        class BrokenMigrationStore(OAuthAuthorizationStore):
            @classmethod
            def _schema_v1_statements(cls) -> tuple[str, ...]:
                valid = super()._schema_v1_statements()
                return (valid[0], "CREATE TABLE broken syntax", *valid[1:])

        with oauth_root() as root:
            path = root / "oauth.sqlite3"
            with self.assertRaises(OAuthStoreError):
                BrokenMigrationStore(path, pepper=PEPPER)
            with closing(sqlite3.connect(path)) as conn:
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'oauth_%'"
                ).fetchall()
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(tables, [])
            self.assertEqual(version, 0)

    def test_client_disable_and_grant_revoke_are_idempotent(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            self.assertTrue(store.revoke_grant(grant_id, reason="test"))
            first_grant = store.get_grant(grant_id)
            self.assertTrue(store.revoke_grant(grant_id, reason="different"))
            second_grant = store.get_grant(grant_id)
            self.assertEqual(first_grant["revoked_at"], second_grant["revoked_at"])
            self.assertEqual(second_grant["revoke_reason"], "test")

            self.assertTrue(store.set_client_enabled("agent-a", False, reason="test"))
            first_client = store.get_client("agent-a")
            self.assertTrue(store.set_client_enabled("agent-a", False, reason="different"))
            second_client = store.get_client("agent-a")
            self.assertEqual(first_client["revoked_at"], second_client["revoked_at"])
            self.assertFalse(bool(second_client["enabled"]))
            self.assertFalse(store.set_client_enabled("missing", False))

    def test_access_token_and_signing_key_lifecycle_fail_closed(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            store.record_access_token(
                "jti-a",
                grant_id,
                "agent-a",
                "key-a",
                "mcp",
                issued_at=1,
                expires_at=FUTURE,
            )
            self.assertTrue(store.access_token_is_active("jti-a", now=2))

            store.register_signing_key(
                "key-b",
                "fingerprint-b",
                secret_ref="oauth-signing/key-b",
            )
            states = {item["kid"]: item["status"] for item in store.list_signing_keys()}
            self.assertEqual(states, {"key-a": "retired", "key-b": "active"})
            self.assertTrue(store.access_token_is_active("jti-a", now=2))
            self.assertTrue(store.activate_signing_key("key-a"))
            self.assertTrue(store.retire_signing_key("key-a"))
            self.assertTrue(store.revoke_signing_key("key-a"))
            first = next(item for item in store.list_signing_keys() if item["kid"] == "key-a")
            self.assertTrue(store.revoke_signing_key("key-a"))
            second = next(item for item in store.list_signing_keys() if item["kid"] == "key-a")
            self.assertEqual(first["revoked_at"], second["revoked_at"])
            self.assertFalse(store.access_token_is_active("jti-a", now=2))

    def test_access_token_revocation_is_idempotent(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            store.record_access_token(
                "jti-a",
                grant_id,
                "agent-a",
                "key-a",
                "mcp",
                issued_at=1,
                expires_at=FUTURE,
            )
            self.assertTrue(store.revoke_access_token("jti-a", reason="test"))
            first = store.list_access_tokens()[0]
            self.assertTrue(store.revoke_access_token("jti-a", reason="different"))
            second = store.list_access_tokens()[0]
            self.assertEqual(first["revoked_at"], second["revoked_at"])
            self.assertEqual(second["revoke_reason"], "test")
            revoked_events = [
                item
                for item in store.list_audit_events(limit=500)
                if item["event_type"] == "access_token_revoked"
            ]
            self.assertEqual(len(revoked_events), 1)

    def test_refresh_token_is_stored_only_as_peppered_digest(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            family_id, token = store.issue_refresh_token(
                grant_id,
                "agent-a",
                "mcp",
                expires_at=FUTURE,
            )
            expected = hmac.new(PEPPER, token.encode(), hashlib.sha256).hexdigest()
            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                row = conn.execute(
                    "SELECT token_hash FROM oauth_refresh_tokens WHERE family_id=?",
                    (family_id,),
                ).fetchone()
                refresh_columns = {
                    item[1] for item in conn.execute("PRAGMA table_info(oauth_refresh_tokens)")
                }
                access_columns = {
                    item[1] for item in conn.execute("PRAGMA table_info(oauth_access_tokens)")
                }
            self.assertEqual(row[0], expected)
            self.assertNotIn("token", refresh_columns)
            self.assertNotIn("access_token", access_columns)
            self.assertNotIn(token, str(store.list_audit_events(limit=500)))
            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()

            persisted = b"".join(
                path.read_bytes()
                for path in root.iterdir()
                if path.name.startswith("oauth.sqlite3")
            )
            self.assertNotIn(token.encode(), persisted)
            self.assertNotIn("fingerprint-a".encode() + b"-secret-material", persisted)

    def test_refresh_rotation_detects_reuse_and_revokes_family(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            family_id, token = store.issue_refresh_token(
                grant_id,
                "agent-a",
                "mcp",
                expires_at=FUTURE,
            )
            replacement = store.rotate_refresh_token(token, expires_at=FUTURE)
            self.assertIsInstance(replacement, RefreshTokenResult)
            self.assertFalse(store.refresh_family_is_revoked(family_id))

            self.assertIsNone(store.rotate_refresh_token(token, expires_at=FUTURE))
            self.assertTrue(store.refresh_family_is_revoked(family_id))
            self.assertIsNone(
                store.rotate_refresh_token(replacement.token, expires_at=FUTURE)
            )
            reuse_events = [
                item
                for item in store.list_audit_events(limit=500)
                if item["event_type"] == "refresh_token_reuse"
            ]
            self.assertEqual(len(reuse_events), 1)

    def test_expired_refresh_token_is_rejected_even_when_family_is_valid(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            with patch("coding_tools_mcp.oauth_store.time.time", return_value=100.0):
                _family_id, token = store.issue_refresh_token(
                    grant_id,
                    "agent-a",
                    "mcp",
                    expires_at=200.0,
                )
                replacement = store.rotate_refresh_token(token, expires_at=150.0)
            self.assertIsNotNone(replacement)

            with patch("coding_tools_mcp.oauth_store.time.time", return_value=151.0):
                self.assertIsNone(
                    store.rotate_refresh_token(
                        replacement.token,
                        expires_at=4_000_000_000.0,
                    )
                )

    def test_refresh_family_revocation_is_idempotent(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            family_id, _token = store.issue_refresh_token(
                grant_id,
                "agent-a",
                "mcp",
                expires_at=FUTURE,
            )
            self.assertTrue(store.revoke_refresh_family(family_id, reason="test"))
            first = store.list_refresh_token_families()[0]
            self.assertTrue(store.revoke_refresh_family(family_id, reason="different"))
            second = store.list_refresh_token_families()[0]
            self.assertEqual(first["revoked_at"], second["revoked_at"])
            self.assertEqual(second["revoke_reason"], "test")

    def test_rotation_failure_rolls_back_replacement_and_old_token_state(self) -> None:
        class FailingRotationStore(OAuthAuthorizationStore):
            @staticmethod
            def _audit(
                conn: sqlite3.Connection,
                event_type: str,
                **kwargs: object,
            ) -> None:
                if event_type == "refresh_token_rotated":
                    raise sqlite3.IntegrityError("injected rotation audit failure")
                OAuthAuthorizationStore._audit(conn, event_type, **kwargs)  # type: ignore[arg-type]

        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            family_id, token = store.issue_refresh_token(
                grant_id,
                "agent-a",
                "mcp",
                expires_at=FUTURE,
            )
            failing = FailingRotationStore(root / "oauth.sqlite3", pepper=PEPPER)
            with self.assertRaises(OAuthStoreError):
                failing.rotate_refresh_token(token, expires_at=FUTURE)

            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                rows = conn.execute(
                    """
                    SELECT used_at, revoked_at, replacement_token_id
                    FROM oauth_refresh_tokens WHERE family_id=?
                    """,
                    (family_id,),
                ).fetchall()
            self.assertEqual(rows, [(None, None, None)])
            recovered = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=PEPPER)
            self.assertIsNotNone(recovered.rotate_refresh_token(token, expires_at=FUTURE))

    def test_refresh_exchange_access_failure_rolls_back_rotation_and_metadata(self) -> None:
        class FailingAccessAuditStore(OAuthAuthorizationStore):
            @staticmethod
            def _audit(
                conn: sqlite3.Connection,
                event_type: str,
                **kwargs: object,
            ) -> None:
                if event_type == "access_token_issued":
                    raise sqlite3.IntegrityError("injected access-token audit failure")
                OAuthAuthorizationStore._audit(conn, event_type, **kwargs)  # type: ignore[arg-type]

        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            family_id, token = store.issue_refresh_token(
                grant_id,
                "agent-a",
                "mcp",
                expires_at=FUTURE,
            )
            failing = FailingAccessAuditStore(root / "oauth.sqlite3", pepper=PEPPER)
            binding = failing.refresh_token_binding(token)
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.client_id, "agent-a")
            self.assertEqual(binding.grant_id, grant_id)

            with self.assertRaises(OAuthStoreError):
                failing.rotate_refresh_token_and_record_access_token(
                    token,
                    expected_client_id="agent-a",
                    refresh_expires_at=FUTURE,
                    access_jti="jti-atomic-failure",
                    access_signing_kid="key-a",
                    access_scopes="mcp",
                    access_issued_at=100.0,
                    access_expires_at=FUTURE,
                )

            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                refresh_rows = conn.execute(
                    """
                    SELECT used_at, revoked_at, replacement_token_id
                    FROM oauth_refresh_tokens WHERE family_id=?
                    """,
                    (family_id,),
                ).fetchall()
                access_count = conn.execute(
                    "SELECT COUNT(*) FROM oauth_access_tokens WHERE jti=?",
                    ("jti-atomic-failure",),
                ).fetchone()[0]
            self.assertEqual(refresh_rows, [(None, None, None)])
            self.assertEqual(access_count, 0)
            self.assertFalse(
                any(
                    event["event_type"] in {"refresh_token_rotated", "access_token_issued"}
                    for event in failing.list_audit_events(limit=500)
                )
            )

            recovered = OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=PEPPER)
            rotated = recovered.rotate_refresh_token_and_record_access_token(
                token,
                expected_client_id="agent-a",
                refresh_expires_at=FUTURE,
                access_jti="jti-atomic-success",
                access_signing_kid="key-a",
                access_scopes="mcp",
                access_issued_at=100.0,
                access_expires_at=FUTURE,
            )
            self.assertIsNotNone(rotated)
            self.assertTrue(recovered.access_token_is_active("jti-atomic-success", now=101.0))

    def test_refresh_exchange_client_mismatch_does_not_consume_token(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            family_id, token = store.issue_refresh_token(
                grant_id,
                "agent-a",
                "mcp",
                expires_at=FUTURE,
            )

            with self.assertRaises(RefreshTokenClientMismatchError):
                store.rotate_refresh_token_and_record_access_token(
                    token,
                    expected_client_id="agent-b",
                    refresh_expires_at=FUTURE,
                    access_jti="jti-mismatch",
                    access_signing_kid="key-a",
                    access_scopes="mcp",
                    access_issued_at=100.0,
                    access_expires_at=FUTURE,
                )

            with closing(sqlite3.connect(root / "oauth.sqlite3")) as conn:
                row = conn.execute(
                    """
                    SELECT used_at, revoked_at, replacement_token_id
                    FROM oauth_refresh_tokens WHERE family_id=?
                    """,
                    (family_id,),
                ).fetchone()
                access_count = conn.execute(
                    "SELECT COUNT(*) FROM oauth_access_tokens WHERE jti=?",
                    ("jti-mismatch",),
                ).fetchone()[0]
            self.assertEqual(row, (None, None, None))
            self.assertEqual(access_count, 0)

            rotated = store.rotate_refresh_token_and_record_access_token(
                token,
                expected_client_id="agent-a",
                refresh_expires_at=FUTURE,
                access_jti="jti-after-mismatch",
                access_signing_kid="key-a",
                access_scopes="mcp",
                access_issued_at=100.0,
                access_expires_at=FUTURE,
            )
            self.assertIsNotNone(rotated)
            self.assertTrue(store.access_token_is_active("jti-after-mismatch", now=101.0))

    def test_concurrent_refresh_rotation_has_one_winner_and_detects_reuse(self) -> None:
        with oauth_root() as root:
            store, grant_id = prepared_store(root)
            family_id, token = store.issue_refresh_token(
                grant_id,
                "agent-a",
                "mcp",
                expires_at=FUTURE,
            )
            stores = [
                OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=PEPPER),
                OAuthAuthorizationStore(root / "oauth.sqlite3", pepper=PEPPER),
            ]
            barrier = threading.Barrier(2)
            results: list[RefreshTokenResult | None] = []
            errors: list[BaseException] = []

            def rotate(candidate: OAuthAuthorizationStore) -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(candidate.rotate_refresh_token(token, expires_at=FUTURE))
                except BaseException as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=rotate, args=(candidate,)) for candidate in stores]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertEqual(sum(item is not None for item in results), 1)
            self.assertEqual(sum(item is None for item in results), 1)
            self.assertTrue(store.refresh_family_is_revoked(family_id))


if __name__ == "__main__":
    unittest.main()
