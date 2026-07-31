from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


MAX_HTTP_SESSIONS = 128
HTTP_SESSION_TTL_SECONDS = 60 * 60


def _close_runtime(runtime: Any) -> None:
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


@dataclass
class HTTPSessionRecord:
    runtime: Any
    last_seen: float


class HTTPSessionManager:
    """Own independent Runtime instances for Streamable HTTP sessions."""

    def __init__(self, factory: Callable[[Any], Any]) -> None:
        self._factory = factory
        self._sessions: dict[str, HTTPSessionRecord] = {}
        self._lock = threading.Lock()
        self._creating = 0
        self._closed = False

    def create(self, context: Any) -> Any:
        self.prune()
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP session manager is closed")
            if len(self._sessions) + self._creating >= MAX_HTTP_SESSIONS:
                raise RuntimeError("maximum HTTP session count reached")
            self._creating += 1
        runtime: Any | None = None
        installed = False
        try:
            runtime = self._factory(context)
            record = HTTPSessionRecord(runtime=runtime, last_seen=time.time())
            with self._lock:
                if self._closed:
                    raise RuntimeError("HTTP session manager is closed")
                if runtime.http_session_id in self._sessions:
                    raise RuntimeError("duplicate HTTP session identifier")
                self._sessions[runtime.http_session_id] = record
                installed = True
            return runtime
        finally:
            with self._lock:
                self._creating -= 1
            if runtime is not None and not installed:
                _close_runtime(runtime)

    def get(self, session_id: str) -> Any | None:
        self.prune()
        with self._lock:
            if self._closed:
                return None
            record = self._sessions.get(session_id)
            if record is None:
                return None
            record.last_seen = time.time()
            return record.runtime

    def delete(self, session_id: str) -> bool:
        with self._lock:
            record = self._sessions.pop(session_id, None)
        if record is None:
            return False
        _close_runtime(record.runtime)
        return True

    def prune(self) -> None:
        cutoff = time.time() - HTTP_SESSION_TTL_SECONDS
        with self._lock:
            expired = [session_id for session_id, record in self._sessions.items() if record.last_seen < cutoff]
            records = [self._sessions.pop(session_id) for session_id in expired]
        for record in records:
            _close_runtime(record.runtime)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            records = list(self._sessions.values())
            self._sessions.clear()
        for record in records:
            _close_runtime(record.runtime)
