"""Ephemeral server-side sessions for the browser Admin console.

The raw Admin token is accepted only for the one-time session exchange.  The
browser then authenticates with an HttpOnly cookie whose raw session id is
never retained server-side; only a SHA-256 digest is stored in memory.  The
store is intentionally process-local, so every service restart invalidates all
browser Admin sessions.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


ADMIN_SESSION_COOKIE = "coding_tools_mcp_admin_session"
ADMIN_SESSION_IDLE_TIMEOUT_SECONDS = 45 * 60
ADMIN_SESSION_ABSOLUTE_LIFETIME_SECONDS = 8 * 60 * 60
ADMIN_SESSION_MAX_CSRF_TOKENS = 8


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class _AdminSessionRecord:
    created_at: float
    last_seen_at: float
    csrf_digests: list[str] = field(default_factory=list)


class AdminSessionStore:
    """Thread-safe, process-local Admin browser-session store."""

    def __init__(
        self,
        *,
        idle_timeout_seconds: int = ADMIN_SESSION_IDLE_TIMEOUT_SECONDS,
        absolute_lifetime_seconds: int = ADMIN_SESSION_ABSOLUTE_LIFETIME_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = _new_token,
    ) -> None:
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        if absolute_lifetime_seconds <= 0:
            raise ValueError("absolute_lifetime_seconds must be positive")
        if idle_timeout_seconds > absolute_lifetime_seconds:
            raise ValueError("idle timeout cannot exceed absolute lifetime")
        self.idle_timeout_seconds = int(idle_timeout_seconds)
        self.absolute_lifetime_seconds = int(absolute_lifetime_seconds)
        self._clock = clock
        self._token_factory = token_factory
        self._sessions: dict[str, _AdminSessionRecord] = {}
        self._lock = threading.Lock()

    def _expired(self, record: _AdminSessionRecord, now: float) -> bool:
        return (
            now - record.last_seen_at >= self.idle_timeout_seconds
            or now - record.created_at >= self.absolute_lifetime_seconds
        )

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            digest
            for digest, record in self._sessions.items()
            if self._expired(record, now)
        ]
        for digest in expired:
            self._sessions.pop(digest, None)

    def create(self) -> tuple[str, str]:
        """Create a session and its first CSRF token, returning raw browser values."""

        session_id = self._token_factory()
        csrf_token = self._token_factory()
        if not session_id or not csrf_token:
            raise RuntimeError("Admin session token generation failed")
        now = self._clock()
        session_digest = _digest(session_id)
        record = _AdminSessionRecord(
            created_at=now,
            last_seen_at=now,
            csrf_digests=[_digest(csrf_token)],
        )
        with self._lock:
            self._purge_expired_locked(now)
            self._sessions[session_digest] = record
        return session_id, csrf_token

    def authorize(
        self,
        session_id: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> bool:
        """Validate a session, optionally requiring one of its CSRF tokens."""

        if not session_id:
            return False
        session_digest = _digest(session_id)
        csrf_digest = _digest(csrf_token) if csrf_token else ""
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            record = self._sessions.get(session_digest)
            if record is None:
                return False
            if require_csrf:
                if not csrf_digest or not any(
                    secrets.compare_digest(csrf_digest, stored)
                    for stored in record.csrf_digests
                ):
                    return False
            record.last_seen_at = now
            return True

    def is_active(self, session_id: str) -> bool:
        """Check session validity without extending its idle lifetime."""

        if not session_id:
            return False
        session_digest = _digest(session_id)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            return session_digest in self._sessions

    def issue_csrf(self, session_id: str) -> str | None:
        """Issue an additional CSRF token without exposing a stored raw value."""

        if not session_id:
            return None
        session_digest = _digest(session_id)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            record = self._sessions.get(session_digest)
            if record is None:
                return None
            csrf_token = self._token_factory()
            if not csrf_token:
                raise RuntimeError("Admin CSRF token generation failed")
            record.csrf_digests.append(_digest(csrf_token))
            del record.csrf_digests[:-ADMIN_SESSION_MAX_CSRF_TOKENS]
            record.last_seen_at = now
            return csrf_token

    def revoke(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(_digest(session_id), None) is not None

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def active_count(self) -> int:
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            return len(self._sessions)


__all__ = [
    "ADMIN_SESSION_ABSOLUTE_LIFETIME_SECONDS",
    "ADMIN_SESSION_COOKIE",
    "ADMIN_SESSION_IDLE_TIMEOUT_SECONDS",
    "AdminSessionStore",
]
