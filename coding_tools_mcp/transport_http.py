from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


MAX_HTTP_SESSIONS = 128
HTTP_SESSION_TTL_SECONDS = 60 * 60
MAX_HTTP_SESSIONS_PER_IDENTITY = 64
MAX_HTTP_SESSION_INITIALIZATIONS = 16
HTTP_SESSION_INITIALIZATION_WAIT_SECONDS = 5.0


class HTTPSessionAdmissionError(RuntimeError):
    """Structured admission failure safe to expose through the HTTP adapter."""

    def __init__(self, code: str, message: str, *, retry_after_seconds: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = max(1, int(retry_after_seconds))


@dataclass(frozen=True)
class HTTPSessionLimits:
    max_total: int = MAX_HTTP_SESSIONS
    max_per_identity: int = MAX_HTTP_SESSIONS_PER_IDENTITY
    idle_ttl_seconds: float = HTTP_SESSION_TTL_SECONDS
    max_initializations: int = MAX_HTTP_SESSION_INITIALIZATIONS

    def __post_init__(self) -> None:
        if not isinstance(self.max_total, int) or isinstance(self.max_total, bool) or self.max_total < 1:
            raise ValueError("max_http_sessions_total must be a positive integer")
        if (
            not isinstance(self.max_per_identity, int)
            or isinstance(self.max_per_identity, bool)
            or self.max_per_identity < 1
        ):
            raise ValueError("max_http_sessions_per_identity must be a positive integer")
        if self.max_per_identity > self.max_total:
            raise ValueError("max_http_sessions_per_identity cannot exceed max_http_sessions_total")
        if (
            not isinstance(self.max_initializations, int)
            or isinstance(self.max_initializations, bool)
            or self.max_initializations < 1
        ):
            raise ValueError("max_http_session_initializations must be a positive integer")
        if self.max_initializations > self.max_total:
            raise ValueError("max_http_session_initializations cannot exceed max_http_sessions_total")
        if (
            not isinstance(self.idle_ttl_seconds, (int, float))
            or isinstance(self.idle_ttl_seconds, bool)
            or self.idle_ttl_seconds <= 0
        ):
            raise ValueError("http_session_idle_ttl_seconds must be positive")


@dataclass
class HTTPSessionRecord:
    runtime: Any
    quota_key: Hashable
    created_at: float
    last_seen: float
    active_request_leases: int = 0
    closing: bool = False
    generation: int = 0


def _close_runtime(runtime: Any) -> None:
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


def _default_quota_key(context: Any) -> Hashable:
    """Use verified authorization context fields without retaining bearer material."""

    method = getattr(context, "method", None)
    identity = getattr(context, "oauth_identity", None)
    if isinstance(method, str):
        client_id = getattr(identity, "client_id", None) if identity is not None else None
        grant_id = getattr(identity, "grant_id", None) if identity is not None else None
        return (method, client_id, grant_id)
    return "anonymous"


class HTTPSessionManager:
    """Own independent Runtime instances for Streamable HTTP sessions.

    Session lookup is O(1). Idle pruning walks only the oldest portion of an LRU
    index and never performs Runtime I/O while holding the manager lock.
    """

    def __init__(
        self,
        factory: Callable[[Any], Any],
        *,
        limits: HTTPSessionLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        quota_key_resolver: Callable[[Any], Hashable] = _default_quota_key,
        initialization_wait_seconds: float = HTTP_SESSION_INITIALIZATION_WAIT_SECONDS,
    ) -> None:
        if (
            not isinstance(initialization_wait_seconds, (int, float))
            or isinstance(initialization_wait_seconds, bool)
            or initialization_wait_seconds < 0
        ):
            raise ValueError("HTTP session initialization wait must be non-negative")
        self._factory = factory
        self.limits = limits or HTTPSessionLimits()
        self._clock = clock
        self._quota_key_resolver = quota_key_resolver
        self._initialization_wait_seconds = float(initialization_wait_seconds)
        self._sessions: dict[str, HTTPSessionRecord] = {}
        self._idle_order: OrderedDict[str, None] = OrderedDict()
        self._condition = threading.Condition(threading.Lock())
        self._creating = 0
        self._creating_by_identity: dict[Hashable, int] = {}
        self._sessions_by_identity: dict[Hashable, int] = {}
        self._closed = False
        self._shutdown_complete = False
        self._pending_closes = 0
        self._created_total = 0
        self._deleted_total = 0
        self._expired_total = 0
        self._capacity_rejected_total = 0
        self._identity_quota_rejected_total = 0
        self._initialization_rejected_total = 0
        self._waiting_initializations = 0
        self._initialization_waited_total = 0
        self._reclaimed_total = 0

    def create(self, context: Any) -> Any:
        self.prune_expired()
        quota_key = self._quota_key_resolver(context)
        wait_deadline = time.monotonic() + self._initialization_wait_seconds
        waiting = False
        try:
            while True:
                reclaimed: HTTPSessionRecord | None = None
                with self._condition:
                    if self._closed:
                        raise HTTPSessionAdmissionError(
                            "http_session_server_closing",
                            "HTTP session manager is closing",
                        )
                    identity_count = self._sessions_by_identity.get(quota_key, 0)
                    identity_creating = self._creating_by_identity.get(quota_key, 0)
                    total_full = len(self._sessions) >= self.limits.max_total
                    identity_full = identity_count >= self.limits.max_per_identity
                    if total_full or identity_full:
                        reclaimed = self._detach_oldest_idle_locked(quota_key)
                        if reclaimed is None:
                            if identity_full:
                                self._identity_quota_rejected_total += 1
                                raise HTTPSessionAdmissionError(
                                    "http_session_identity_quota",
                                    "maximum HTTP session count reached for this identity",
                                )
                            self._capacity_rejected_total += 1
                            raise HTTPSessionAdmissionError(
                                "http_session_capacity",
                                "maximum HTTP session count reached",
                            )
                    else:
                        initialization_full = self._creating >= self.limits.max_initializations
                        projected_total_full = (
                            len(self._sessions) + self._creating >= self.limits.max_total
                        )
                        projected_identity_full = (
                            identity_count + identity_creating >= self.limits.max_per_identity
                        )
                        if not (
                            initialization_full
                            or projected_total_full
                            or projected_identity_full
                        ):
                            self._creating += 1
                            self._creating_by_identity[quota_key] = identity_creating + 1
                            break
                        remaining = wait_deadline - time.monotonic()
                        if remaining <= 0:
                            if initialization_full:
                                self._initialization_rejected_total += 1
                                raise HTTPSessionAdmissionError(
                                    "http_session_initialization_limit",
                                    "maximum concurrent HTTP session initialization count reached",
                                )
                            if projected_identity_full:
                                self._identity_quota_rejected_total += 1
                                raise HTTPSessionAdmissionError(
                                    "http_session_identity_quota",
                                    "maximum HTTP session count reached for this identity",
                                )
                            self._capacity_rejected_total += 1
                            raise HTTPSessionAdmissionError(
                                "http_session_capacity",
                                "maximum HTTP session count reached",
                            )
                        if not waiting:
                            waiting = True
                            self._waiting_initializations += 1
                            self._initialization_waited_total += 1
                        self._condition.wait(timeout=remaining)
                        continue
                if reclaimed is not None:
                    self._close_detached_record(reclaimed)
        finally:
            if waiting:
                with self._condition:
                    self._waiting_initializations = max(0, self._waiting_initializations - 1)
                    self._condition.notify_all()

        runtime: Any | None = None
        installed = False
        try:
            runtime = self._factory(context)
            session_id = getattr(runtime, "http_session_id", None)
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError("Runtime did not provide a valid HTTP session identifier")
            now = self._clock()
            record = HTTPSessionRecord(
                runtime=runtime,
                quota_key=quota_key,
                created_at=now,
                last_seen=now,
            )
            with self._condition:
                if self._closed:
                    raise HTTPSessionAdmissionError(
                        "http_session_server_closing",
                        "HTTP session manager is closing",
                    )
                if session_id in self._sessions:
                    raise RuntimeError("duplicate HTTP session identifier")
                self._sessions[session_id] = record
                self._idle_order[session_id] = None
                self._sessions_by_identity[quota_key] = self._sessions_by_identity.get(quota_key, 0) + 1
                self._created_total += 1
                installed = True
            return runtime
        finally:
            try:
                if runtime is not None and not installed:
                    _close_runtime(runtime)
            finally:
                with self._condition:
                    self._creating -= 1
                    remaining = self._creating_by_identity.get(quota_key, 0) - 1
                    if remaining > 0:
                        self._creating_by_identity[quota_key] = remaining
                    else:
                        self._creating_by_identity.pop(quota_key, None)
                    self._condition.notify_all()

    def create_recovered(self, context: Any, session_id: str) -> Any:
        """Create a Runtime while retaining a client-provided transport header."""

        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 4096:
            raise ValueError("Recovered HTTP session identifier is invalid.")
        # Reuse admission accounting, then install under the retained opaque ID.
        runtime = self.create(context)
        old_id = getattr(runtime, "http_session_id", None)
        record = None
        with self._condition:
            if self._closed or session_id in self._sessions:
                installed = False
            else:
                record = self._sessions.pop(old_id, None)
                installed = record is not None
                if installed:
                    self._idle_order.pop(old_id, None)
                    self._idle_order[session_id] = None
                    self._sessions[session_id] = record
                    runtime.http_session_id = session_id
        if not installed:
            if old_id is not None:
                self.delete(old_id)
            raise RuntimeError("Recovered HTTP session identifier is unavailable.")
        return runtime

    def _detach_oldest_idle_locked(
        self,
        quota_key: Hashable,
    ) -> HTTPSessionRecord | None:
        for session_id in tuple(self._idle_order):
            record = self._sessions.get(session_id)
            if (
                record is None
                or record.closing
                or record.active_request_leases
                or record.quota_key != quota_key
            ):
                continue
            record.closing = True
            self._idle_order.pop(session_id, None)
            self._sessions.pop(session_id, None)
            self._decrement_identity_locked(record.quota_key)
            self._pending_closes += 1
            self._reclaimed_total += 1
            return record
        return None

    @contextmanager
    def lease(self, session_id: str) -> Iterator[Any | None]:
        self.prune_expired()
        record: HTTPSessionRecord | None = None
        with self._condition:
            if not self._closed:
                record = self._sessions.get(session_id)
                if record is not None and not record.closing:
                    record.active_request_leases += 1
                    record.last_seen = self._clock()
                    record.generation += 1
                    self._idle_order.move_to_end(session_id)
                else:
                    record = None
        try:
            yield record.runtime if record is not None else None
        finally:
            if record is not None:
                with self._condition:
                    record.active_request_leases = max(0, record.active_request_leases - 1)
                    record.last_seen = self._clock()
                    record.generation += 1
                    if not record.closing and session_id in self._idle_order:
                        self._idle_order.move_to_end(session_id)
                    if record.active_request_leases == 0:
                        self._condition.notify_all()

    def get(self, session_id: str) -> Any | None:
        """Compatibility lookup; HTTP handlers must use :meth:`lease`."""

        self.prune_expired()
        with self._condition:
            if self._closed:
                return None
            record = self._sessions.get(session_id)
            if record is None or record.closing:
                return None
            record.last_seen = self._clock()
            record.generation += 1
            self._idle_order.move_to_end(session_id)
        return record.runtime

    def contains(self, session_id: str) -> bool:
        """Return whether a live transport ID exists without touching its LRU state."""

        with self._condition:
            return (
                not self._closed
                and session_id in self._sessions
                and not self._sessions[session_id].closing
            )

    def delete(self, session_id: str) -> bool:
        record = self._detach_for_close(session_id)
        if record is None:
            return False
        self._close_detached_record(record)
        with self._condition:
            self._deleted_total += 1
        return True

    def _detach_for_close(self, session_id: str) -> HTTPSessionRecord | None:
        with self._condition:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            record.closing = True
            self._idle_order.pop(session_id, None)
            while record.active_request_leases:
                self._condition.wait()
            current = self._sessions.get(session_id)
            if current is not record:
                return None
            self._sessions.pop(session_id, None)
            self._decrement_identity_locked(record.quota_key)
            self._pending_closes += 1
            return record

    def prune(self) -> None:
        self.prune_expired()

    def prune_expired(self, now: float | None = None) -> int:
        current_time = self._clock() if now is None else now
        cutoff = current_time - float(self.limits.idle_ttl_seconds)
        records: list[HTTPSessionRecord] = []
        with self._condition:
            while self._idle_order:
                session_id = next(iter(self._idle_order))
                record = self._sessions.get(session_id)
                if record is None:
                    self._idle_order.popitem(last=False)
                    continue
                if record.last_seen >= cutoff:
                    break
                if record.active_request_leases:
                    record.last_seen = current_time
                    record.generation += 1
                    self._idle_order.move_to_end(session_id)
                    continue
                record.closing = True
                self._idle_order.pop(session_id, None)
                self._sessions.pop(session_id, None)
                self._decrement_identity_locked(record.quota_key)
                records.append(record)
            self._pending_closes += len(records)
            self._expired_total += len(records)
        first_error: BaseException | None = None
        for record in records:
            try:
                self._close_detached_record(record)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        return len(records)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._condition:
            active_sessions = sum(
                1 for record in self._sessions.values() if record.active_request_leases > 0
            )
            closing_sessions = sum(1 for record in self._sessions.values() if record.closing)
            idle_sessions = max(0, len(self._sessions) - active_sessions - closing_sessions)
            oldest_idle_seconds = 0.0
            for session_id in self._idle_order:
                record = self._sessions.get(session_id)
                if record is not None and not record.closing and record.active_request_leases == 0:
                    oldest_idle_seconds = max(0.0, now - record.last_seen)
                    break
            return {
                "active_sessions": active_sessions,
                "idle_sessions": idle_sessions,
                "creating_sessions": self._creating,
                "waiting_initializations": self._waiting_initializations,
                "closing_sessions": closing_sessions,
                "capacity_total": self.limits.max_total,
                "capacity_per_identity": self.limits.max_per_identity,
                "initialization_capacity": self.limits.max_initializations,
                "idle_ttl_seconds": self.limits.idle_ttl_seconds,
                "oldest_idle_seconds": oldest_idle_seconds,
                "created_total": self._created_total,
                "deleted_total": self._deleted_total,
                "expired_total": self._expired_total,
                "capacity_rejected_total": self._capacity_rejected_total,
                "identity_quota_rejected_total": self._identity_quota_rejected_total,
                "initialization_rejected_total": self._initialization_rejected_total,
                "initialization_waited_total": self._initialization_waited_total,
                "reclaimed_total": self._reclaimed_total,
                "closed": self._closed,
            }

    def close(self) -> None:
        records: list[HTTPSessionRecord]
        with self._condition:
            if self._closed:
                while not self._shutdown_complete:
                    self._condition.wait()
                return
            self._closed = True
            for record in self._sessions.values():
                record.closing = True
            self._idle_order.clear()
            while self._creating or any(record.active_request_leases for record in self._sessions.values()):
                self._condition.wait()
            records = list(self._sessions.values())
            self._sessions.clear()
            self._sessions_by_identity.clear()
            self._pending_closes += len(records)
        first_error: BaseException | None = None
        for record in records:
            try:
                self._close_detached_record(record)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            with self._condition:
                while self._pending_closes:
                    self._condition.wait()
        finally:
            with self._condition:
                self._shutdown_complete = True
                self._condition.notify_all()
        if first_error is not None:
            raise first_error

    def _close_detached_record(self, record: HTTPSessionRecord) -> None:
        try:
            _close_runtime(record.runtime)
        finally:
            with self._condition:
                self._pending_closes -= 1
                self._condition.notify_all()

    def _decrement_identity_locked(self, quota_key: Hashable) -> None:
        remaining = self._sessions_by_identity.get(quota_key, 0) - 1
        if remaining > 0:
            self._sessions_by_identity[quota_key] = remaining
        else:
            self._sessions_by_identity.pop(quota_key, None)
