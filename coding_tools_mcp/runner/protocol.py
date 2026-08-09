"""Versioned, bounded JSON protocol shared by runner transport endpoints."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RUNNER_PROTOCOL_VERSION = "1"
DEFAULT_MAX_MESSAGE_BYTES = 256 * 1024
MAX_CAPABILITIES = 64
MAX_WORKSPACES = 256
MAX_IDENTIFIER_LENGTH = 128
MAX_ROOT_LENGTH = 4096
MAX_METHOD_LENGTH = 160
MAX_DISCONNECT_REASON_LENGTH = 256

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_CAPABILITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_METHOD_RE = re.compile(r"[a-z][a-z0-9_.:/-]{0,159}\Z")


class RunnerProtocolError(ValueError):
    """Raised when a runner message violates the public transport contract."""


@dataclass(frozen=True)
class WorkspaceInventoryItem:
    """Runner-local workspace metadata.

    ``root`` is deliberately a string. The control plane must never resolve a
    remote root with local ``Path`` APIs.
    """

    workspace_id: str
    name: str
    root: str
    enabled: bool = True

    def __post_init__(self) -> None:
        _validate_identifier(self.workspace_id, "workspace_id")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name.strip()) > 200:
            raise RunnerProtocolError("workspace name must contain 1-200 characters")
        if isinstance(self.root, Path) or not isinstance(self.root, str):
            raise RunnerProtocolError("remote workspace root must be runner-local string data")
        if not self.root.strip() or len(self.root) > MAX_ROOT_LENGTH:
            raise RunnerProtocolError("remote workspace root must contain 1-4096 characters")
        if not isinstance(self.enabled, bool):
            raise RunnerProtocolError("workspace enabled must be a boolean")

    def payload(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "name": self.name.strip(),
            "root": self.root,
            "enabled": self.enabled,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "WorkspaceInventoryItem":
        if not isinstance(payload, dict):
            raise RunnerProtocolError("workspace inventory item must be an object")
        return cls(
            workspace_id=_required_string(payload, "workspace_id"),
            name=_required_string(payload, "name"),
            root=_required_string(payload, "root"),
            enabled=payload.get("enabled", True),
        )


@dataclass(frozen=True)
class RunnerHello:
    runner_id: str
    instance_id: str
    credential: str
    capabilities: tuple[str, ...]
    workspaces: tuple[WorkspaceInventoryItem, ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.runner_id, "runner_id")
        _validate_identifier(self.instance_id, "instance_id")
        if not isinstance(self.credential, str) or not self.credential or len(self.credential) > 512:
            raise RunnerProtocolError("runner credential is required")
        if len(self.capabilities) > MAX_CAPABILITIES:
            raise RunnerProtocolError("too many runner capabilities")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise RunnerProtocolError("runner capabilities must be unique")
        for capability in self.capabilities:
            if not isinstance(capability, str) or _CAPABILITY_RE.fullmatch(capability) is None:
                raise RunnerProtocolError("runner capability contains unsupported characters")
        if len(self.workspaces) > MAX_WORKSPACES:
            raise RunnerProtocolError("too many runner workspaces")
        workspace_ids = [item.workspace_id for item in self.workspaces]
        if len(set(workspace_ids)) != len(workspace_ids):
            raise RunnerProtocolError("runner workspace ids must be unique")

    def public_payload(self) -> dict[str, Any]:
        """Return a payload safe for status views and logs; excludes credential."""

        return {
            "runner_id": self.runner_id,
            "instance_id": self.instance_id,
            "capabilities": list(self.capabilities),
            "workspaces": [item.payload() for item in self.workspaces],
        }

    def message_payload(self) -> dict[str, Any]:
        payload = self.public_payload()
        payload.update(
            {
                "type": "hello",
                "protocol_version": RUNNER_PROTOCOL_VERSION,
                "credential": self.credential,
            }
        )
        return payload

    @classmethod
    def from_payload(cls, payload: Any) -> "RunnerHello":
        message = require_message_type(payload, "hello")
        _require_protocol_version(message)
        raw_capabilities = message.get("capabilities", [])
        raw_workspaces = message.get("workspaces", [])
        if not isinstance(raw_capabilities, list):
            raise RunnerProtocolError("runner capabilities must be an array")
        if not isinstance(raw_workspaces, list):
            raise RunnerProtocolError("runner workspaces must be an array")
        return cls(
            runner_id=_required_string(message, "runner_id"),
            instance_id=_required_string(message, "instance_id"),
            credential=_required_string(message, "credential"),
            capabilities=tuple(raw_capabilities),
            workspaces=tuple(WorkspaceInventoryItem.from_payload(item) for item in raw_workspaces),
        )


@dataclass(frozen=True)
class RunnerHeartbeat:
    runner_id: str
    instance_id: str
    sequence: int
    workspaces: tuple[WorkspaceInventoryItem, ...] | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.runner_id, "runner_id")
        _validate_identifier(self.instance_id, "instance_id")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise RunnerProtocolError("heartbeat sequence must be a non-negative integer")
        if self.workspaces is not None:
            if len(self.workspaces) > MAX_WORKSPACES:
                raise RunnerProtocolError("too many heartbeat workspaces")
            workspace_ids = [item.workspace_id for item in self.workspaces]
            if len(set(workspace_ids)) != len(workspace_ids):
                raise RunnerProtocolError("heartbeat workspace ids must be unique")

    @classmethod
    def from_payload(cls, payload: Any) -> "RunnerHeartbeat":
        message = require_message_type(payload, "heartbeat")
        raw_workspaces = message.get("workspaces")
        if raw_workspaces is not None and not isinstance(raw_workspaces, list):
            raise RunnerProtocolError("heartbeat workspaces must be an array when provided")
        return cls(
            runner_id=_required_string(message, "runner_id"),
            instance_id=_required_string(message, "instance_id"),
            sequence=message.get("sequence"),
            workspaces=(
                tuple(WorkspaceInventoryItem.from_payload(item) for item in raw_workspaces)
                if raw_workspaces is not None
                else None
            ),
        )


@dataclass(frozen=True)
class RunnerEvent:
    runner_id: str
    instance_id: str
    name: str
    payload: dict[str, Any]
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.runner_id, "runner_id")
        _validate_identifier(self.instance_id, "instance_id")
        if not isinstance(self.name, str) or _METHOD_RE.fullmatch(self.name) is None:
            raise RunnerProtocolError("runner event name contains unsupported characters")
        if self.workspace_id is not None:
            _validate_identifier(self.workspace_id, "workspace_id")
        if not isinstance(self.payload, dict):
            raise RunnerProtocolError("runner event payload must be an object")

    def message_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "event",
            "runner_id": self.runner_id,
            "instance_id": self.instance_id,
            "name": self.name,
            "payload": self.payload,
        }
        if self.workspace_id is not None:
            result["workspace_id"] = self.workspace_id
        return result

    @classmethod
    def from_payload(cls, payload: Any) -> "RunnerEvent":
        message = require_message_type(payload, "event")
        raw_payload = message.get("payload", {})
        if not isinstance(raw_payload, dict):
            raise RunnerProtocolError("runner event payload must be an object")
        workspace_id = message.get("workspace_id")
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise RunnerProtocolError("runner event workspace_id must be a string when provided")
        return cls(
            runner_id=_required_string(message, "runner_id"),
            instance_id=_required_string(message, "instance_id"),
            name=_required_string(message, "name"),
            payload=raw_payload,
            workspace_id=workspace_id,
        )


@dataclass(frozen=True)
class RunnerDisconnect:
    runner_id: str
    instance_id: str
    reason: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.runner_id, "runner_id")
        _validate_identifier(self.instance_id, "instance_id")
        if self.reason is not None:
            if not isinstance(self.reason, str) or len(self.reason) > MAX_DISCONNECT_REASON_LENGTH:
                raise RunnerProtocolError("runner disconnect reason is too long")

    def message_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "disconnect",
            "runner_id": self.runner_id,
            "instance_id": self.instance_id,
        }
        if self.reason:
            result["reason"] = self.reason
        return result

    @classmethod
    def from_payload(cls, payload: Any) -> "RunnerDisconnect":
        message = require_message_type(payload, "disconnect")
        reason = message.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise RunnerProtocolError("runner disconnect reason must be a string when provided")
        return cls(
            runner_id=_required_string(message, "runner_id"),
            instance_id=_required_string(message, "instance_id"),
            reason=reason,
        )


def disconnect_ack_payload(runner_id: str, instance_id: str) -> dict[str, Any]:
    _validate_identifier(runner_id, "runner_id")
    _validate_identifier(instance_id, "instance_id")
    return {
        "type": "disconnect_ack",
        "runner_id": runner_id,
        "instance_id": instance_id,
    }


def encode_message(payload: dict[str, Any], *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> str:
    if not isinstance(payload, dict):
        raise RunnerProtocolError("runner message must be an object")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError("runner message is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise RunnerProtocolError("runner message exceeds the configured size limit")
    return encoded


def decode_message(raw: str | bytes, *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise RunnerProtocolError("runner message exceeds the configured size limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RunnerProtocolError("runner message must be UTF-8") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > max_bytes:
            raise RunnerProtocolError("runner message exceeds the configured size limit")
        text = raw
    else:
        raise RunnerProtocolError("runner message must be text or UTF-8 bytes")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RunnerProtocolError("runner message must contain valid JSON") from exc
    if not isinstance(payload, dict):
        raise RunnerProtocolError("runner message must be a JSON object")
    return payload


def require_message_type(payload: Any, expected: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RunnerProtocolError("runner message must be an object")
    if payload.get("type") != expected:
        raise RunnerProtocolError(f"expected runner message type {expected!r}")
    return payload


def rpc_request_payload(
    request_id: str,
    *,
    workspace_id: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_identifier(request_id, "request_id")
    _validate_identifier(workspace_id, "workspace_id")
    if not isinstance(method, str) or _METHOD_RE.fullmatch(method) is None:
        raise RunnerProtocolError("runner RPC method contains unsupported characters")
    if params is not None and not isinstance(params, dict):
        raise RunnerProtocolError("runner RPC params must be an object")
    return {
        "type": "rpc_request",
        "request_id": request_id,
        "workspace_id": workspace_id,
        "method": method,
        "params": params or {},
    }


def parse_rpc_request(payload: Any) -> tuple[str, str, str, dict[str, Any]]:
    message = require_message_type(payload, "rpc_request")
    request_id = _required_string(message, "request_id")
    workspace_id = _required_string(message, "workspace_id")
    method = _required_string(message, "method")
    params = message.get("params", {})
    _validate_identifier(request_id, "request_id")
    _validate_identifier(workspace_id, "workspace_id")
    if _METHOD_RE.fullmatch(method) is None:
        raise RunnerProtocolError("runner RPC method contains unsupported characters")
    if not isinstance(params, dict):
        raise RunnerProtocolError("runner RPC params must be an object")
    return request_id, workspace_id, method, params


def rpc_response_payload(
    request_id: str,
    *,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_identifier(request_id, "request_id")
    if error is not None and not isinstance(error, dict):
        raise RunnerProtocolError("runner RPC error must be an object")
    if error is not None and result is not None:
        raise RunnerProtocolError("runner RPC response cannot contain both result and error")
    return {
        "type": "rpc_response",
        "request_id": request_id,
        "error": error,
        "result": result,
    }


def parse_rpc_response(payload: Any) -> tuple[str, Any, dict[str, Any] | None]:
    message = require_message_type(payload, "rpc_response")
    request_id = _required_string(message, "request_id")
    _validate_identifier(request_id, "request_id")
    error = message.get("error")
    if error is not None and not isinstance(error, dict):
        raise RunnerProtocolError("runner RPC error must be an object")
    result = message.get("result")
    if error is not None and result is not None:
        raise RunnerProtocolError("runner RPC response cannot contain both result and error")
    return request_id, result, error


def _require_protocol_version(payload: dict[str, Any]) -> None:
    if payload.get("protocol_version") != RUNNER_PROTOCOL_VERSION:
        raise RunnerProtocolError("runner protocol version is unsupported")


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise RunnerProtocolError(f"runner message field {field!r} must be a non-empty string")
    return value


def _validate_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise RunnerProtocolError(
            f"{field} must contain 1-{MAX_IDENTIFIER_LENGTH} supported ASCII identifier characters"
        )
    return value


def validate_identifier(value: Any, field: str = "identifier") -> str:
    """Validate a runner-facing identifier without exposing protocol internals."""

    return _validate_identifier(value, field)
