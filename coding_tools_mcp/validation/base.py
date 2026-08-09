"""Product-neutral structured validation backend contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ValidationResult:
    status: str
    recipe: str
    command: str | None = None
    exit_code: int | None = None
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    duration_ms: int = 0
    reason: str | None = None

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "recipe": self.recipe,
            "command": self.command,
            "exit_code": self.exit_code,
            "diagnostics": [dict(item) for item in self.diagnostics],
            "duration_ms": self.duration_ms,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@runtime_checkable
class ValidationBackend(Protocol):
    """Workspace-scoped recipe validation that never installs dependencies."""

    def status(self) -> dict[str, Any]: ...

    def run(self, recipe: str) -> ValidationResult: ...

    def close(self) -> None: ...


class NullValidationBackend:
    """Explicit unavailable implementation used until a workspace supplies a runner."""

    def __init__(self, reason: str = "Structured validation is not configured.") -> None:
        self.reason = reason

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "backend": "null",
            "status": "unavailable",
            "reason": self.reason,
            "recipes": [],
        }

    def run(self, recipe: str) -> ValidationResult:
        if not isinstance(recipe, str) or not recipe:
            raise ValueError("recipe must be a non-empty string")
        return ValidationResult("unavailable", recipe, reason=self.reason)

    def close(self) -> None:
        return None
