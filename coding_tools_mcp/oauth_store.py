"""Persistent, metadata-only OAuth authorization state.

Bearer credentials are deliberately never written to this database.  Access
tokens are identified by their JWT ``jti`` and refresh tokens are stored only
as a keyed digest so that the administration API can revoke and audit a
connection without being able to disclose a usable credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class OAuthStoreError(RuntimeError):
    """OAuth persistence cannot safely serve an authorization decision."""


class _ConnectionContext:
    """Close SQLite handles deterministically (required for Windows file locks)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False


@dataclass(frozen=True)
class RefreshTokenResult:
    family_id: str
    token: str
    client_id: str
    grant_id: str
    scopes: str


class OAuthAuthorizationStore:
    """SQLite-backed authorization state with fail-closed query helpers."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, *, pepper: bytes) -> None:
        if not pepper:
            raise ValueError("OAuth refresh-token pepper must not be empty.")
        self.path = Path(path).expanduser()
        self.pepper = bytes(pepper)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> _ConnectionContext:
        try:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            return _ConnectionContext(conn)
        except sqlite3.Error as exc:
            raise OAuthStoreError(f"OAuth authorization database is unavailable: {exc}") from exc

    def _migrate(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                current = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if current > self.SCHEMA_VERSION:
                    raise OAuthStoreError("OAuth authorization database was created by a newer server version.")
                if current == self.SCHEMA_VERSION:
                    return
                conn.executescript(
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
                    );
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
                    );
                    CREATE INDEX oauth_grants_client_idx ON oauth_grants(client_id);
                    CREATE TABLE oauth_access_tokens (
                        jti TEXT PRIMARY KEY,
                        grant_id TEXT NOT NULL REFERENCES oauth_grants(grant_id),
                        client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
                        signing_kid TEXT NOT NULL,
                        scopes TEXT NOT NULL,
                        token_mode TEXT NOT NULL DEFAULT 'standard',
                        issued_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        last_used_at REAL,
                        revoked_at REAL,
                        revoke_reason TEXT
                    );
                    CREATE INDEX oauth_access_tokens_grant_idx ON oauth_access_tokens(grant_id);
                    CREATE INDEX oauth_access_tokens_expires_idx ON oauth_access_tokens(expires_at);
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
                    );
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
                    );
                    CREATE INDEX oauth_refresh_tokens_family_idx ON oauth_refresh_tokens(family_id);
                    CREATE TABLE oauth_signing_keys (
                        kid TEXT PRIMARY KEY,
                        algorithm TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        activated_at REAL,
                        retired_at REAL,
                        revoked_at REAL
                    );
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
                    );
                    CREATE INDEX oauth_audit_events_time_idx ON oauth_audit_events(timestamp DESC);
                    """
                )
                conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise OAuthStoreError(f"OAuth authorization database migration failed: {exc}") from exc

    @staticmethod
    def validate_client_id(client_id: str) -> str:
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
            raise ValueError("OAuth client_id must contain 1-128 characters.")
        if not all(char.isalnum() or char in "-._~" for char in client_id):
            raise ValueError("OAuth client_id contains unsupported characters.")
        return client_id

    @staticmethod
    def validate_redirect_uri(value: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
            raise ValueError("OAuth redirect_uri must be an absolute http(s) URI without a fragment.")
        if parsed.username or parsed.password:
            raise ValueError("OAuth redirect_uri must not contain user credentials.")
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))

    def upsert_client(
        self,
        client_id: str,
        *,
        display_name: str | None = None,
        redirect_uri: str,
        scopes: str,
        client_type: str = "public_pkce",
    ) -> None:
        client_id = self.validate_client_id(client_id)
        redirect_uri = self.validate_redirect_uri(redirect_uri)
        now = time.time()
        with self._connect() as conn:
            existing = conn.execute("SELECT redirect_uri, enabled FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
            if existing is not None and existing["redirect_uri"] != redirect_uri:
                raise ValueError("OAuth redirect_uri does not exactly match the registered client URI.")
            if existing is None:
                conn.execute(
                    "INSERT INTO oauth_clients(client_id, display_name, client_type, redirect_uri, allowed_scopes, created_at, updated_at, first_authorized_at) VALUES(?,?,?,?,?,?,?,?)",
                    (client_id, display_name or client_id, client_type, redirect_uri, scopes, now, now, now),
                )
                self._audit(conn, "client_authorized", client_id=client_id, actor_kind="user", details={"redirect_uri": redirect_uri})
            elif not bool(existing["enabled"]):
                raise ValueError("OAuth client is disabled.")

    def create_grant(self, client_id: str, scopes: str) -> str:
        now = time.time()
        grant_id = str(uuid.uuid4())
        with self._connect() as conn:
            client = conn.execute("SELECT enabled FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
            if client is None or not bool(client["enabled"]):
                raise OAuthStoreError("OAuth client is not active.")
            conn.execute(
                "INSERT INTO oauth_grants(grant_id, client_id, scopes, created_at, updated_at) VALUES(?,?,?,?,?)",
                (grant_id, client_id, scopes, now, now),
            )
            self._audit(conn, "grant_created", client_id=client_id, grant_id=grant_id, actor_kind="user", details={"scopes": scopes})
        return grant_id

    def register_signing_key(self, kid: str, fingerprint: str, *, algorithm: str = "HS256", active: bool = True) -> None:
        now = time.time()
        with self._connect() as conn:
            if active:
                conn.execute("UPDATE oauth_signing_keys SET status='retired', retired_at=? WHERE status='active' AND kid <> ?", (now, kid))
            conn.execute(
                "INSERT INTO oauth_signing_keys(kid, algorithm, fingerprint, status, created_at, activated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(kid) DO UPDATE SET fingerprint=excluded.fingerprint, status=excluded.status, activated_at=excluded.activated_at",
                (kid, algorithm, fingerprint, "active" if active else "retired", now, now if active else None),
            )

    def signing_key_is_usable(self, kid: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM oauth_signing_keys WHERE kid = ?", (kid,)).fetchone()
        return row is not None and row["status"] in {"active", "retired"}

    def record_access_token(
        self, jti: str, grant_id: str, client_id: str, signing_kid: str, scopes: str, *, issued_at: float, expires_at: float, token_mode: str = "standard"
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO oauth_access_tokens(jti, grant_id, client_id, signing_kid, scopes, token_mode, issued_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (jti, grant_id, client_id, signing_kid, scopes, token_mode, issued_at, expires_at),
            )
            self._audit(conn, "access_token_issued", client_id=client_id, grant_id=grant_id, token_id=jti, key_id=signing_kid, actor_kind="server", details={"mode": token_mode})

    def access_token_is_active(self, jti: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._connect() as conn:
            row = conn.execute(
                """SELECT t.expires_at, t.revoked_at, c.enabled AS client_enabled, c.revoked_at AS client_revoked,
                          g.enabled AS grant_enabled, g.revoked_at AS grant_revoked, k.status AS key_status
                   FROM oauth_access_tokens t
                   JOIN oauth_clients c ON c.client_id=t.client_id
                   JOIN oauth_grants g ON g.grant_id=t.grant_id
                   LEFT JOIN oauth_signing_keys k ON k.kid=t.signing_kid
                   WHERE t.jti=?""",
                (jti,),
            ).fetchone()
            if row is None:
                return False
            active = (
                row["expires_at"] > now and row["revoked_at"] is None and bool(row["client_enabled"])
                and row["client_revoked"] is None and bool(row["grant_enabled"]) and row["grant_revoked"] is None
                and (row["key_status"] is None or row["key_status"] in {"active", "retired"})
            )
            if active:
                conn.execute("UPDATE oauth_access_tokens SET last_used_at=? WHERE jti=? AND (last_used_at IS NULL OR last_used_at < ?)", (now, jti, now - 60))
        return active

    def revoke_access_token(self, jti: str, *, reason: str = "administrator") -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT client_id, grant_id FROM oauth_access_tokens WHERE jti=?", (jti,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE oauth_access_tokens SET revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,?) WHERE jti=?", (time.time(), reason, jti))
            self._audit(conn, "access_token_revoked", client_id=row["client_id"], grant_id=row["grant_id"], token_id=jti, actor_kind="admin", details={"reason": reason})
            return True

    def revoke_grant(self, grant_id: str, *, reason: str = "administrator") -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT client_id FROM oauth_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if row is None:
                return False
            now = time.time()
            conn.execute("UPDATE oauth_grants SET enabled=0, revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,?) WHERE grant_id=?", (now, reason, grant_id))
            conn.execute("UPDATE oauth_access_tokens SET revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,?) WHERE grant_id=?", (now, reason, grant_id))
            conn.execute("UPDATE oauth_refresh_token_families SET revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,?) WHERE grant_id=?", (now, reason, grant_id))
            self._audit(conn, "grant_revoked", client_id=row["client_id"], grant_id=grant_id, actor_kind="admin", details={"reason": reason})
            return True

    def set_client_enabled(self, client_id: str, enabled: bool, *, reason: str = "administrator") -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT client_id FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
            if row is None:
                return False
            now = time.time()
            conn.execute(
                "UPDATE oauth_clients SET enabled=?, revoked_at=CASE WHEN ? THEN NULL ELSE COALESCE(revoked_at,?) END, updated_at=? WHERE client_id=?",
                (1 if enabled else 0, 1 if enabled else 0, now, now, client_id),
            )
            if not enabled:
                conn.execute("UPDATE oauth_grants SET enabled=0, revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,?) WHERE client_id=?", (now, reason, client_id))
                conn.execute("UPDATE oauth_access_tokens SET revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,?) WHERE client_id=?", (now, reason, client_id))
                conn.execute("UPDATE oauth_refresh_token_families SET revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,?) WHERE client_id=?", (now, reason, client_id))
            self._audit(conn, "client_enabled" if enabled else "client_disabled", client_id=client_id, actor_kind="admin", details={"reason": reason})
            return True

    def issue_refresh_token(self, grant_id: str, client_id: str, scopes: str, *, expires_at: float) -> tuple[str, str]:
        family_id = str(uuid.uuid4())
        token_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(48)
        now = time.time()
        with self._connect() as conn:
            conn.execute("INSERT INTO oauth_refresh_token_families(family_id, grant_id, client_id, scopes, created_at, expires_at) VALUES(?,?,?,?,?,?)", (family_id, grant_id, client_id, scopes, now, expires_at))
            conn.execute("INSERT INTO oauth_refresh_tokens(token_id, family_id, token_hash, issued_at, expires_at) VALUES(?,?,?,?,?)", (token_id, family_id, self._refresh_hash(token), now, expires_at))
            self._audit(conn, "refresh_token_issued", client_id=client_id, grant_id=grant_id, token_id=family_id, actor_kind="server", details={})
        return family_id, token

    def rotate_refresh_token(self, token: str, *, expires_at: float) -> RefreshTokenResult | None:
        now = time.time()
        digest = self._refresh_hash(token)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT t.token_id, t.family_id, t.revoked_at AS token_revoked, t.replacement_token_id,
                          f.grant_id, f.client_id, f.scopes, f.expires_at, f.revoked_at AS family_revoked,
                          g.enabled AS grant_enabled, g.revoked_at AS grant_revoked, c.enabled AS client_enabled, c.revoked_at AS client_revoked
                   FROM oauth_refresh_tokens t JOIN oauth_refresh_token_families f ON f.family_id=t.family_id
                   JOIN oauth_grants g ON g.grant_id=f.grant_id JOIN oauth_clients c ON c.client_id=f.client_id
                   WHERE t.token_hash=?""",
                (digest,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            if row["token_revoked"] is not None:
                if row["replacement_token_id"] is not None and row["family_revoked"] is None:
                    conn.execute("UPDATE oauth_refresh_token_families SET revoked_at=?, revoke_reason='refresh_token_reuse' WHERE family_id=?", (now, row["family_id"]))
                    conn.execute("UPDATE oauth_refresh_tokens SET reuse_detected_at=? WHERE token_id=?", (now, row["token_id"]))
                    self._audit(conn, "refresh_token_reuse", client_id=row["client_id"], grant_id=row["grant_id"], token_id=row["family_id"], actor_kind="server", details={"severity": "high"})
                conn.execute("COMMIT")
                return None
            valid = row["expires_at"] > now and row["family_revoked"] is None and bool(row["grant_enabled"]) and row["grant_revoked"] is None and bool(row["client_enabled"]) and row["client_revoked"] is None
            if not valid:
                conn.execute("COMMIT")
                return None
            new_token = secrets.token_urlsafe(48)
            new_id = str(uuid.uuid4())
            new_expiry = min(expires_at, float(row["expires_at"]))
            conn.execute("INSERT INTO oauth_refresh_tokens(token_id, family_id, token_hash, issued_at, expires_at) VALUES(?,?,?,?,?)", (new_id, row["family_id"], self._refresh_hash(new_token), now, new_expiry))
            conn.execute("UPDATE oauth_refresh_tokens SET used_at=?, revoked_at=?, replacement_token_id=? WHERE token_id=?", (now, now, new_id, row["token_id"]))
            conn.execute("UPDATE oauth_refresh_token_families SET last_used_at=? WHERE family_id=?", (now, row["family_id"]))
            self._audit(conn, "refresh_token_rotated", client_id=row["client_id"], grant_id=row["grant_id"], token_id=row["family_id"], actor_kind="server", details={})
            conn.execute("COMMIT")
            return RefreshTokenResult(row["family_id"], new_token, row["client_id"], row["grant_id"], row["scopes"])

    def refresh_family_is_revoked(self, family_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT revoked_at FROM oauth_refresh_token_families WHERE family_id=?", (family_id,)).fetchone()
        return row is not None and row["revoked_at"] is not None

    def revoke_refresh_family(self, family_id: str, *, reason: str = "administrator") -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT client_id, grant_id FROM oauth_refresh_token_families WHERE family_id=?", (family_id,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE oauth_refresh_token_families SET revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,?) WHERE family_id=?", (time.time(), reason, family_id))
            self._audit(conn, "refresh_family_revoked", client_id=row["client_id"], grant_id=row["grant_id"], token_id=family_id, actor_kind="admin", details={"reason": reason})
            return True

    def list_clients(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT c.*, (SELECT count(*) FROM oauth_access_tokens t WHERE t.client_id=c.client_id AND t.revoked_at IS NULL AND t.expires_at > ?) AS active_access_tokens, (SELECT count(*) FROM oauth_refresh_token_families f WHERE f.client_id=c.client_id AND f.revoked_at IS NULL AND f.expires_at > ?) AS active_refresh_families FROM oauth_clients c ORDER BY c.created_at DESC, c.client_id", (time.time(), time.time())).fetchall()
        return [dict(row) for row in rows]

    def list_grants(self, client_id: str | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM oauth_grants", ()
        if client_id:
            query += " WHERE client_id=?"
            args = (client_id,)
        query += " ORDER BY created_at DESC, grant_id"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, args).fetchall()]

    def list_access_tokens(self, client_id: str | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM oauth_access_tokens", ()
        if client_id:
            query += " WHERE client_id=?"
            args = (client_id,)
        query += " ORDER BY issued_at DESC, jti"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, args).fetchall()]

    def list_signing_keys(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM oauth_signing_keys ORDER BY created_at DESC, kid").fetchall()]

    def list_audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM oauth_audit_events ORDER BY timestamp DESC, event_id DESC LIMIT ?", (limit,)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def _refresh_hash(self, token: str) -> str:
        return hmac.new(self.pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _audit(conn: sqlite3.Connection, event_type: str, *, client_id: str | None = None, grant_id: str | None = None, token_id: str | None = None, key_id: str | None = None, actor_kind: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO oauth_audit_events(event_id, timestamp, event_type, client_id, grant_id, token_id, key_id, actor_kind, details_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), time.time(), event_type, client_id, grant_id, token_id, key_id, actor_kind, json.dumps(details, sort_keys=True, ensure_ascii=True)),
        )
