"""Shared redacted failure throttling for isolated upstream MCP clients."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum


class UpstreamClientState(str, Enum):
    NEW = "NEW"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    SUSPECT = "SUSPECT"
    BACKING_OFF = "BACKING_OFF"
    CLOSED = "CLOSED"


class UpstreamResilienceGateError(RuntimeError):
    def __init__(self, code: str, message: str, *, retry_after_ms: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_ms = max(0, int(retry_after_ms))


@dataclass(frozen=True)
class BackoffPolicy:
    initial_seconds: float = 0.25
    maximum_seconds: float = 30.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.initial_seconds <= 0 or self.maximum_seconds <= 0:
            raise ValueError("upstream backoff durations must be positive")
        if self.maximum_seconds < self.initial_seconds:
            raise ValueError("upstream maximum backoff cannot be below initial backoff")
        if self.multiplier < 1:
            raise ValueError("upstream backoff multiplier must be at least 1")
        if self.jitter_ratio < 0 or self.jitter_ratio > 1:
            raise ValueError("upstream backoff jitter ratio must be between 0 and 1")


@dataclass(frozen=True)
class ResilienceSnapshot:
    initializing_count: int
    consecutive_failures: int
    next_retry_in_ms: int
    last_error_code: str | None
    circuit_open_total: int


class _TargetState:
    def __init__(self, max_initializations: int) -> None:
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(max_initializations)
        self.initializing_count = 0
        self.consecutive_failures = 0
        self.next_retry_at = 0.0
        self.last_error_code: str | None = None
        self.circuit_open_total = 0
        self.probe_in_flight = False


class UpstreamResilienceCoordinator:
    """Share only initialization throttling/backoff, never live client state."""

    def __init__(
        self,
        *,
        max_initializations: int = 4,
        policy: BackoffPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        random_value: Callable[[], float] = random.random,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(max_initializations, int) or isinstance(max_initializations, bool) or max_initializations < 1:
            raise ValueError("max upstream initializations must be a positive integer")
        self.max_initializations = max_initializations
        self.policy = policy or BackoffPolicy()
        self._clock = clock
        self._random_value = random_value
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._targets: dict[str, _TargetState] = {}

    def _target(self, key: str) -> _TargetState:
        with self._lock:
            state = self._targets.get(key)
            if state is None:
                state = _TargetState(self.max_initializations)
                self._targets[key] = state
            return state

    @contextmanager
    def initialization_slot(self, key: str, *, probe: bool) -> Iterator[None]:
        state = self._target(key)
        now = self._clock()
        effective_probe = probe
        with state.lock:
            if state.consecutive_failures > 0:
                effective_probe = True
            if effective_probe:
                if now < state.next_retry_at:
                    raise UpstreamResilienceGateError(
                        "UPSTREAM_BACKING_OFF",
                        "Upstream MCP target is backing off after a transport failure.",
                        retry_after_ms=int((state.next_retry_at - now) * 1000),
                    )
                if state.probe_in_flight:
                    raise UpstreamResilienceGateError(
                        "UPSTREAM_CIRCUIT_PROBE_IN_PROGRESS",
                        "Another Runtime is probing the recovering upstream MCP target.",
                        retry_after_ms=50,
                    )
                state.probe_in_flight = True
        acquired = state.semaphore.acquire(blocking=False)
        if not acquired:
            if effective_probe:
                with state.lock:
                    state.probe_in_flight = False
            raise UpstreamResilienceGateError(
                "UPSTREAM_INITIALIZATION_LIMIT",
                "Upstream MCP initialization concurrency limit reached.",
                retry_after_ms=50,
            )
        with state.lock:
            state.initializing_count += 1
        try:
            yield
        finally:
            with state.lock:
                state.initializing_count = max(0, state.initializing_count - 1)
                if effective_probe:
                    state.probe_in_flight = False
            state.semaphore.release()

    def record_success(self, key: str) -> None:
        state = self._target(key)
        with state.lock:
            state.consecutive_failures = 0
            state.next_retry_at = 0.0
            state.last_error_code = None

    def record_failure(self, key: str, error_code: str) -> int:
        state = self._target(key)
        with state.lock:
            state.consecutive_failures += 1
            exponent = max(0, state.consecutive_failures - 1)
            base = min(
                self.policy.maximum_seconds,
                self.policy.initial_seconds * (self.policy.multiplier**exponent),
            )
            random_unit = min(1.0, max(0.0, float(self._random_value())))
            jitter = base * self.policy.jitter_ratio * ((2.0 * random_unit) - 1.0)
            delay = max(0.0, min(self.policy.maximum_seconds, base + jitter))
            state.next_retry_at = self._clock() + delay
            state.last_error_code = error_code
            state.circuit_open_total += 1
            return int(delay * 1000)

    def snapshot(self, key: str) -> ResilienceSnapshot:
        state = self._target(key)
        now = self._clock()
        with state.lock:
            return ResilienceSnapshot(
                initializing_count=state.initializing_count,
                consecutive_failures=state.consecutive_failures,
                next_retry_in_ms=max(0, int((state.next_retry_at - now) * 1000)),
                last_error_code=state.last_error_code,
                circuit_open_total=state.circuit_open_total,
            )

    def sleep_until_retry(self, key: str) -> None:
        """Optional blocking helper; normal request paths use retryable errors instead."""

        delay_ms = self.snapshot(key).next_retry_in_ms
        if delay_ms > 0:
            self._sleeper(delay_ms / 1000)


DEFAULT_UPSTREAM_RESILIENCE_COORDINATOR = UpstreamResilienceCoordinator()
