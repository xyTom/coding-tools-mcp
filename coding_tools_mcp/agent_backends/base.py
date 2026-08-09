"""Stable, product-neutral interface for live agent backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Protocol, runtime_checkable


class AgentBackendError(RuntimeError):
    """Structured failure returned by an Agent Session backend."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class BackendHealth:
    available: bool
    backend_kind: str
    version: str | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackendThread:
    thread_id: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class BackendTurn:
    turn_id: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AgentBackendEvent:
    """One bounded backend event suitable for projection into a Web stream."""

    sequence: int
    kind: str
    method: str
    params: dict[str, Any]
    approval_id: str | None = None


@runtime_checkable
class AgentSessionBackend(Protocol):
    """Minimum live-agent contract required by AgentSessionService."""

    @property
    def backend_kind(self) -> str: ...

    def health(self) -> BackendHealth: ...

    def create_thread(self, *, instructions: str | None = None) -> BackendThread: ...

    def resume_thread(
        self,
        thread_id: str,
        *,
        instructions: str | None = None,
    ) -> BackendThread: ...

    def send_turn(self, thread_id: str, message: str) -> BackendTurn: ...

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None: ...

    def approve(self, approval_id: str, decision: str) -> None: ...

    def list_threads(self, *, limit: int = 50) -> list[BackendThread]: ...

    def close_thread(self, thread_id: str) -> None: ...

    def stream_events(self, *, timeout: float | None = None) -> Iterator[AgentBackendEvent]: ...

    def drain_events(self, *, limit: int = 100) -> list[AgentBackendEvent]: ...

    def close(self) -> None: ...
