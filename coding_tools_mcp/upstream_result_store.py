from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


RESULT_STORE_SINGLE_MAX_BYTES = 8 * 1024 * 1024
RESULT_STORE_OWNER_MAX_HANDLES = 16
RESULT_STORE_OWNER_MAX_BYTES = 16 * 1024 * 1024
RESULT_STORE_GLOBAL_MAX_BYTES = 64 * 1024 * 1024
RESULT_STORE_TTL_SECONDS = 5 * 60
RESULT_FETCH_MAX_CODEPOINTS = 32_000


class ResultNotFound(LookupError):
    """Opaque handle is absent, expired, or owned by another session."""


@dataclass(frozen=True)
class ResultPage:
    handle: str
    text: str
    offset: int
    next_offset: int
    total_codepoints: int
    eof: bool
    server_alias: str


@dataclass(frozen=True)
class _StoredResult:
    handle: str
    owner: str
    text: str
    byte_size: int
    created_at: float
    server_alias: str


class ResultStore:
    """Bounded FIFO store for original oversized Broker results."""

    def __init__(
        self,
        *,
        single_max_bytes: int = RESULT_STORE_SINGLE_MAX_BYTES,
        owner_max_handles: int = RESULT_STORE_OWNER_MAX_HANDLES,
        owner_max_bytes: int = RESULT_STORE_OWNER_MAX_BYTES,
        global_max_bytes: int = RESULT_STORE_GLOBAL_MAX_BYTES,
        ttl_seconds: float = RESULT_STORE_TTL_SECONDS,
        now: Callable[[], float] = time.monotonic,
        handle_factory: Callable[[], str] | None = None,
    ) -> None:
        self.single_max_bytes = int(single_max_bytes)
        self.owner_max_handles = int(owner_max_handles)
        self.owner_max_bytes = int(owner_max_bytes)
        self.global_max_bytes = int(global_max_bytes)
        self.ttl_seconds = float(ttl_seconds)
        self._now = now
        self._handle_factory = handle_factory or (
            lambda: f"ur_{secrets.token_urlsafe(24)}"
        )
        self._entries: OrderedDict[str, _StoredResult] = OrderedDict()
        self._owner_handles: dict[str, OrderedDict[str, None]] = {}
        self._owner_bytes: dict[str, int] = {}
        self._global_bytes = 0
        self._lock = threading.RLock()

    def store(self, text: str, *, owner: str, server_alias: str) -> str | None:
        if not owner:
            return None
        encoded_size = len(text.encode("utf-8"))
        if encoded_size > self.single_max_bytes:
            return None
        with self._lock:
            self._purge_expired_locked()
            handle = self._unique_handle_locked()
            entry = _StoredResult(
                handle=handle,
                owner=owner,
                text=text,
                byte_size=encoded_size,
                created_at=self._now(),
                server_alias=server_alias,
            )
            self._entries[handle] = entry
            owner_handles = self._owner_handles.setdefault(owner, OrderedDict())
            owner_handles[handle] = None
            self._owner_bytes[owner] = self._owner_bytes.get(owner, 0) + encoded_size
            self._global_bytes += encoded_size
            self._enforce_owner_limits_locked(owner)
            self._enforce_global_limit_locked()
            return handle if handle in self._entries else None

    def fetch(
        self,
        handle: str,
        *,
        owner: str,
        offset: int = 0,
        limit: int = RESULT_FETCH_MAX_CODEPOINTS,
    ) -> ResultPage:
        bounded_offset = max(0, int(offset))
        bounded_limit = max(1, min(int(limit), RESULT_FETCH_MAX_CODEPOINTS))
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(handle)
            if entry is None or not owner or entry.owner != owner:
                raise ResultNotFound("Result handle was not found.")
            total = len(entry.text)
            start = min(bounded_offset, total)
            end = min(total, start + bounded_limit)
            return ResultPage(
                handle=handle,
                text=entry.text[start:end],
                offset=start,
                next_offset=end,
                total_codepoints=total,
                eof=end >= total,
                server_alias=entry.server_alias,
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._owner_handles.clear()
            self._owner_bytes.clear()
            self._global_bytes = 0

    @property
    def total_bytes(self) -> int:
        with self._lock:
            self._purge_expired_locked()
            return self._global_bytes

    def owner_handle_count(self, owner: str) -> int:
        with self._lock:
            self._purge_expired_locked()
            return len(self._owner_handles.get(owner, ()))

    def owner_bytes(self, owner: str) -> int:
        with self._lock:
            self._purge_expired_locked()
            return self._owner_bytes.get(owner, 0)

    def _unique_handle_locked(self) -> str:
        for _ in range(32):
            handle = self._handle_factory()
            if handle and handle not in self._entries:
                return handle
        raise RuntimeError("Could not allocate a unique upstream result handle.")

    def _purge_expired_locked(self) -> None:
        cutoff = self._now() - self.ttl_seconds
        expired = [
            handle
            for handle, entry in self._entries.items()
            if entry.created_at <= cutoff
        ]
        for handle in expired:
            self._remove_locked(handle)

    def _enforce_owner_limits_locked(self, owner: str) -> None:
        handles = self._owner_handles.get(owner)
        while handles and (
            len(handles) > self.owner_max_handles
            or self._owner_bytes.get(owner, 0) > self.owner_max_bytes
        ):
            oldest = next(iter(handles))
            self._remove_locked(oldest)
            handles = self._owner_handles.get(owner)

    def _enforce_global_limit_locked(self) -> None:
        while self._entries and self._global_bytes > self.global_max_bytes:
            oldest = next(iter(self._entries))
            self._remove_locked(oldest)

    def _remove_locked(self, handle: str) -> None:
        entry = self._entries.pop(handle, None)
        if entry is None:
            return
        self._global_bytes = max(0, self._global_bytes - entry.byte_size)
        owner_handles = self._owner_handles.get(entry.owner)
        if owner_handles is not None:
            owner_handles.pop(handle, None)
            if not owner_handles:
                self._owner_handles.pop(entry.owner, None)
        remaining = max(0, self._owner_bytes.get(entry.owner, 0) - entry.byte_size)
        if remaining:
            self._owner_bytes[entry.owner] = remaining
        else:
            self._owner_bytes.pop(entry.owner, None)
