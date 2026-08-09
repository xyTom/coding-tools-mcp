from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

SemanticPayload = dict[str, Any]


def semantic_unavailable(
    capability: str,
    reason: str,
    *,
    backend: str,
    code: str = "SEMANTIC_UNAVAILABLE",
    language: str | None = None,
    path: str | None = None,
) -> SemanticPayload:
    payload: SemanticPayload = {
        "ok": False,
        "backend": backend,
        "capability": capability,
        "status": "unavailable",
        "error": {
            "code": code,
            "message": reason,
            "category": "capability",
            "retryable": False,
        },
    }
    if language is not None:
        payload["language"] = language
    if path is not None:
        payload["path"] = path
    return payload


class SemanticBackend(ABC):
    """Workspace-scoped semantic code intelligence capability."""

    @abstractmethod
    def semantic_status(self, path: str | None = None) -> SemanticPayload:
        raise NotImplementedError

    @abstractmethod
    def document_symbols(self, path: str) -> SemanticPayload:
        raise NotImplementedError

    @abstractmethod
    def goto_definition(self, path: str, line: int, character: int) -> SemanticPayload:
        raise NotImplementedError

    @abstractmethod
    def find_references(self, path: str, line: int, character: int) -> SemanticPayload:
        raise NotImplementedError

    @abstractmethod
    def document_diagnostics(self, path: str) -> SemanticPayload:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> SemanticBackend:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class NullSemanticBackend(SemanticBackend):
    """Explicit capability-unavailable backend used when semantic support is disabled."""

    def __init__(self, reason: str = "Semantic code intelligence is not configured.") -> None:
        self.reason = reason

    def semantic_status(self, path: str | None = None) -> SemanticPayload:
        payload: SemanticPayload = {
            "ok": True,
            "backend": "null",
            "capability": "semantic_status",
            "status": "unavailable",
            "reason": self.reason,
            "languages": [],
        }
        if path is not None:
            payload["path"] = path
        return payload

    def document_symbols(self, path: str) -> SemanticPayload:
        return semantic_unavailable(
            "document_symbols", self.reason, backend="null", path=path
        )

    def goto_definition(self, path: str, line: int, character: int) -> SemanticPayload:
        return semantic_unavailable(
            "goto_definition", self.reason, backend="null", path=path
        )

    def find_references(self, path: str, line: int, character: int) -> SemanticPayload:
        return semantic_unavailable(
            "find_references", self.reason, backend="null", path=path
        )

    def document_diagnostics(self, path: str) -> SemanticPayload:
        return semantic_unavailable(
            "document_diagnostics", self.reason, backend="null", path=path
        )

    def close(self) -> None:
        return None
