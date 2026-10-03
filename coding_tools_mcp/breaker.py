"""Bounded recent-failure history for nonblocking tool advice.

The legacy RepeatFailureBreaker name and threshold-query API are retained for
Python callers, but Runtime no longer uses this history to refuse execution.
Matching arguments and past errors cannot prove that external state is unchanged.
Counts are shared by one runtime, per tool/argument fingerprint and error code;
they describe recent server observations, not a client's consecutive attempts.
Success, workspace lifecycle invalidation, and expiration clear stale advice.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

# Start giving advice after two matching failures; this is not an execution cap.
REPEAT_FAILURE_LIMIT = 2
# Retained for historical telemetry and compatibility, not emitted by Runtime.
REPEATED_CALL_BLOCKED = "REPEATED_CALL_BLOCKED"
BREAKER_CAPACITY = 256
# Expire diagnostic history after sixty seconds without a counted failure.
BREAKER_TTL_SECONDS = 60.0
# Arguments that name the call rather than the work. A model that varies only
# these has not changed anything the failure depended on.
FINGERPRINT_IGNORED_ARGUMENTS = frozenset({"idempotency_key"})
# A fresh idempotency key is a legitimate repair, and internal failures say
# nothing reliable about the request; neither should produce repeat advice.
BREAKER_EXCLUDED_ERROR_CODES = frozenset({"IDEMPOTENCY_KEY_REUSED", "INTERNAL_ERROR"})
# These usually need changed arguments or refreshed state, so repeated errors
# merit advice even when marked retryable. Other retryable failures, such as
# PATCH_CONFLICT and COMMAND_LIMIT_REACHED, do not contribute to advice.
RETRY_MEANS_CHANGING_THE_CALL = frozenset(
    {
        "PATCH_CONTEXT_NOT_FOUND",
        "PATCH_CONTEXT_AMBIGUOUS",
        "REVISION_MISMATCH",
        "REVISION_REQUIRED",
    }
)


def argument_fingerprint(arguments: Mapping[str, Any]) -> str:
    """A stable short hash of one call's arguments.

    Serialized with sorted keys so key order never makes two identical calls
    look different, and truncated because the fingerprint only ever has to
    distinguish calls within one runtime.
    """

    normalized = {
        key: value for key, value in sorted(arguments.items()) if key not in FINGERPRINT_IGNORED_ARGUMENTS
    }
    try:
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):
        encoded = repr(sorted(normalized.items()))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


class RepeatFailureBreaker:
    """Bounded LRU of ``(tool, fingerprint) -> {error_code: recent count}``.

    Each entry also remembers when it last failed; an entry older than
    ``ttl_seconds`` (measured on ``clock``, monotonic by default) is dropped
    instead of advising.
    """

    def __init__(
        self,
        *,
        limit: int = REPEAT_FAILURE_LIMIT,
        capacity: int = BREAKER_CAPACITY,
        ttl_seconds: float = BREAKER_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize bounded failure tracking with a history TTL and an injectable clock."""
        self._limit = limit
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], dict[str, int]] = OrderedDict()
        self._last_failure: dict[tuple[str, str], float] = {}
        self._generation = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def generation(self) -> int:
        """Return the current workspace-verdict generation."""

        with self._lock:
            return self._generation

    def blocked_error_code(self, tool: str, fingerprint: str) -> str | None:
        """Legacy threshold query; a returned code does not block Runtime execution."""

        key = (tool, fingerprint)
        with self._lock:
            counts = self._live_counts_locked(key)
            if not counts:
                return None
            self._entries.move_to_end(key)
            for code, count in counts.items():
                if count >= self._limit:
                    return code
        return None

    def record_failure(
        self,
        tool: str,
        fingerprint: str,
        *,
        error_code: str,
        retryable: bool,
        generation: int | None = None,
    ) -> int:
        """Count one failure and return the recent count for this request and code."""

        if error_code in BREAKER_EXCLUDED_ERROR_CODES:
            return 0
        if retryable and error_code not in RETRY_MEANS_CHANGING_THE_CALL:
            return 0
        with self._lock:
            if generation is not None and generation != self._generation:
                # The call began against a tree whose verdicts have since been
                # invalidated. Let the caller observe its real failure, but do
                # not seed the fresh generation with stale evidence.
                return 0
            key = (tool, fingerprint)
            counts = self._live_counts_locked(key)
            if counts is None:
                counts = self._entries[key] = {}
            self._entries.move_to_end(key)
            self._last_failure[key] = self._clock()
            count = counts.get(error_code, 0) + 1
            counts[error_code] = count
            while len(self._entries) > self._capacity:
                evicted, _ = self._entries.popitem(last=False)
                self._last_failure.pop(evicted, None)
            return count

    def _live_counts_locked(self, key: tuple[str, str]) -> dict[str, int] | None:
        """Return an entry's counts, dropping it first if its TTL has passed."""

        counts = self._entries.get(key)
        if counts is None:
            return None
        last = self._last_failure.get(key)
        if last is not None and self._clock() - last > self._ttl:
            del self._entries[key]
            del self._last_failure[key]
            return None
        return counts

    def record_success(
        self, tool: str, fingerprint: str, *, generation: int | None = None
    ) -> None:
        """Forget this call's history: the same arguments just worked."""

        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._entries.pop((tool, fingerprint), None)
            self._last_failure.pop((tool, fingerprint), None)

    def reset(self) -> None:
        """Forget stale diagnostic history after a possible workspace change."""

        with self._lock:
            self._entries.clear()
            self._last_failure.clear()
            self._generation += 1
