"""Process-local browser sessions for the ordinary Operator WebUI."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field


OPERATOR_SESSION_COOKIE = "coding_tools_mcp_operator_session"
OPERATOR_SESSION_IDLE_TIMEOUT_SECONDS = 45 * 60
OPERATOR_SESSION_ABSOLUTE_LIFETIME_SECONDS = 8 * 60 * 60
OPERATOR_SESSION_MAX_CSRF_TOKENS = 8


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OperatorSessionIdentity:
    principal_id: str
    workspace_ids: tuple[str, ...]


@dataclass
class _Record:
    identity: OperatorSessionIdentity
    created_at: float
    last_seen_at: float
    csrf_digests: list[str] = field(default_factory=list)


class OperatorSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, _Record] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _expired(record: _Record, now: float) -> bool:
        return (
            now - record.last_seen_at >= OPERATOR_SESSION_IDLE_TIMEOUT_SECONDS
            or now - record.created_at >= OPERATOR_SESSION_ABSOLUTE_LIFETIME_SECONDS
        )

    def _purge(self, now: float) -> None:
        for key, record in list(self._sessions.items()):
            if self._expired(record, now):
                self._sessions.pop(key, None)

    def create(self, principal_id: str, workspace_ids: tuple[str, ...]) -> tuple[str, str]:
        if not principal_id or not workspace_ids:
            raise ValueError("Operator session requires a scoped principal")
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = time.monotonic()
        record = _Record(
            OperatorSessionIdentity(principal_id, tuple(workspace_ids)),
            now,
            now,
            [_digest(csrf_token)],
        )
        with self._lock:
            self._purge(now)
            self._sessions[_digest(session_id)] = record
        return session_id, csrf_token

    def authorize(
        self,
        session_id: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> OperatorSessionIdentity | None:
        if not session_id:
            return None
        now = time.monotonic()
        csrf_digest = _digest(csrf_token) if csrf_token else ""
        with self._lock:
            self._purge(now)
            record = self._sessions.get(_digest(session_id))
            if record is None:
                return None
            if require_csrf and not any(
                secrets.compare_digest(csrf_digest, stored) for stored in record.csrf_digests
            ):
                return None
            record.last_seen_at = now
            return record.identity

    def issue_csrf(self, session_id: str) -> str | None:
        if not session_id:
            return None
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            record = self._sessions.get(_digest(session_id))
            if record is None:
                return None
            token = secrets.token_urlsafe(32)
            record.csrf_digests.append(_digest(token))
            del record.csrf_digests[:-OPERATOR_SESSION_MAX_CSRF_TOKENS]
            record.last_seen_at = now
            return token

    def revoke(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(_digest(session_id), None) is not None

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

