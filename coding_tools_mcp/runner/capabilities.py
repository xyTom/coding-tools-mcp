"""Coarse remote Workspace capability routing over the authenticated Runner RPC."""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..agent_backends.base import (
    AgentBackendError,
    AgentBackendEvent,
    AgentSessionBackend,
    BackendHealth,
    BackendThread,
    BackendTurn,
)
from ..semantic.base import SemanticBackend
from ..validation.base import ValidationBackend, ValidationResult
from .routing import RemoteMcpRouteError, RemoteMcpRouteService
from .transport import RunnerRemoteError, RunnerUnavailableError


MAX_CAPABILITY_HANDLES = 512
MAX_CAPABILITY_TOMBSTONES = 512
MAX_AGENT_EVENTS = 200
MAX_AGENT_THREADS = 200


class RunnerCapabilityError(RuntimeError):
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


@dataclass
class _CapabilityRecord:
    workspace_id: str
    kind: str
    capability: Any


class RunnerCapabilityHost:
    """Runner-side bounded handle table for Agent/Semantic/Validation capabilities."""

    def __init__(
        self,
        *,
        agent_backend_factory: Callable[[str, str], AgentSessionBackend] | None = None,
        semantic_backend_factory: Callable[[str], SemanticBackend] | None = None,
        validation_backend_factory: Callable[[str], ValidationBackend] | None = None,
        fingerprint_factory: Callable[[str], Mapping[str, Any]] | None = None,
        max_handles: int = MAX_CAPABILITY_HANDLES,
    ) -> None:
        if max_handles < 1:
            raise ValueError("max_handles must be positive")
        self.agent_backend_factory = agent_backend_factory
        self.semantic_backend_factory = semantic_backend_factory
        self.validation_backend_factory = validation_backend_factory
        self.fingerprint_factory = fingerprint_factory
        self.max_handles = max_handles
        self._lock = threading.RLock()
        self._handles: dict[str, _CapabilityRecord] = {}
        self._closed: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def dispatch(self, workspace_id: str, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "workspace.fingerprint":
            if self.fingerprint_factory is None:
                raise RunnerCapabilityError(
                    "WORKSPACE_FINGERPRINT_UNAVAILABLE",
                    "Runner workspace fingerprint is unavailable",
                    retryable=True,
                )
            raw = self.fingerprint_factory(workspace_id)
            if not isinstance(raw, Mapping):
                raise RunnerCapabilityError(
                    "RUNNER_CAPABILITY_INVALID_RESPONSE",
                    "Runner workspace fingerprint is invalid",
                )
            return {"fingerprint": _bounded_fingerprint(raw)}
        if method == "capability.open":
            return self._open(workspace_id, params)
        if method == "capability.call":
            return self._call(workspace_id, params)
        if method == "capability.close":
            return self._close(workspace_id, params)
        raise RunnerCapabilityError(
            "RUNNER_RPC_METHOD_UNSUPPORTED",
            "Runner capability method is unsupported",
        )

    def _open(self, workspace_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        kind = _required_choice(params.get("kind"), "kind", {"agent", "semantic", "validation"})
        with self._lock:
            if len(self._handles) >= self.max_handles:
                raise RunnerCapabilityError(
                    "RUNNER_CAPABILITY_CAPACITY",
                    "Runner capability handle capacity reached",
                    retryable=True,
                )
        capability: Any
        if kind == "agent":
            if self.agent_backend_factory is None:
                raise RunnerCapabilityError("AGENT_BACKEND_UNAVAILABLE", "Runner Agent backend is unavailable", retryable=True)
            backend_kind = params.get("backend_kind", "codex-app-server")
            if not isinstance(backend_kind, str) or not backend_kind:
                raise RunnerCapabilityError("RUNNER_RPC_INVALID_PARAMS", "backend_kind is required")
            capability = self.agent_backend_factory(workspace_id, backend_kind)
        elif kind == "semantic":
            if self.semantic_backend_factory is None:
                raise RunnerCapabilityError("SEMANTIC_UNAVAILABLE", "Runner semantic backend is unavailable", retryable=True)
            capability = self.semantic_backend_factory(workspace_id)
        else:
            if self.validation_backend_factory is None:
                raise RunnerCapabilityError("VALIDATION_UNAVAILABLE", "Runner validation backend is unavailable", retryable=True)
            capability = self.validation_backend_factory(workspace_id)

        handle = f"cap-{secrets.token_hex(12)}"
        installed = False
        try:
            with self._lock:
                if len(self._handles) >= self.max_handles:
                    raise RunnerCapabilityError(
                        "RUNNER_CAPABILITY_CAPACITY",
                        "Runner capability handle capacity reached",
                        retryable=True,
                    )
                self._handles[handle] = _CapabilityRecord(workspace_id, kind, capability)
                installed = True
            return {"handle": handle, "kind": kind}
        finally:
            if not installed:
                _close_quietly(capability)

    def _record(self, workspace_id: str, params: Mapping[str, Any]) -> tuple[str, _CapabilityRecord]:
        handle = _required_handle(params.get("handle"))
        kind = _required_choice(params.get("kind"), "kind", {"agent", "semantic", "validation"})
        with self._lock:
            record = self._handles.get(handle)
        if record is None:
            raise RunnerCapabilityError("RUNNER_CAPABILITY_NOT_FOUND", "Runner capability handle is unknown")
        if record.workspace_id != workspace_id or record.kind != kind:
            raise RunnerCapabilityError("RUNNER_CAPABILITY_FORBIDDEN", "Runner capability route does not match")
        return handle, record

    def _call(self, workspace_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        _handle, record = self._record(workspace_id, params)
        operation = params.get("operation")
        arguments = params.get("arguments", {})
        if not isinstance(operation, str) or not operation:
            raise RunnerCapabilityError("RUNNER_RPC_INVALID_PARAMS", "capability operation is required")
        if not isinstance(arguments, Mapping):
            raise RunnerCapabilityError("RUNNER_RPC_INVALID_PARAMS", "capability arguments must be an object")
        if record.kind == "agent":
            return {"result": _dispatch_agent(record.capability, operation, arguments)}
        if record.kind == "semantic":
            return {"result": _dispatch_semantic(record.capability, operation, arguments)}
        return {"result": _dispatch_validation(record.capability, operation, arguments)}

    def _close(self, workspace_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        handle = _required_handle(params.get("handle"))
        kind = _required_choice(params.get("kind"), "kind", {"agent", "semantic", "validation"})
        with self._lock:
            record = self._handles.get(handle)
            if record is None:
                tombstone = self._closed.get(handle)
                if tombstone is None:
                    return {"closed": True}
                if tombstone != (workspace_id, kind):
                    raise RunnerCapabilityError("RUNNER_CAPABILITY_FORBIDDEN", "closed capability route does not match")
                return {"closed": True}
            if record.workspace_id != workspace_id or record.kind != kind:
                raise RunnerCapabilityError("RUNNER_CAPABILITY_FORBIDDEN", "Runner capability route does not match")
            self._handles.pop(handle, None)
            self._closed[handle] = (workspace_id, kind)
            self._closed.move_to_end(handle)
            while len(self._closed) > MAX_CAPABILITY_TOMBSTONES:
                self._closed.popitem(last=False)
        _close_quietly(record.capability)
        return {"closed": True}

    def shutdown(self) -> None:
        with self._lock:
            records = tuple(self._handles.values())
            self._handles.clear()
        for record in records:
            _close_quietly(record.capability)


class _RemoteCapabilityProxy:
    kind = ""

    def __init__(
        self,
        route_service: RemoteMcpRouteService,
        runner_id: str,
        workspace_id: str,
        *,
        open_params: dict[str, Any] | None = None,
    ) -> None:
        self.route_service = route_service
        self.runner_id = runner_id
        self.workspace_id = workspace_id
        self.closed = False
        response = self._rpc_raw(
            "capability.open",
            {"kind": self.kind, **dict(open_params or {})},
        )
        handle = response.get("handle") if isinstance(response, Mapping) else None
        self.handle = _required_handle(handle)

    def _rpc_raw(self, method: str, params: dict[str, Any]) -> Any:
        return self.route_service.call_runner_sync(
            runner_id=self.runner_id,
            workspace_id=self.workspace_id,
            method=method,
            params=params,
        )

    def _call(self, operation: str, arguments: dict[str, Any] | None = None) -> Any:
        if self.closed:
            raise RunnerCapabilityError("RUNNER_CAPABILITY_CLOSED", "remote capability is closed")
        response = self._rpc_raw(
            "capability.call",
            {
                "handle": self.handle,
                "kind": self.kind,
                "operation": operation,
                "arguments": arguments or {},
            },
        )
        if not isinstance(response, Mapping) or "result" not in response:
            raise RunnerCapabilityError("RUNNER_CAPABILITY_INVALID_RESPONSE", "Runner capability response is invalid")
        return response["result"]

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._rpc_raw(
                "capability.close",
                {"handle": self.handle, "kind": self.kind},
            )
        except (RemoteMcpRouteError, RunnerUnavailableError, RunnerRemoteError):
            return
        finally:
            self.closed = True


class RemoteAgentBackendProxy(_RemoteCapabilityProxy):
    kind = "agent"

    def __init__(self, route_service: RemoteMcpRouteService, runner_id: str, workspace_id: str, backend_kind: str) -> None:
        self._backend_kind = backend_kind
        try:
            super().__init__(route_service, runner_id, workspace_id, open_params={"backend_kind": backend_kind})
        except Exception as exc:  # noqa: BLE001 - normalize route errors to backend contract
            raise _agent_error(exc) from exc

    @property
    def backend_kind(self) -> str:
        return self._backend_kind

    def health(self) -> BackendHealth:
        try:
            raw = self._call("health")
            return BackendHealth(
                bool(raw.get("available")),
                str(raw.get("backend_kind") or self.backend_kind),
                version=raw.get("version") if isinstance(raw.get("version"), str) else None,
                error=dict(raw["error"]) if isinstance(raw.get("error"), Mapping) else None,
            )
        except Exception as exc:  # noqa: BLE001
            error = _agent_error(exc)
            return BackendHealth(False, self.backend_kind, error=error.payload())

    def create_thread(self, *, instructions: str | None = None) -> BackendThread:
        return _thread(self._agent_call("create_thread", {"instructions": instructions}))

    def resume_thread(self, thread_id: str, *, instructions: str | None = None) -> BackendThread:
        return _thread(self._agent_call("resume_thread", {"thread_id": thread_id, "instructions": instructions}))

    def send_turn(self, thread_id: str, message: str) -> BackendTurn:
        raw = self._agent_call("send_turn", {"thread_id": thread_id, "message": message})
        return BackendTurn(str(raw["turn_id"]), dict(raw.get("metadata") or {}))

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self._agent_call("interrupt_turn", {"thread_id": thread_id, "turn_id": turn_id})

    def approve(self, approval_id: str, decision: str) -> None:
        self._agent_call("approve", {"approval_id": approval_id, "decision": decision})

    def list_threads(self, *, limit: int = 50) -> list[BackendThread]:
        raw = self._agent_call("list_threads", {"limit": limit})
        return [_thread(item) for item in raw]

    def close_thread(self, thread_id: str) -> None:
        self._agent_call("close_thread", {"thread_id": thread_id})

    def stream_events(self, *, timeout: float | None = None):
        del timeout
        yield from self.drain_events(limit=MAX_AGENT_EVENTS)

    def drain_events(self, *, limit: int = 100) -> list[AgentBackendEvent]:
        raw = self._agent_call("drain_events", {"limit": limit})
        return [_event(item) for item in raw]

    def _agent_call(self, operation: str, arguments: dict[str, Any] | None = None) -> Any:
        try:
            return self._call(operation, arguments)
        except Exception as exc:  # noqa: BLE001
            raise _agent_error(exc) from exc


class RemoteSemanticBackendProxy(_RemoteCapabilityProxy, SemanticBackend):
    kind = "semantic"

    def semantic_status(self, path: str | None = None) -> dict[str, Any]:
        return self._semantic("semantic_status", {"path": path})

    def document_symbols(self, path: str) -> dict[str, Any]:
        return self._semantic("document_symbols", {"path": path})

    def goto_definition(self, path: str, line: int, character: int) -> dict[str, Any]:
        return self._semantic("goto_definition", {"path": path, "line": line, "character": character})

    def find_references(self, path: str, line: int, character: int) -> dict[str, Any]:
        return self._semantic("find_references", {"path": path, "line": line, "character": character})

    def document_diagnostics(self, path: str) -> dict[str, Any]:
        return self._semantic("document_diagnostics", {"path": path})

    def _semantic(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = self._call(operation, arguments)
            return dict(raw) if isinstance(raw, Mapping) else _semantic_error(operation, "RUNNER_CAPABILITY_INVALID_RESPONSE", "Runner semantic response is invalid", False)
        except Exception as exc:  # noqa: BLE001
            code, message, retryable = _route_error(exc)
            return _semantic_error(operation, code, message, retryable)


class RemoteValidationBackendProxy(_RemoteCapabilityProxy):
    kind = "validation"

    def status(self) -> dict[str, Any]:
        try:
            raw = self._call("status")
            return dict(raw) if isinstance(raw, Mapping) else {"ok": False, "status": "unavailable", "reason": "invalid Runner response"}
        except Exception as exc:  # noqa: BLE001
            _code, message, _retryable = _route_error(exc)
            return {"ok": False, "backend": "remote-runner", "status": "unavailable", "reason": message}

    def run(self, recipe: str) -> ValidationResult:
        try:
            raw = self._call("run", {"recipe": recipe})
            if not isinstance(raw, Mapping):
                raise RunnerCapabilityError("RUNNER_CAPABILITY_INVALID_RESPONSE", "Runner validation response is invalid")
            diagnostics = raw.get("diagnostics")
            return ValidationResult(
                status=str(raw.get("status") or "unavailable"),
                recipe=str(raw.get("recipe") or recipe),
                command=raw.get("command") if isinstance(raw.get("command"), str) else None,
                exit_code=raw.get("exit_code") if isinstance(raw.get("exit_code"), int) else None,
                diagnostics=tuple(dict(item) for item in diagnostics if isinstance(item, Mapping)) if isinstance(diagnostics, list) else (),
                duration_ms=int(raw.get("duration_ms") or 0),
                reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
            )
        except Exception as exc:  # noqa: BLE001
            _code, message, _retryable = _route_error(exc)
            return ValidationResult("unavailable", recipe, reason=message)


def _dispatch_agent(backend: AgentSessionBackend, operation: str, args: Mapping[str, Any]) -> Any:
    if operation == "health":
        health = backend.health()
        return {"available": health.available, "backend_kind": health.backend_kind, "version": health.version, "error": health.error}
    if operation == "create_thread":
        return _thread_payload(backend.create_thread(instructions=_optional_string(args.get("instructions"))))
    if operation == "resume_thread":
        return _thread_payload(backend.resume_thread(_required_string(args.get("thread_id"), "thread_id"), instructions=_optional_string(args.get("instructions"))))
    if operation == "send_turn":
        turn = backend.send_turn(_required_string(args.get("thread_id"), "thread_id"), _required_string(args.get("message"), "message"))
        return {"turn_id": turn.turn_id, "metadata": dict(turn.metadata)}
    if operation == "interrupt_turn":
        backend.interrupt_turn(_required_string(args.get("thread_id"), "thread_id"), _required_string(args.get("turn_id"), "turn_id")); return {}
    if operation == "approve":
        backend.approve(_required_string(args.get("approval_id"), "approval_id"), _required_string(args.get("decision"), "decision")); return {}
    if operation == "list_threads":
        limit = _bounded_int(args.get("limit", 50), 1, MAX_AGENT_THREADS, "limit")
        return [_thread_payload(item) for item in backend.list_threads(limit=limit)]
    if operation == "close_thread":
        backend.close_thread(_required_string(args.get("thread_id"), "thread_id")); return {}
    if operation == "drain_events":
        limit = _bounded_int(args.get("limit", 100), 1, MAX_AGENT_EVENTS, "limit")
        return [_event_payload(item) for item in backend.drain_events(limit=limit)]
    raise RunnerCapabilityError("RUNNER_CAPABILITY_OPERATION_UNSUPPORTED", "Agent capability operation is unsupported")


def _dispatch_semantic(backend: SemanticBackend, operation: str, args: Mapping[str, Any]) -> Any:
    path = args.get("path")
    if path is not None and not isinstance(path, str):
        raise RunnerCapabilityError("RUNNER_RPC_INVALID_PARAMS", "path must be a string")
    if operation == "semantic_status": return backend.semantic_status(path)
    if operation == "document_symbols": return backend.document_symbols(_required_string(path, "path"))
    if operation == "document_diagnostics": return backend.document_diagnostics(_required_string(path, "path"))
    line = _bounded_int(args.get("line"), 0, 10_000_000, "line")
    character = _bounded_int(args.get("character"), 0, 10_000_000, "character")
    if operation == "goto_definition": return backend.goto_definition(_required_string(path, "path"), line, character)
    if operation == "find_references": return backend.find_references(_required_string(path, "path"), line, character)
    raise RunnerCapabilityError("RUNNER_CAPABILITY_OPERATION_UNSUPPORTED", "Semantic capability operation is unsupported")


def _dispatch_validation(backend: ValidationBackend, operation: str, args: Mapping[str, Any]) -> Any:
    if operation == "status": return backend.status()
    if operation == "run": return backend.run(_required_string(args.get("recipe"), "recipe")).payload()
    raise RunnerCapabilityError("RUNNER_CAPABILITY_OPERATION_UNSUPPORTED", "Validation capability operation is unsupported")


def _route_error(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, (RunnerRemoteError, RemoteMcpRouteError)):
        return getattr(exc, "code", "RUNNER_UNAVAILABLE"), str(exc), bool(getattr(exc, "retryable", False))
    if isinstance(exc, RunnerUnavailableError):
        return "RUNNER_UNAVAILABLE", str(exc), True
    if isinstance(exc, RunnerCapabilityError):
        return exc.code, str(exc), exc.retryable
    return "RUNNER_CAPABILITY_FAILED", str(exc), False


def _agent_error(exc: Exception) -> AgentBackendError:
    if isinstance(exc, AgentBackendError): return exc
    code, message, retryable = _route_error(exc)
    return AgentBackendError(code, message, retryable=retryable)


def _semantic_error(operation: str, code: str, message: str, retryable: bool) -> dict[str, Any]:
    return {"ok": False, "backend": "remote-runner", "capability": operation, "status": "unavailable", "error": {"code": code, "message": message, "category": "capability", "retryable": retryable}}


def _thread_payload(item: BackendThread) -> dict[str, Any]: return {"thread_id": item.thread_id, "metadata": dict(item.metadata)}
def _event_payload(item: AgentBackendEvent) -> dict[str, Any]: return {"sequence": item.sequence, "kind": item.kind, "method": item.method, "params": dict(item.params), "approval_id": item.approval_id}
def _thread(raw: Any) -> BackendThread:
    if not isinstance(raw, Mapping): raise RunnerCapabilityError("RUNNER_CAPABILITY_INVALID_RESPONSE", "Runner thread response is invalid")
    return BackendThread(_required_string(raw.get("thread_id"), "thread_id"), dict(raw.get("metadata") or {}))
def _event(raw: Any) -> AgentBackendEvent:
    if not isinstance(raw, Mapping): raise RunnerCapabilityError("RUNNER_CAPABILITY_INVALID_RESPONSE", "Runner event response is invalid")
    return AgentBackendEvent(int(raw.get("sequence") or 0), str(raw.get("kind") or "event"), str(raw.get("method") or ""), dict(raw.get("params") or {}), raw.get("approval_id") if isinstance(raw.get("approval_id"), str) else None)
def _required_handle(value: Any) -> str:
    value = _required_string(value, "handle")
    if len(value) > 128: raise RunnerCapabilityError("RUNNER_RPC_INVALID_PARAMS", "handle is too long")
    return value
def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096: raise RunnerCapabilityError("RUNNER_RPC_INVALID_PARAMS", f"{field} must be a non-empty bounded string")
    return value
def _optional_string(value: Any) -> str | None:
    if value is None: return None
    return _required_string(value, "value")
def _required_choice(value: Any, field: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices: raise RunnerCapabilityError("RUNNER_RPC_INVALID_PARAMS", f"{field} is invalid")
    return value
def _bounded_int(value: Any, minimum: int, maximum: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum: raise RunnerCapabilityError("RUNNER_RPC_INVALID_PARAMS", f"{field} is out of range")
    return value
def _close_quietly(capability: Any) -> None:
    close = getattr(capability, "close", None)
    if callable(close):
        try: close()
        except Exception: pass


def _bounded_fingerprint(raw: Mapping[str, Any]) -> dict[str, Any]:
    changed_paths = raw.get("changed_paths")
    paths = (
        [str(item)[:512] for item in changed_paths[:100] if isinstance(item, str)]
        if isinstance(changed_paths, list)
        else []
    )
    changes = raw.get("changes")
    bounded_changes = (
        [str(item)[:200] for item in changes[:20] if isinstance(item, str)]
        if isinstance(changes, list)
        else []
    )
    return {
        "version": int(raw.get("version") or 1),
        "git_available": bool(raw.get("git_available", False)),
        "branch": str(raw["branch"])[:128] if isinstance(raw.get("branch"), str) else None,
        "head": str(raw["head"])[:128] if isinstance(raw.get("head"), str) else None,
        "worktree_digest": str(raw["worktree_digest"])[:128] if isinstance(raw.get("worktree_digest"), str) else None,
        "instruction_digest": str(raw["instruction_digest"])[:128] if isinstance(raw.get("instruction_digest"), str) else None,
        "changed_paths": paths,
        "context_changed": bool(raw.get("context_changed", False)),
        "changes": bounded_changes,
        "remote": True,
    }


__all__ = [
    "RemoteAgentBackendProxy",
    "RemoteSemanticBackendProxy",
    "RemoteValidationBackendProxy",
    "RunnerCapabilityError",
    "RunnerCapabilityHost",
]
