"""Persistent, metadata-only OAuth authorization state.

Bearer credentials are deliberately never written to this database. Access
tokens are identified by JWT ``jti`` values and refresh tokens are stored only
as HMAC-SHA256 digests keyed by a server-side pepper.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class OAuthStoreError(RuntimeError):
    """OAuth persistence cannot safely serve an authorization decision."""


class RefreshTokenClientMismatchError(OAuthStoreError):
    """A refresh token is bound to a different authenticated client."""


@dataclass(frozen=True)
class RefreshTokenResult:
    family_id: str
    token: str
    client_id: str
    grant_id: str
    scopes: str


@dataclass(frozen=True)
class RefreshTokenBinding:
    family_id: str
    client_id: str
    grant_id: str
    scopes: str


class OAuthAuthorizationStore:
    """SQLite-backed authorization metadata with fail-closed helpers."""

    SCHEMA_VERSION = 4

    def __init__(self, path: str | Path, *, pepper: bytes) -> None:
        if not isinstance(pepper, bytes) or not pepper:
            raise ValueError("OAuth refresh-token pepper must not be empty.")
        self.path = Path(path).expanduser()
        self.pepper = bytes(pepper)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connection(self, operation: str) -> Iterator[sqlite3.Connection]:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        except sqlite3.Error as exc:
            raise OAuthStoreError(f"OAuth authorization database {operation} failed: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    @contextmanager
    def _transaction(
        self,
        operation: str,
        *,
        immediate: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        with self._connection(operation) as conn:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _migrate(self) -> None:
        with self._connection("migration setup") as conn:
            conn.execute("PRAGMA journal_mode = WAL")

        with self._transaction("migration", immediate=True) as conn:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current > self.SCHEMA_VERSION:
                raise OAuthStoreError(
                    "OAuth authorization database was created by a newer server version."
                )
            if current == 0:
                for statement in self._schema_v1_statements():
                    conn.execute(statement)
                conn.execute("PRAGMA user_version = 1")
                current = 1
            if current == 1:
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(oauth_signing_keys)").fetchall()
                }
                if "secret_ref" not in columns:
                    conn.execute("ALTER TABLE oauth_signing_keys ADD COLUMN secret_ref TEXT")
                conn.execute("PRAGMA user_version = 2")
                current = 2
            if current == 2:
                client_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(oauth_clients)").fetchall()
                }
                if "redirect_uris_json" not in client_columns:
                    conn.execute("ALTER TABLE oauth_clients ADD COLUMN redirect_uris_json TEXT")
                if "token_endpoint_auth_method" not in client_columns:
                    conn.execute(
                        "ALTER TABLE oauth_clients ADD COLUMN token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none'"
                    )
                if "client_secret_digest" not in client_columns:
                    conn.execute("ALTER TABLE oauth_clients ADD COLUMN client_secret_digest TEXT")
                rows = conn.execute(
                    "SELECT client_id, redirect_uri FROM oauth_clients WHERE redirect_uris_json IS NULL"
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "UPDATE oauth_clients SET redirect_uris_json=? WHERE client_id=?",
                        (json.dumps([row["redirect_uri"]]), row["client_id"]),
                    )
                conn.execute("PRAGMA user_version = 3")
                current = 3
            if current == 3:
                client_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(oauth_clients)").fetchall()
                }
                grant_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(oauth_grants)").fetchall()
                }
                if "workspace_id" not in client_columns:
                    conn.execute("ALTER TABLE oauth_clients ADD COLUMN workspace_id TEXT")
                if "workspace_id" not in grant_columns:
                    conn.execute("ALTER TABLE oauth_grants ADD COLUMN workspace_id TEXT")
                conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    @classmethod
    def _schema_v1_statements(cls) -> tuple[str, ...]:
        return (
            """
            CREATE TABLE oauth_clients (
                client_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                client_type TEXT NOT NULL DEFAULT 'public_pkce',
                redirect_uri TEXT NOT NULL,
                allowed_scopes TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                revoked_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                first_authorized_at REAL,
                last_seen_at REAL
            )
            """,
            """
            CREATE TABLE oauth_grants (
                grant_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
                scopes TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                revoked_at REAL,
                revoke_reason TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL
            )
            """,
            "CREATE INDEX oauth_grants_client_idx ON oauth_grants(client_id)",
            """
            CREATE TABLE oauth_signing_keys (
                kid TEXT PRIMARY KEY,
                algorithm TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                activated_at REAL,
                retired_at REAL,
                revoked_at REAL
            )
            """,
            """
            CREATE TABLE oauth_access_tokens (
                jti TEXT PRIMARY KEY,
                grant_id TEXT NOT NULL REFERENCES oauth_grants(grant_id),
                client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
                signing_kid TEXT NOT NULL REFERENCES oauth_signing_keys(kid),
                scopes TEXT NOT NULL,
                token_mode TEXT NOT NULL DEFAULT 'standard',
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                last_used_at REAL,
                revoked_at REAL,
                revoke_reason TEXT
            )
            """,
            "CREATE INDEX oauth_access_tokens_grant_idx ON oauth_access_tokens(grant_id)",
            "CREATE INDEX oauth_access_tokens_expires_idx ON oauth_access_tokens(expires_at)",
            """
            CREATE TABLE oauth_refresh_token_families (
                family_id TEXT PRIMARY KEY,
                grant_id TEXT NOT NULL REFERENCES oauth_grants(grant_id),
                client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
                scopes TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                last_used_at REAL,
                revoked_at REAL,
                revoke_reason TEXT
            )
            """,
            """
            CREATE TABLE oauth_refresh_tokens (
                token_id TEXT PRIMARY KEY,
                family_id TEXT NOT NULL REFERENCES oauth_refresh_token_families(family_id),
                token_hash TEXT NOT NULL UNIQUE,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used_at REAL,
                revoked_at REAL,
                replacement_token_id TEXT REFERENCES oauth_refresh_tokens(token_id),
                reuse_detected_at REAL
            )
            """,
            "CREATE INDEX oauth_refresh_tokens_family_idx ON oauth_refresh_tokens(family_id)",
            """
            CREATE TABLE oauth_audit_events (
                event_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                client_id TEXT,
                grant_id TEXT,
                token_id TEXT,
                key_id TEXT,
                actor_kind TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
            """,
            "CREATE INDEX oauth_audit_events_time_idx ON oauth_audit_events(timestamp DESC)",
        )

    @staticmethod
    def validate_client_id(client_id: str) -> str:
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
            raise ValueError("OAuth client_id must contain 1-128 characters.")
        if not all(char.isalnum() or char in "-._~" for char in client_id):
            raise ValueError("OAuth client_id contains unsupported characters.")
        return client_id

    @staticmethod
    def validate_workspace_id(workspace_id: str) -> str:
        if not isinstance(workspace_id, str) or not 1 <= len(workspace_id) <= 128:
            raise ValueError("Workspace id must contain 1-128 characters.")
        if not all(char.isalnum() or char in "._-" for char in workspace_id):
            raise ValueError("Workspace id contains unsupported characters.")
        return workspace_id

    @staticmethod
    def validate_redirect_uri(value: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
            raise ValueError(
                "OAuth redirect_uri must be an absolute http(s) URI without a fragment."
            )
        if parsed.username or parsed.password:
            raise ValueError("OAuth redirect_uri must not contain user credentials.")
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path or "/",
                parsed.query,
                "",
            )
        )

    def upsert_client(
        self,
        client_id: str,
        *,
        display_name: str | None = None,
        scopes: str,
        redirect_uri: str | None = None,
        redirect_uris: tuple[str, ...] | list[str] | None = None,
        client_type: str = "public_pkce",
        token_endpoint_auth_method: str = "none",
        client_secret_digest: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        client_id = self.validate_client_id(client_id)
        if workspace_id is not None:
            workspace_id = self.validate_workspace_id(workspace_id)
        if redirect_uris is not None and redirect_uri is not None:
            raise ValueError("Specify redirect_uri or redirect_uris, not both.")
        raw_redirects = list(redirect_uris) if redirect_uris is not None else [redirect_uri]
        if not 1 <= len(raw_redirects) <= 10 or any(item is None for item in raw_redirects):
            raise ValueError("OAuth clients require between 1 and 10 redirect URIs.")
        normalized_redirects = tuple(
            self.validate_redirect_uri(str(item)) for item in raw_redirects
        )
        if len(set(normalized_redirects)) != len(normalized_redirects):
            raise ValueError("OAuth redirect URIs must be unique.")
        if token_endpoint_auth_method not in {
            "none",
            "client_secret_post",
            "client_secret_basic",
        }:
            raise ValueError("Unsupported token_endpoint_auth_method.")
        if token_endpoint_auth_method == "none" and client_secret_digest is not None:
            raise ValueError("Public OAuth clients must not have a client-secret digest.")
        if token_endpoint_auth_method != "none":
            if (
                not isinstance(client_secret_digest, str)
                or len(client_secret_digest) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in client_secret_digest.lower()
                )
            ):
                raise ValueError(
                    "Confidential OAuth clients require a SHA-256 secret digest."
                )
            client_secret_digest = client_secret_digest.lower()
        redirects_json = json.dumps(list(normalized_redirects), separators=(",", ":"))
        now = time.time()
        with self._transaction("client upsert", immediate=True) as conn:
            existing = conn.execute(
                """
                SELECT redirect_uri, redirect_uris_json, enabled, workspace_id
                FROM oauth_clients WHERE client_id = ?
                """,
                (client_id,),
            ).fetchone()
            existing_redirects = None
            if existing is not None:
                raw_existing = existing["redirect_uris_json"]
                existing_redirects = tuple(
                    json.loads(raw_existing)
                    if raw_existing
                    else [existing["redirect_uri"]]
                )
            if (
                existing_redirects is not None
                and existing_redirects != normalized_redirects
            ):
                raise ValueError(
                    "OAuth redirect URIs do not exactly match the registered client URIs."
                )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO oauth_clients(
                        client_id, display_name, client_type, redirect_uri,
                        allowed_scopes, created_at, updated_at, first_authorized_at,
                        redirect_uris_json, token_endpoint_auth_method,
                        client_secret_digest, workspace_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        client_id,
                        display_name or client_id,
                        client_type,
                        normalized_redirects[0],
                        scopes,
                        now,
                        now,
                        now,
                        redirects_json,
                        token_endpoint_auth_method,
                        client_secret_digest,
                        workspace_id,
                    ),
                )
                self._audit(
                    conn,
                    "client_authorized",
                    client_id=client_id,
                    actor_kind="user",
                    details={
                        "redirect_uris": list(normalized_redirects),
                        "workspace_id": workspace_id,
                    },
                )
                return
            if not bool(existing["enabled"]):
                raise OAuthStoreError("OAuth client is disabled.")
            conn.execute(
                """
                UPDATE oauth_clients
                SET display_name=?, client_type=?, allowed_scopes=?, updated_at=?,
                    redirect_uris_json=?, token_endpoint_auth_method=?,
                    client_secret_digest=?, workspace_id=COALESCE(?, workspace_id)
                WHERE client_id=?
                """,
                (
                    display_name or client_id,
                    client_type,
                    scopes,
                    now,
                    redirects_json,
                    token_endpoint_auth_method,
                    client_secret_digest,
                    workspace_id,
                    client_id,
                ),
            )

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._connection("client query") as conn:
            row = conn.execute(
                "SELECT * FROM oauth_clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
        return self._client_payload(row) if row is not None else None

    def set_client_workspace(self, client_id: str, workspace_id: str) -> bool:
        client_id = self.validate_client_id(client_id)
        workspace_id = self.validate_workspace_id(workspace_id)
        with self._transaction("client workspace binding", immediate=True) as conn:
            row = conn.execute(
                "SELECT enabled, revoked_at, workspace_id FROM oauth_clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
            if row is None:
                return False
            if not bool(row["enabled"]) or row["revoked_at"] is not None:
                raise OAuthStoreError("OAuth client is not active.")
            if row["workspace_id"] == workspace_id:
                return True
            now = time.time()
            conn.execute(
                "UPDATE oauth_clients SET workspace_id=?, updated_at=? WHERE client_id=?",
                (workspace_id, now, client_id),
            )
            self._audit(
                conn,
                "client_workspace_bound",
                client_id=client_id,
                actor_kind="admin",
                details={"workspace_id": workspace_id},
            )
            return True

    def set_client_enabled(
        self,
        client_id: str,
        enabled: bool,
        *,
        reason: str = "administrator",
    ) -> bool:
        with self._transaction("client state update", immediate=True) as conn:
            row = conn.execute(
                "SELECT enabled, revoked_at FROM oauth_clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
            if row is None:
                return False
            if bool(row["enabled"]) is enabled:
                return True
            now = time.time()
            conn.execute(
                """
                UPDATE oauth_clients
                SET enabled=?, revoked_at=?, updated_at=?
                WHERE client_id=?
                """,
                (1 if enabled else 0, None if enabled else now, now, client_id),
            )
            if not enabled:
                conn.execute(
                    """
                    UPDATE oauth_grants
                    SET enabled=0, revoked_at=COALESCE(revoked_at,?),
                        revoke_reason=COALESCE(revoke_reason,?)
                    WHERE client_id=?
                    """,
                    (now, reason, client_id),
                )
                conn.execute(
                    """
                    UPDATE oauth_access_tokens
                    SET revoked_at=COALESCE(revoked_at,?),
                        revoke_reason=COALESCE(revoke_reason,?)
                    WHERE client_id=?
                    """,
                    (now, reason, client_id),
                )
                conn.execute(
                    """
                    UPDATE oauth_refresh_token_families
                    SET revoked_at=COALESCE(revoked_at,?),
                        revoke_reason=COALESCE(revoke_reason,?)
                    WHERE client_id=?
                    """,
                    (now, reason, client_id),
                )
            self._audit(
                conn,
                "client_enabled" if enabled else "client_disabled",
                client_id=client_id,
                actor_kind="admin",
                details={"reason": reason},
            )
            return True

    def create_grant(self, client_id: str, scopes: str) -> str:
        now = time.time()
        grant_id = str(uuid.uuid4())
        with self._transaction("grant creation", immediate=True) as conn:
            client = conn.execute(
                "SELECT enabled, revoked_at, workspace_id FROM oauth_clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
            if (
                client is None
                or not bool(client["enabled"])
                or client["revoked_at"] is not None
            ):
                raise OAuthStoreError("OAuth client is not active.")
            workspace_id = client["workspace_id"]
            if not isinstance(workspace_id, str) or not workspace_id:
                raise OAuthStoreError("OAuth client has no authorized Workspace binding.")
            conn.execute(
                """
                INSERT INTO oauth_grants(
                    grant_id, client_id, scopes, workspace_id, created_at, updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (grant_id, client_id, scopes, workspace_id, now, now),
            )
            self._audit(
                conn,
                "grant_created",
                client_id=client_id,
                grant_id=grant_id,
                actor_kind="user",
                details={"scopes": scopes, "workspace_id": workspace_id},
            )
        return grant_id

    def get_grant(self, grant_id: str) -> dict[str, Any] | None:
        with self._connection("grant query") as conn:
            row = conn.execute(
                "SELECT * FROM oauth_grants WHERE grant_id=?",
                (grant_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def revoke_grant(self, grant_id: str, *, reason: str = "administrator") -> bool:
        with self._transaction("grant revocation", immediate=True) as conn:
            row = conn.execute(
                "SELECT client_id, revoked_at FROM oauth_grants WHERE grant_id=?",
                (grant_id,),
            ).fetchone()
            if row is None:
                return False
            if row["revoked_at"] is not None:
                return True
            now = time.time()
            conn.execute(
                """
                UPDATE oauth_grants
                SET enabled=0, revoked_at=?, revoke_reason=?, updated_at=?
                WHERE grant_id=?
                """,
                (now, reason, now, grant_id),
            )
            conn.execute(
                """
                UPDATE oauth_access_tokens
                SET revoked_at=COALESCE(revoked_at,?),
                    revoke_reason=COALESCE(revoke_reason,?)
                WHERE grant_id=?
                """,
                (now, reason, grant_id),
            )
            conn.execute(
                """
                UPDATE oauth_refresh_token_families
                SET revoked_at=COALESCE(revoked_at,?),
                    revoke_reason=COALESCE(revoke_reason,?)
                WHERE grant_id=?
                """,
                (now, reason, grant_id),
            )
            self._audit(
                conn,
                "grant_revoked",
                client_id=row["client_id"],
                grant_id=grant_id,
                actor_kind="admin",
                details={"reason": reason},
            )
            return True

    def register_signing_key(
        self,
        kid: str,
        fingerprint: str,
        *,
        secret_ref: str,
        algorithm: str = "HS256",
        active: bool = True,
    ) -> None:
        if not kid or not fingerprint or not secret_ref:
            raise ValueError("Signing keys require kid, fingerprint, and secret_ref metadata.")
        now = time.time()
        with self._transaction("signing-key registration", immediate=True) as conn:
            existing = conn.execute(
                "SELECT status FROM oauth_signing_keys WHERE kid=?",
                (kid,),
            ).fetchone()
            if existing is not None and existing["status"] == "revoked":
                raise OAuthStoreError("A revoked signing key cannot be registered again.")
            if active:
                conn.execute(
                    """
                    UPDATE oauth_signing_keys
                    SET status='retired', retired_at=COALESCE(retired_at,?)
                    WHERE status='active' AND kid<>?
                    """,
                    (now, kid),
                )
            status = "active" if active else "retired"
            conn.execute(
                """
                INSERT INTO oauth_signing_keys(
                    kid, algorithm, fingerprint, status, created_at,
                    activated_at, retired_at, secret_ref
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(kid) DO UPDATE SET
                    algorithm=excluded.algorithm,
                    fingerprint=excluded.fingerprint,
                    status=excluded.status,
                    activated_at=excluded.activated_at,
                    retired_at=excluded.retired_at,
                    secret_ref=excluded.secret_ref
                """,
                (
                    kid,
                    algorithm,
                    fingerprint,
                    status,
                    now,
                    now if active else None,
                    None if active else now,
                    secret_ref,
                ),
            )
            self._audit(
                conn,
                "signing_key_registered",
                key_id=kid,
                actor_kind="admin",
                details={"status": status},
            )

    def activate_signing_key(self, kid: str) -> bool:
        with self._transaction("signing-key activation", immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM oauth_signing_keys WHERE kid=?",
                (kid,),
            ).fetchone()
            if row is None or row["status"] == "revoked":
                return False
            if row["status"] == "active":
                return True
            now = time.time()
            conn.execute(
                """
                UPDATE oauth_signing_keys
                SET status='retired', retired_at=COALESCE(retired_at,?)
                WHERE status='active' AND kid<>?
                """,
                (now, kid),
            )
            conn.execute(
                """
                UPDATE oauth_signing_keys
                SET status='active', activated_at=?, retired_at=NULL
                WHERE kid=?
                """,
                (now, kid),
            )
            self._audit(
                conn,
                "signing_key_activated",
                key_id=kid,
                actor_kind="admin",
                details={},
            )
            return True

    def retire_signing_key(self, kid: str) -> bool:
        with self._transaction("signing-key retirement", immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM oauth_signing_keys WHERE kid=?",
                (kid,),
            ).fetchone()
            if row is None or row["status"] == "revoked":
                return False
            if row["status"] == "retired":
                return True
            now = time.time()
            conn.execute(
                "UPDATE oauth_signing_keys SET status='retired', retired_at=? WHERE kid=?",
                (now, kid),
            )
            self._audit(
                conn,
                "signing_key_retired",
                key_id=kid,
                actor_kind="admin",
                details={},
            )
            return True

    def revoke_signing_key(self, kid: str) -> bool:
        with self._transaction("signing-key revocation", immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM oauth_signing_keys WHERE kid=?",
                (kid,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "revoked":
                return True
            now = time.time()
            conn.execute(
                """
                UPDATE oauth_signing_keys
                SET status='revoked', revoked_at=?
                WHERE kid=?
                """,
                (now, kid),
            )
            conn.execute(
                """
                UPDATE oauth_access_tokens
                SET revoked_at=COALESCE(revoked_at,?),
                    revoke_reason=COALESCE(revoke_reason,'signing_key_revoked')
                WHERE signing_kid=?
                """,
                (now, kid),
            )
            self._audit(
                conn,
                "signing_key_revoked",
                key_id=kid,
                actor_kind="admin",
                details={"severity": "high"},
            )
            return True

    def signing_key_is_usable(self, kid: str) -> bool:
        with self._connection("signing-key query") as conn:
            row = conn.execute(
                "SELECT status FROM oauth_signing_keys WHERE kid=?",
                (kid,),
            ).fetchone()
        return row is not None and row["status"] in {"active", "retired"}

    def record_access_token(
        self,
        jti: str,
        grant_id: str,
        client_id: str,
        signing_kid: str,
        scopes: str,
        *,
        issued_at: float,
        expires_at: float,
        token_mode: str = "standard",
    ) -> None:
        if not jti:
            raise ValueError("Access-token jti must not be empty.")
        with self._transaction("access-token recording", immediate=True) as conn:
            self._record_access_token_in_transaction(
                conn,
                jti,
                grant_id,
                client_id,
                signing_kid,
                scopes,
                issued_at=issued_at,
                expires_at=expires_at,
                token_mode=token_mode,
            )

    def _record_access_token_in_transaction(
        self,
        conn: sqlite3.Connection,
        jti: str,
        grant_id: str,
        client_id: str,
        signing_kid: str,
        scopes: str,
        *,
        issued_at: float,
        expires_at: float,
        token_mode: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO oauth_access_tokens(
                jti, grant_id, client_id, signing_kid, scopes,
                token_mode, issued_at, expires_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                jti,
                grant_id,
                client_id,
                signing_kid,
                scopes,
                token_mode,
                issued_at,
                expires_at,
            ),
        )
        self._audit(
            conn,
            "access_token_issued",
            client_id=client_id,
            grant_id=grant_id,
            token_id=jti,
            key_id=signing_kid,
            actor_kind="server",
            details={"mode": token_mode},
        )

    def active_access_token_identity(
        self,
        jti: str,
        *,
        now: float | None = None,
    ) -> dict[str, str] | None:
        checked_at = time.time() if now is None else now
        with self._transaction("access-token query") as conn:
            row = conn.execute(
                """
                SELECT t.expires_at, t.revoked_at, t.client_id, t.grant_id,
                       c.enabled AS client_enabled, c.revoked_at AS client_revoked,
                       g.enabled AS grant_enabled, g.revoked_at AS grant_revoked,
                       g.workspace_id AS workspace_id, k.status AS key_status
                FROM oauth_access_tokens t
                JOIN oauth_clients c ON c.client_id=t.client_id
                JOIN oauth_grants g ON g.grant_id=t.grant_id
                JOIN oauth_signing_keys k ON k.kid=t.signing_kid
                WHERE t.jti=?
                """,
                (jti,),
            ).fetchone()
            if row is None:
                return None
            workspace_id = row["workspace_id"]
            active = (
                row["expires_at"] > checked_at
                and row["revoked_at"] is None
                and bool(row["client_enabled"])
                and row["client_revoked"] is None
                and bool(row["grant_enabled"])
                and row["grant_revoked"] is None
                and row["key_status"] in {"active", "retired"}
                and isinstance(workspace_id, str)
                and bool(workspace_id)
            )
            if not active:
                return None
            conn.execute(
                """
                UPDATE oauth_access_tokens
                SET last_used_at=?
                WHERE jti=? AND (last_used_at IS NULL OR last_used_at < ?)
                """,
                (checked_at, jti, checked_at - 60),
            )
            return {
                "client_id": str(row["client_id"]),
                "grant_id": str(row["grant_id"]),
                "workspace_id": workspace_id,
                "jti": jti,
            }

    def access_token_is_active(self, jti: str, *, now: float | None = None) -> bool:
        return self.active_access_token_identity(jti, now=now) is not None

    def revoke_access_token(self, jti: str, *, reason: str = "administrator") -> bool:
        with self._transaction("access-token revocation", immediate=True) as conn:
            row = conn.execute(
                """
                SELECT client_id, grant_id, revoked_at
                FROM oauth_access_tokens WHERE jti=?
                """,
                (jti,),
            ).fetchone()
            if row is None:
                return False
            if row["revoked_at"] is not None:
                return True
            conn.execute(
                """
                UPDATE oauth_access_tokens
                SET revoked_at=?, revoke_reason=? WHERE jti=?
                """,
                (time.time(), reason, jti),
            )
            self._audit(
                conn,
                "access_token_revoked",
                client_id=row["client_id"],
                grant_id=row["grant_id"],
                token_id=jti,
                actor_kind="admin",
                details={"reason": reason},
            )
            return True

    def issue_refresh_token(
        self,
        grant_id: str,
        client_id: str,
        scopes: str,
        *,
        expires_at: float,
    ) -> tuple[str, str]:
        family_id = str(uuid.uuid4())
        token_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(48)
        now = time.time()
        if expires_at <= now:
            raise ValueError("Refresh-token expiry must be in the future.")
        with self._transaction("refresh-token issuance", immediate=True) as conn:
            state = conn.execute(
                """
                SELECT g.enabled AS grant_enabled, g.revoked_at AS grant_revoked,
                       c.enabled AS client_enabled, c.revoked_at AS client_revoked
                FROM oauth_grants g
                JOIN oauth_clients c ON c.client_id=g.client_id
                WHERE g.grant_id=? AND g.client_id=?
                """,
                (grant_id, client_id),
            ).fetchone()
            if (
                state is None
                or not bool(state["grant_enabled"])
                or state["grant_revoked"] is not None
                or not bool(state["client_enabled"])
                or state["client_revoked"] is not None
            ):
                raise OAuthStoreError("OAuth client or grant is not active.")
            conn.execute(
                """
                INSERT INTO oauth_refresh_token_families(
                    family_id, grant_id, client_id, scopes, created_at, expires_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (family_id, grant_id, client_id, scopes, now, expires_at),
            )
            conn.execute(
                """
                INSERT INTO oauth_refresh_tokens(
                    token_id, family_id, token_hash, issued_at, expires_at
                ) VALUES(?,?,?,?,?)
                """,
                (token_id, family_id, self._refresh_hash(token), now, expires_at),
            )
            self._audit(
                conn,
                "refresh_token_issued",
                client_id=client_id,
                grant_id=grant_id,
                token_id=family_id,
                actor_kind="server",
                details={},
            )
        return family_id, token

    def refresh_token_binding(self, token: str) -> RefreshTokenBinding | None:
        digest = self._refresh_hash(token)
        with self._connection("refresh-token binding") as conn:
            row = self._refresh_token_row(conn, digest)
            if row is None:
                return None
            return RefreshTokenBinding(
                family_id=str(row["family_id"]),
                client_id=str(row["client_id"]),
                grant_id=str(row["grant_id"]),
                scopes=str(row["scopes"]),
            )

    def rotate_refresh_token(
        self,
        token: str,
        *,
        expires_at: float,
    ) -> RefreshTokenResult | None:
        now = time.time()
        digest = self._refresh_hash(token)
        with self._transaction("refresh-token rotation", immediate=True) as conn:
            row = self._refresh_token_row(conn, digest)
            return self._rotate_refresh_token_in_transaction(
                conn,
                row,
                expires_at=expires_at,
                now=now,
            )

    def rotate_refresh_token_and_record_access_token(
        self,
        token: str,
        *,
        expected_client_id: str,
        refresh_expires_at: float,
        access_jti: str,
        access_signing_kid: str,
        access_scopes: str,
        access_issued_at: float,
        access_expires_at: float,
        token_mode: str = "standard",
    ) -> RefreshTokenResult | None:
        if not expected_client_id:
            raise ValueError("Expected OAuth client id must not be empty.")
        if not access_jti:
            raise ValueError("Access-token jti must not be empty.")
        now = time.time()
        digest = self._refresh_hash(token)
        with self._transaction("refresh-token exchange", immediate=True) as conn:
            row = self._refresh_token_row(conn, digest)
            if row is None:
                return None
            stored_client_id = str(row["client_id"])
            if not secrets.compare_digest(stored_client_id, expected_client_id):
                raise RefreshTokenClientMismatchError(
                    "Refresh token is bound to a different OAuth client."
                )
            stored_scopes = str(row["scopes"])
            if stored_scopes != access_scopes:
                raise OAuthStoreError(
                    "Access-token scopes do not match the refresh-token family."
                )
            rotated = self._rotate_refresh_token_in_transaction(
                conn,
                row,
                expires_at=refresh_expires_at,
                now=now,
            )
            if rotated is None:
                return None
            self._record_access_token_in_transaction(
                conn,
                access_jti,
                rotated.grant_id,
                rotated.client_id,
                access_signing_kid,
                rotated.scopes,
                issued_at=access_issued_at,
                expires_at=access_expires_at,
                token_mode=token_mode,
            )
            return rotated

    @staticmethod
    def _refresh_token_row(
        conn: sqlite3.Connection,
        digest: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT t.token_id, t.family_id, t.used_at,
                   t.revoked_at AS token_revoked, t.replacement_token_id,
                   t.expires_at AS token_expires_at,
                   f.grant_id, f.client_id, f.scopes,
                   f.expires_at AS family_expires_at,
                   f.revoked_at AS family_revoked,
                   g.enabled AS grant_enabled, g.revoked_at AS grant_revoked,
                   c.enabled AS client_enabled, c.revoked_at AS client_revoked
            FROM oauth_refresh_tokens t
            JOIN oauth_refresh_token_families f ON f.family_id=t.family_id
            JOIN oauth_grants g ON g.grant_id=f.grant_id
            JOIN oauth_clients c ON c.client_id=f.client_id
            WHERE t.token_hash=?
            """,
            (digest,),
        ).fetchone()

    def _rotate_refresh_token_in_transaction(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row | None,
        *,
        expires_at: float,
        now: float,
    ) -> RefreshTokenResult | None:
        if row is None:
            return None
        already_used = (
            row["used_at"] is not None
            or row["token_revoked"] is not None
            or row["replacement_token_id"] is not None
        )
        if already_used:
            if row["replacement_token_id"] is not None and row["family_revoked"] is None:
                conn.execute(
                    """
                    UPDATE oauth_refresh_token_families
                    SET revoked_at=?, revoke_reason='refresh_token_reuse'
                    WHERE family_id=?
                    """,
                    (now, row["family_id"]),
                )
                conn.execute(
                    """
                    UPDATE oauth_refresh_tokens
                    SET revoked_at=COALESCE(revoked_at,?),
                        reuse_detected_at=CASE WHEN token_id=? THEN ? ELSE reuse_detected_at END
                    WHERE family_id=?
                    """,
                    (now, row["token_id"], now, row["family_id"]),
                )
                self._audit(
                    conn,
                    "refresh_token_reuse",
                    client_id=row["client_id"],
                    grant_id=row["grant_id"],
                    token_id=row["family_id"],
                    actor_kind="server",
                    details={"severity": "high"},
                )
            return None
        valid = (
            row["token_expires_at"] > now
            and row["family_expires_at"] > now
            and row["family_revoked"] is None
            and bool(row["grant_enabled"])
            and row["grant_revoked"] is None
            and bool(row["client_enabled"])
            and row["client_revoked"] is None
        )
        if not valid:
            return None
        new_token = secrets.token_urlsafe(48)
        new_id = str(uuid.uuid4())
        new_expiry = min(expires_at, float(row["family_expires_at"]))
        if new_expiry <= now:
            return None
        conn.execute(
            """
            INSERT INTO oauth_refresh_tokens(
                token_id, family_id, token_hash, issued_at, expires_at
            ) VALUES(?,?,?,?,?)
            """,
            (
                new_id,
                row["family_id"],
                self._refresh_hash(new_token),
                now,
                new_expiry,
            ),
        )
        conn.execute(
            """
            UPDATE oauth_refresh_tokens
            SET used_at=?, revoked_at=?, replacement_token_id=?
            WHERE token_id=?
            """,
            (now, now, new_id, row["token_id"]),
        )
        conn.execute(
            "UPDATE oauth_refresh_token_families SET last_used_at=? WHERE family_id=?",
            (now, row["family_id"]),
        )
        self._audit(
            conn,
            "refresh_token_rotated",
            client_id=row["client_id"],
            grant_id=row["grant_id"],
            token_id=row["family_id"],
            actor_kind="server",
            details={},
        )
        return RefreshTokenResult(
            str(row["family_id"]),
            new_token,
            str(row["client_id"]),
            str(row["grant_id"]),
            str(row["scopes"]),
        )

    def refresh_family_is_revoked(self, family_id: str) -> bool:
        with self._connection("refresh-family query") as conn:
            row = conn.execute(
                "SELECT revoked_at FROM oauth_refresh_token_families WHERE family_id=?",
                (family_id,),
            ).fetchone()
        return row is not None and row["revoked_at"] is not None

    def revoke_refresh_family(
        self,
        family_id: str,
        *,
        reason: str = "administrator",
    ) -> bool:
        with self._transaction("refresh-family revocation", immediate=True) as conn:
            row = conn.execute(
                """
                SELECT client_id, grant_id, revoked_at
                FROM oauth_refresh_token_families WHERE family_id=?
                """,
                (family_id,),
            ).fetchone()
            if row is None:
                return False
            if row["revoked_at"] is not None:
                return True
            now = time.time()
            conn.execute(
                """
                UPDATE oauth_refresh_token_families
                SET revoked_at=?, revoke_reason=? WHERE family_id=?
                """,
                (now, reason, family_id),
            )
            conn.execute(
                """
                UPDATE oauth_refresh_tokens
                SET revoked_at=COALESCE(revoked_at,?) WHERE family_id=?
                """,
                (now, family_id),
            )
            self._audit(
                conn,
                "refresh_family_revoked",
                client_id=row["client_id"],
                grant_id=row["grant_id"],
                token_id=family_id,
                actor_kind="admin",
                details={"reason": reason},
            )
            return True

    def list_clients(self) -> list[dict[str, Any]]:
        with self._connection("client listing") as conn:
            rows = conn.execute(
                "SELECT * FROM oauth_clients ORDER BY created_at DESC, client_id"
            ).fetchall()
        return [self._client_payload(row) for row in rows]

    @staticmethod
    def _client_payload(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        raw_redirects = item.pop("redirect_uris_json", None)
        item["redirect_uris"] = (
            json.loads(raw_redirects) if raw_redirects else [item["redirect_uri"]]
        )
        return item

    def list_grants(self, client_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM oauth_grants"
        args: tuple[Any, ...] = ()
        if client_id:
            query += " WHERE client_id=?"
            args = (client_id,)
        query += " ORDER BY created_at DESC, grant_id"
        with self._connection("grant listing") as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def list_access_tokens(self, client_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM oauth_access_tokens"
        args: tuple[Any, ...] = ()
        if client_id:
            query += " WHERE client_id=?"
            args = (client_id,)
        query += " ORDER BY issued_at DESC, jti"
        with self._connection("access-token listing") as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def list_refresh_token_families(
        self,
        client_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM oauth_refresh_token_families"
        args: tuple[Any, ...] = ()
        if client_id:
            query += " WHERE client_id=?"
            args = (client_id,)
        query += " ORDER BY created_at DESC, family_id"
        with self._connection("refresh-family listing") as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def list_signing_keys(self) -> list[dict[str, Any]]:
        with self._connection("signing-key listing") as conn:
            rows = conn.execute(
                "SELECT * FROM oauth_signing_keys ORDER BY created_at DESC, kid"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._connection("audit listing") as conn:
            rows = conn.execute(
                """
                SELECT * FROM oauth_audit_events
                ORDER BY timestamp DESC, event_id DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def _refresh_hash(self, token: str) -> str:
        if not isinstance(token, str) or not token:
            raise ValueError("Refresh token must be a non-empty string.")
        return hmac.new(self.pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        event_type: str,
        *,
        client_id: str | None = None,
        grant_id: str | None = None,
        token_id: str | None = None,
        key_id: str | None = None,
        actor_kind: str,
        details: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO oauth_audit_events(
                event_id, timestamp, event_type, client_id, grant_id,
                token_id, key_id, actor_kind, details_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                time.time(),
                event_type,
                client_id,
                grant_id,
                token_id,
                key_id,
                actor_kind,
                json.dumps(details, sort_keys=True, ensure_ascii=True),
            ),
        )
