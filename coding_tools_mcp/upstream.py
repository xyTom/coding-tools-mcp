from __future__ import annotations

import atexit
import copy
import json
import os
import queue
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.client import RemoteDisconnected
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from .json_utils import strict_json_bytes, strict_json_loads
from .upstream_result import RESULT_INLINE_MAX, budget_tool_result, result_json_bytes
from .upstream_result_store import ResultStore
from .upstream_resilience import (
    DEFAULT_UPSTREAM_RESILIENCE_COORDINATOR,
    UpstreamClientState,
    UpstreamResilienceCoordinator,
    UpstreamResilienceGateError,
)
from .upstream_sanitize import raw_schema_digest, sanitize_definition, schema_digest
from .upstream_search import (
    CatalogSearchIndex,
    SearchBackend,
    ToolSearchFilters,
    ToolSearchResult,
    UpstreamToolCatalogEntry,
)


DEFAULT_PROTOCOL_VERSION = "2025-11-25"
DEFAULT_TIMEOUT_MS = 30_000
UPSTREAM_CLOSE_TIMEOUT_SECONDS = 2.0
DEFAULT_CREDENTIAL_POLICY = "local"
CREDENTIAL_POLICIES = frozenset({"local", "strict"})
SENSITIVE_ENV_NAME_RE = re.compile(
    r"PASSWORD|PASSWD|TOKEN|SECRET|KEY|CREDENTIAL|AUTH",
    re.I,
)
MAX_RESPONSE_BYTES = 1_048_576
MAX_UPSTREAM_RESULT_DEPTH = 64
MAX_TOOL_NAME_CHARS = 512
MAX_TOOL_FILTER_ITEMS = 256
MAX_TAG_ITEMS = 32
MAX_TAG_CHARS = 64
MAX_TOOL_POLICY_ITEMS = 256
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
FORBIDDEN_STDIO_COMMANDS = {
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "bash",
    "bash.exe",
    "sh",
    "sh.exe",
    "zsh",
    "zsh.exe",
    "fish",
    "fish.exe",
}
SHELL_FRAGMENT_RE = re.compile(r"(\|\||&&|[|<>;`]|\$\(|\$\{)")
UPSTREAM_BASE_ENV_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "TERM",
    }
)


class UpstreamConfigError(ValueError):
    """Gateway configuration cannot safely form a fixed tool snapshot."""


class UpstreamError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "runtime",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.retryable = retryable
        self.details = details or {}


class _FrozenJsonDict(Mapping[str, Any]):
    """Read-only Mapping wrapper with no mutable dict base-class escape hatch."""

    __slots__ = ("_data",)
    _data: Mapping[str, Any]

    def __init__(self, value: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_data", MappingProxyType(dict(value)))

    @staticmethod
    def _raise_immutable() -> NoReturn:
        raise TypeError("Upstream definition snapshots are immutable.")

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        self._raise_immutable()

    def __repr__(self) -> str:
        return repr(dict(self._data))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return False

    def __copy__(self) -> "_FrozenJsonDict":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        copied = _thaw_frozen_json(self, memo)
        if not isinstance(copied, dict):
            raise TypeError("Frozen JSON object did not thaw to a dict.")
        return copied


class _FrozenJsonList(Sequence[Any]):
    """Read-only Sequence wrapper with no mutable list base-class escape hatch."""

    __slots__ = ("_items",)
    _items: tuple[Any, ...]

    def __init__(self, values: Iterable[Any]) -> None:
        object.__setattr__(self, "_items", tuple(values))

    @staticmethod
    def _raise_immutable() -> NoReturn:
        raise TypeError("Upstream definition snapshots are immutable.")

    def __getitem__(self, index: int | slice) -> Any:
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._items)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        self._raise_immutable()

    def __repr__(self) -> str:
        return repr(list(self._items))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(
            other,
            (str, bytes, bytearray),
        ):
            return tuple(self._items) == tuple(other)
        return False

    def append(self, value: Any) -> None:
        del value
        self._raise_immutable()

    def clear(self) -> None:
        self._raise_immutable()

    def extend(self, values: Iterable[Any]) -> None:
        del values
        self._raise_immutable()

    def insert(self, index: int, value: Any) -> None:
        del index, value
        self._raise_immutable()

    def pop(self, index: int = -1) -> Any:
        del index
        self._raise_immutable()

    def remove(self, value: Any) -> None:
        del value
        self._raise_immutable()

    def reverse(self) -> None:
        self._raise_immutable()

    def sort(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        del args, kwargs
        self._raise_immutable()

    def __copy__(self) -> "_FrozenJsonList":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        copied = _thaw_frozen_json(self, memo)
        if not isinstance(copied, list):
            raise TypeError("Frozen JSON array did not thaw to a list.")
        return copied


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _clone_json_tree(value: Any, *, max_depth: int) -> Any:
    """Clone an untrusted JSON tree without recursive Python calls.

    Parsed JSON cannot contain cycles or shared container identities. Rejecting
    either shape also keeps fabricated in-process clients inside the same
    protocol boundary as real stdio/HTTP clients.
    """

    if _is_json_scalar(value):
        return value
    if not isinstance(value, (dict, list)):
        raise TypeError("Value was not JSON-compatible.")

    # Container depth is counted uniformly across all upstream JSON contracts:
    # the root dict/list is level 1, each child dict/list adds one level, and
    # scalars add no level. Therefore max_depth=64 rejects the 65th container.
    completed: dict[int, Any] = {}
    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 1, False)]
    while stack:
        current, depth, expanded = stack.pop()
        if depth > max_depth:
            raise ValueError("JSON nesting exceeded the supported upstream depth.")
        if _is_json_scalar(current):
            continue
        if not isinstance(current, (dict, list)):
            raise TypeError("Value was not JSON-compatible.")

        identity = id(current)
        if expanded:
            active.remove(identity)
            if isinstance(current, dict):
                cloned: dict[str, Any] = {}
                for key, item in current.items():
                    if not isinstance(key, str):
                        raise TypeError("JSON object key was not a string.")
                    cloned[key] = item if _is_json_scalar(item) else completed[id(item)]
                completed[identity] = cloned
            else:
                completed[identity] = [
                    item if _is_json_scalar(item) else completed[id(item)]
                    for item in current
                ]
            continue

        if identity in active or identity in completed:
            raise ValueError("JSON tree contained a cycle or shared container.")
        active.add(identity)
        stack.append((current, depth, True))
        children = list(current.values()) if isinstance(current, dict) else list(current)
        for child in reversed(children):
            if _is_json_scalar(child):
                continue
            if not isinstance(child, (dict, list)):
                raise TypeError("Value was not JSON-compatible.")
            stack.append((child, depth + 1, False))

    return completed[id(value)]


def _thaw_frozen_json(value: Any, memo: dict[int, Any]) -> Any:
    """Export a frozen JSON snapshot without recursive Python calls."""

    if _is_json_scalar(value):
        return value
    if not isinstance(value, (_FrozenJsonDict, _FrozenJsonList)):
        raise TypeError("Value was not a frozen JSON snapshot.")
    existing = memo.get(id(value))
    if existing is not None:
        return existing

    completed: dict[int, Any] = {}
    active: set[int] = set()
    stack: list[tuple[Any, bool]] = [(value, False)]
    while stack:
        current, expanded = stack.pop()
        if _is_json_scalar(current):
            continue
        if not isinstance(current, (_FrozenJsonDict, _FrozenJsonList)):
            raise TypeError("Frozen snapshot contained a non-JSON value.")

        identity = id(current)
        if identity in memo:
            completed[identity] = memo[identity]
            continue
        if expanded:
            active.remove(identity)
            if isinstance(current, _FrozenJsonDict):
                thawed: dict[str, Any] = {}
                for key, item in current.items():
                    thawed[key] = (
                        item if _is_json_scalar(item) else completed[id(item)]
                    )
                completed[identity] = thawed
            else:
                completed[identity] = [
                    item if _is_json_scalar(item) else completed[id(item)]
                    for item in current
                ]
            memo[identity] = completed[identity]
            continue

        if identity in active or identity in completed:
            raise ValueError("Frozen JSON snapshot contained a cycle or shared container.")
        active.add(identity)
        stack.append((current, True))
        children = (
            list(current.values())
            if isinstance(current, _FrozenJsonDict)
            else list(current)
        )
        for child in reversed(children):
            if _is_json_scalar(child):
                continue
            if not isinstance(child, (_FrozenJsonDict, _FrozenJsonList)):
                raise TypeError("Frozen snapshot contained a non-JSON value.")
            if id(child) in memo:
                completed[id(child)] = memo[id(child)]
                continue
            stack.append((child, False))

    return completed[id(value)]


def _bounded_error_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize each untrusted top-level detail value independently.

    A container value may contain at most 64 container levels, counting its
    root container as level 1. Cycles or shared containers inside one detail
    value are omitted. Identity sharing between different top-level detail
    values is normalized independently, matching the value semantics of JSON.
    """

    try:
        source = {} if details is None else details
        if not isinstance(source, dict):
            raise TypeError("Error details were not an object.")
        cloned: dict[str, Any] = {}
        for key, value in source.items():
            if not isinstance(key, str):
                raise TypeError("Error detail key was not a string.")
            cloned[key] = (
                value
                if _is_json_scalar(value)
                else _clone_json_tree(
                    value,
                    max_depth=MAX_UPSTREAM_RESULT_DEPTH,
                )
            )
        # Structural cloning alone is insufficient for fabricated in-process
        # clients: NaN/Infinity and over-limit integers are Python scalars but
        # are not legal under the Gateway's strict JSON output contract.
        strict_json_bytes(cloned)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return {
            "_omitted": True,
            "_reason": "invalid_or_excessive_nesting",
        }
    return cloned


def _bounded_status_error(error: dict[str, Any]) -> dict[str, Any]:
    """Export a status error envelope while bounding only its details field."""

    fallback = {
        "code": "UPSTREAM_PROTOCOL_ERROR",
        "message": "Upstream status error payload was invalid.",
        "category": "protocol",
        "retryable": False,
        "details": {
            "_omitted": True,
            "_reason": "invalid_or_excessive_nesting",
        },
    }
    try:
        code = error["code"]
        message = error["message"]
        category = error["category"]
        retryable = error["retryable"]
        if (
            not isinstance(code, str)
            or not isinstance(message, str)
            or not isinstance(category, str)
            or type(retryable) is not bool
        ):
            raise TypeError("Status error envelope fields were invalid.")
        exported: dict[str, Any] = {
            "code": code,
            "message": message,
            "category": category,
            "retryable": retryable,
        }
        if "details" in error:
            exported["details"] = _bounded_error_details(error["details"])
        strict_json_bytes(exported)
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return fallback
    return exported


def _freeze_json(value: Any) -> Any:
    """Freeze an arbitrary-depth JSON tree without recursive Python calls."""

    if _is_json_scalar(value):
        return value
    if not isinstance(value, (dict, list)):
        raise TypeError("Value was not JSON-compatible.")

    completed: dict[int, Any] = {}
    active: set[int] = set()
    stack: list[tuple[Any, bool]] = [(value, False)]
    while stack:
        current, expanded = stack.pop()
        if _is_json_scalar(current):
            continue
        if not isinstance(current, (dict, list)):
            raise TypeError("Value was not JSON-compatible.")

        identity = id(current)
        if expanded:
            active.remove(identity)
            if isinstance(current, dict):
                frozen_items: dict[str, Any] = {}
                for key, item in current.items():
                    if not isinstance(key, str):
                        raise TypeError("JSON object key was not a string.")
                    frozen_items[key] = (
                        item if _is_json_scalar(item) else completed[id(item)]
                    )
                completed[identity] = _FrozenJsonDict(frozen_items)
            else:
                completed[identity] = _FrozenJsonList(
                    item if _is_json_scalar(item) else completed[id(item)]
                    for item in current
                )
            continue

        if identity in active or identity in completed:
            raise ValueError("JSON tree contained a cycle or shared container.")
        active.add(identity)
        stack.append((current, True))
        children = list(current.values()) if isinstance(current, dict) else list(current)
        for child in reversed(children):
            if _is_json_scalar(child):
                continue
            if not isinstance(child, (dict, list)):
                raise TypeError("Value was not JSON-compatible.")
            stack.append((child, False))

    return completed[id(value)]


@dataclass(frozen=True)
class UpstreamServerConfig:
    alias: str
    transport: str
    enabled: bool = True
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    authorization_env: str | None = None
    include_tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    expose_mode: str = "direct"
    pinned_tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    tool_policy: dict[str, str] = field(default_factory=dict)
    timeout_ms: int = DEFAULT_TIMEOUT_MS


@dataclass(frozen=True)
class UpstreamConfigSnapshot:
    """Configuration, enable state, and allowlists fixed before Runtime creation."""

    configs: tuple[UpstreamServerConfig, ...] = ()
    custom_synonyms: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    credential_policy: str = DEFAULT_CREDENTIAL_POLICY
    source: str | None = None

    @classmethod
    def empty(cls) -> "UpstreamConfigSnapshot":
        return cls()


@dataclass(frozen=True)
class UpstreamTool:
    public_name: str
    remote_name: str
    raw_definition: Mapping[str, Any]
    public_definition: Mapping[str, Any]
    effective_risk: str
    public_schema_digest: str
    raw_schema_digest: str | None


@dataclass(frozen=True)
class UpstreamRegistryState:
    """One fixed Runtime-generation snapshot of upstream tools and clients."""

    all_tools: Mapping[str, UpstreamTool]
    direct_tool_names: tuple[str, ...]
    catalog: Mapping[str, UpstreamToolCatalogEntry]
    search_index: SearchBackend | None
    clients: Mapping[str, BaseUpstreamClient]


@dataclass(frozen=True)
class UpstreamStatusTemplate:
    alias: str
    transport: str
    enabled: bool
    initialized: bool
    tool_count: int
    error: Mapping[str, Any] | None
    target: str | None


@dataclass(frozen=True)
class UpstreamCatalogTemplate:
    """Immutable discovery metadata shared by Runtimes of one config revision.

    The template intentionally contains no live client, remote MCP session ID,
    ResultStore, bearer token, or principal/workspace state.
    """

    configs: tuple[UpstreamServerConfig, ...]
    custom_synonyms: Mapping[str, tuple[str, ...]]
    registry_state: UpstreamRegistryState
    statuses: tuple[UpstreamStatusTemplate, ...]
    reserved_names: frozenset[str]


@dataclass
class UpstreamStatus:
    alias: str
    transport: str
    enabled: bool
    initialized: bool = False
    tool_count: int = 0
    error: dict[str, Any] | None = None
    target: str | None = None

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "alias": self.alias,
            "transport": self.transport,
            "enabled": self.enabled,
            "initialized": self.initialized,
            "tool_count": self.tool_count,
        }
        if self.target is not None:
            result["target"] = self.target
        if self.error is not None:
            result["error"] = _bounded_status_error(self.error)
        return result


class BaseUpstreamClient:
    def __init__(
        self,
        config: UpstreamServerConfig,
        protocol_version: str,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.config = config
        self.protocol_version = protocol_version
        self.secret_resolver = secret_resolver
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._lifecycle_condition = threading.Condition(threading.Lock())
        self._lifecycle_closed = False
        self._active_call_leases = 0

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "coding-tools-mcp-upstream", "version": "0"},
            },
        )
        self.notify("notifications/initialized", {})

    def list_tools(self) -> list[dict[str, Any]]:
        response = self.request("tools/list", {})
        tools = response.get("tools") if isinstance(response, dict) else None
        if not isinstance(tools, list):
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream tools/list response did not include a tools list.",
                category="protocol",
            )
        if not all(isinstance(tool, dict) for tool in tools):
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream tools/list contained a non-object tool definition.",
                category="protocol",
            )
        # The response was freshly decoded for this request. Keep the list
        # container independent without recursively traversing untrusted Schema
        # trees before the sanitizer and iterative snapshot freezer run.
        return list(tools)

    def call_tool_raw(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._acquire_call_lease()
        try:
            response = self.request("tools/call", {"name": name, "arguments": arguments})
            if not isinstance(response, dict):
                raise UpstreamError(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "Upstream tools/call result was not an object.",
                    category="protocol",
                )
            return response
        finally:
            self._release_call_lease()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible raw call followed by normalization only."""
        return normalize_tool_result(self.call_tool_raw(name, arguments))

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        with self._lifecycle_condition:
            if self._lifecycle_closed:
                return
            self._lifecycle_closed = True
            while self._active_call_leases:
                self._lifecycle_condition.wait()
        self._close_transport()

    def _close_transport(self) -> None:
        return None

    @property
    def closed(self) -> bool:
        with self._lifecycle_condition:
            return self._lifecycle_closed

    def _acquire_call_lease(self) -> None:
        with self._lifecycle_condition:
            if self._lifecycle_closed:
                raise UpstreamError(
                    "UPSTREAM_NOT_AVAILABLE",
                    "Upstream MCP client is closed.",
                    retryable=True,
                )
            self._active_call_leases += 1

    def _release_call_lease(self) -> None:
        with self._lifecycle_condition:
            self._active_call_leases = max(0, self._active_call_leases - 1)
            if self._active_call_leases == 0:
                self._lifecycle_condition.notify_all()

    def _next_request_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id


def _rpc_result(response: Any, request_id: int, method: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream response was not a JSON object.",
            category="protocol",
            details={"method": method},
        )
    if response.get("jsonrpc") != "2.0":
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream response did not use JSON-RPC 2.0.",
            category="protocol",
            details={"method": method},
        )
    response_id = response.get("id")
    if type(response_id) is not int or response_id != request_id:
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream response id did not match the request id.",
            category="protocol",
            details={"method": method},
        )
    has_result = "result" in response
    has_error = "error" in response
    if has_result == has_error:
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream response must contain exactly one of result or error.",
            category="protocol",
            details={"method": method},
        )
    if has_error:
        error = response.get("error")
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream JSON-RPC error envelope was invalid.",
                category="protocol",
                details={"method": method},
            )
        try:
            rpc_error = _clone_json_tree(
                error,
                max_depth=MAX_UPSTREAM_RESULT_DEPTH,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream JSON-RPC error exceeded the supported structure.",
                category="protocol",
                details={"method": method},
            ) from exc
        if not isinstance(rpc_error, dict):
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream JSON-RPC error envelope was invalid.",
                category="protocol",
                details={"method": method},
            )
        raise UpstreamError(
            "UPSTREAM_RPC_ERROR",
            error["message"],
            category="upstream",
            details={"method": method, "rpc_error": rpc_error},
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream response result was not an object.",
            category="protocol",
            details={"method": method},
        )
    return result


class HttpUpstreamClient(BaseUpstreamClient):
    def __init__(
        self,
        config: UpstreamServerConfig,
        protocol_version: str,
        secret_resolver: Callable[[str], str] | None = None,
        *,
        resilience_coordinator: UpstreamResilienceCoordinator | None = None,
    ) -> None:
        super().__init__(config, protocol_version, secret_resolver=secret_resolver)
        if not config.url:
            raise UpstreamConfigError(
                f"Upstream {config.alias!r} requires url for streamable_http transport."
            )
        self.url = config.url
        self.session_id: str | None = None
        self._session_lock = threading.Lock()
        self.remote_delete_success_total = 0
        self.remote_delete_failure_total = 0
        self._resilience = resilience_coordinator or DEFAULT_UPSTREAM_RESILIENCE_COORDINATOR
        self._resilience_key = f"{config.alias}|{safe_target(config) or 'streamable_http'}"
        self._state_condition = threading.Condition(threading.Lock())
        self._transport_state = UpstreamClientState.NEW
        self._last_state_error: UpstreamError | None = None

    @property
    def transport_state(self) -> UpstreamClientState:
        with self._state_condition:
            return self._transport_state

    def resilience_payload(self) -> dict[str, Any]:
        shared = self._resilience.snapshot(self._resilience_key)
        with self._session_lock:
            has_session = self.session_id is not None
        return {
            "state": self.transport_state.value,
            "active_client_sessions": 1 if has_session else 0,
            "initializing_count": shared.initializing_count,
            "consecutive_failures": shared.consecutive_failures,
            "next_retry_in_ms": shared.next_retry_in_ms,
            "last_error_code": shared.last_error_code,
            "remote_delete_success_total": self.remote_delete_success_total,
            "remote_delete_failure_total": self.remote_delete_failure_total,
            "circuit_open_total": shared.circuit_open_total,
        }

    def initialize(self) -> None:
        self._acquire_call_lease()
        try:
            self._initialize_with_resilience()
        finally:
            self._release_call_lease()

    def _initialize_with_resilience(self) -> None:
        while True:
            with self._state_condition:
                if self.closed:
                    self._transport_state = UpstreamClientState.CLOSED
                    raise UpstreamError(
                        "UPSTREAM_NOT_AVAILABLE",
                        "Upstream MCP client is closed.",
                        retryable=True,
                    )
                if self._transport_state == UpstreamClientState.READY:
                    return
                if self._transport_state == UpstreamClientState.INITIALIZING:
                    self._state_condition.wait()
                    continue
                if self._transport_state == UpstreamClientState.SUSPECT and self._last_state_error is not None:
                    raise self._last_state_error
                probe = self._transport_state == UpstreamClientState.BACKING_OFF
                self._transport_state = UpstreamClientState.INITIALIZING
                break
        try:
            with self._resilience.initialization_slot(self._resilience_key, probe=probe):
                BaseUpstreamClient.initialize(self)
        except UpstreamResilienceGateError as exc:
            error = UpstreamError(
                exc.code,
                exc.message,
                retryable=True,
                details={"retry_after_ms": exc.retry_after_ms},
            )
            with self._state_condition:
                self._transport_state = UpstreamClientState.BACKING_OFF
                self._last_state_error = error
                self._state_condition.notify_all()
            raise error from exc
        except UpstreamError as exc:
            self._record_failure_state(exc, during_initialize=True)
            raise
        except OSError as exc:
            error = UpstreamError(
                "UPSTREAM_CONNECTION_FAILED",
                "Could not connect to upstream MCP server.",
                retryable=True,
            )
            self._record_failure_state(error, during_initialize=True)
            raise error from exc
        else:
            if self.closed:
                with self._state_condition:
                    self._transport_state = UpstreamClientState.CLOSED
                    self._state_condition.notify_all()
                raise UpstreamError(
                    "UPSTREAM_NOT_AVAILABLE",
                    "Upstream MCP client closed while initialization was completing.",
                    retryable=True,
                )
            self._resilience.record_success(self._resilience_key)
            with self._state_condition:
                self._transport_state = UpstreamClientState.READY
                self._last_state_error = None
                self._state_condition.notify_all()

    def call_tool_raw(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Re-establish transport state only before the current call is sent. An
        # ambiguous failure from this tools/call is returned to the caller and
        # is never replayed automatically.
        if self.transport_state != UpstreamClientState.READY:
            self.initialize()
        try:
            return BaseUpstreamClient.call_tool_raw(self, name, arguments)
        except UpstreamError as exc:
            self._record_failure_state(exc, during_initialize=False)
            raise

    def _record_failure_state(self, exc: UpstreamError, *, during_initialize: bool) -> None:
        status = exc.details.get("status") if isinstance(exc.details, dict) else None
        stale_session = status in {404, 410}
        auth_failure = status in {401, 403}
        ambiguous_transport = (
            exc.code in {"UPSTREAM_TIMEOUT", "UPSTREAM_CONNECTION_FAILED", "UPSTREAM_DISCONNECTED"}
            or (isinstance(status, int) and 500 <= status < 600)
        )
        if stale_session:
            with self._session_lock:
                self.session_id = None
            next_state = UpstreamClientState.NEW
        elif ambiguous_transport:
            with self._session_lock:
                self.session_id = None
            self._resilience.record_failure(self._resilience_key, exc.code)
            next_state = UpstreamClientState.BACKING_OFF
        elif auth_failure or exc.category == "protocol":
            next_state = UpstreamClientState.SUSPECT
        elif during_initialize and exc.retryable:
            self._resilience.record_failure(self._resilience_key, exc.code)
            next_state = UpstreamClientState.BACKING_OFF
        else:
            next_state = UpstreamClientState.SUSPECT
        with self._state_condition:
            if self.closed:
                self._transport_state = UpstreamClientState.CLOSED
            else:
                self._transport_state = next_state
            self._last_state_error = exc
            self._state_condition.notify_all()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_request_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        response = self._send(payload, expect_response=True)
        return _rpc_result(response, request_id, method)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload, expect_response=False)

    def _send(self, payload: dict[str, Any], *, expect_response: bool) -> dict[str, Any] | None:
        try:
            data = strict_json_bytes(
                payload,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream request was not valid standard JSON.",
                category="protocol",
            ) from exc
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **_without_protected_headers(self.config.headers),
        }
        with self._session_lock:
            current_session_id = self.session_id
        if current_session_id:
            headers["Mcp-Session-Id"] = current_session_id
            headers["MCP-Protocol-Version"] = self.protocol_version
        token = os.environ.get(self.config.authorization_env) if self.config.authorization_env else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        timeout_s = max(self.config.timeout_ms, 1) / 1000
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    with self._session_lock:
                        if not self.closed:
                            self.session_id = session_id
                if not expect_response or response.status in {202, 204}:
                    return None
                raw = _read_bounded_response(response)
                expected_id = payload.get("id")
                return decode_http_rpc_response(
                    raw,
                    response.headers.get("Content-Type", ""),
                    expected_id=expected_id if type(expected_id) is int else None,
                )
        except urllib.error.HTTPError as exc:
            # An HTTP error body is diagnostic input only. It must never turn a
            # non-2xx response into a successful JSON-RPC result, even when the
            # body happens to contain a valid envelope.
            try:
                exc.read(MAX_RESPONSE_BYTES + 1)
            except OSError:
                pass
            raise UpstreamError(
                "UPSTREAM_HTTP_ERROR",
                f"Upstream MCP server returned HTTP {exc.code}.",
                category="upstream",
                retryable=500 <= exc.code < 600,
                details={"status": exc.code},
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamError(
                "UPSTREAM_TIMEOUT",
                "Timed out waiting for upstream MCP server.",
                retryable=True,
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise UpstreamError(
                    "UPSTREAM_TIMEOUT",
                    "Timed out waiting for upstream MCP server.",
                    retryable=True,
                ) from exc
            raise UpstreamError(
                "UPSTREAM_CONNECTION_FAILED",
                "Could not connect to upstream MCP server.",
                retryable=True,
            ) from exc
        except (RemoteDisconnected, ConnectionError, BrokenPipeError, ConnectionResetError) as exc:
            raise UpstreamError(
                "UPSTREAM_DISCONNECTED",
                "Upstream MCP server disconnected.",
                retryable=True,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream returned invalid JSON.",
                category="protocol",
            ) from exc

    def _close_transport(self) -> None:
        """Best-effort bounded Streamable HTTP session termination.

        DELETE is idempotent from the local client's perspective: the Session
        ID is detached before network I/O, so repeated close() calls cannot
        issue duplicate remote deletes. 404/410 are treated as already closed.
        """

        with self._session_lock:
            session_id = self.session_id
            self.session_id = None
        if not session_id:
            return
        headers = {
            "Accept": "application/json, text/event-stream",
            **_without_protected_headers(self.config.headers),
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": self.protocol_version,
        }
        token = os.environ.get(self.config.authorization_env) if self.config.authorization_env else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.url, headers=headers, method="DELETE")
        timeout_s = min(
            max(self.config.timeout_ms, 1) / 1000,
            UPSTREAM_CLOSE_TIMEOUT_SECONDS,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                if response.status in {200, 202, 204}:
                    self.remote_delete_success_total += 1
                else:
                    self.remote_delete_failure_total += 1
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 410}:
                self.remote_delete_success_total += 1
            else:
                self.remote_delete_failure_total += 1
        except (
            TimeoutError,
            socket.timeout,
            urllib.error.URLError,
            RemoteDisconnected,
            ConnectionError,
            BrokenPipeError,
            ConnectionResetError,
            OSError,
        ):
            self.remote_delete_failure_total += 1

    def close(self) -> None:
        super().close()
        with self._state_condition:
            self._transport_state = UpstreamClientState.CLOSED
            self._state_condition.notify_all()


class StdioUpstreamClient(BaseUpstreamClient):
    def __init__(
        self,
        config: UpstreamServerConfig,
        protocol_version: str,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(config, protocol_version, secret_resolver=secret_resolver)
        if not config.command:
            raise UpstreamConfigError(
                f"Upstream {config.alias!r} requires command for stdio transport."
            )
        self._lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any] | UpstreamError] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=500)
        self._stderr_lock = threading.Lock()
        env = base_upstream_environment()
        env.update(resolve_env_config(config.env, secret_resolver=self.secret_resolver))
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.process = subprocess.Popen(
            [config.command, *config.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        atexit.register(self.close)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_request_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        with self._lock:
            self._write(payload)
            deadline = time.monotonic() + max(self.config.timeout_ms, 1) / 1000
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamError(
                        "UPSTREAM_TIMEOUT",
                        "Timed out waiting for upstream MCP server.",
                        retryable=True,
                    )
                if self.process.poll() is not None and self._responses.empty():
                    raise self._process_exited_error()
                try:
                    response = self._responses.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                if isinstance(response, UpstreamError):
                    raise response
                return _rpc_result(response, request_id, method)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        with self._lock:
            self._write(payload)

    def _close_transport(self) -> None:
        process = getattr(self, "process", None)
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                process.terminate()
            process.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process.poll() is not None:
            raise self._process_exited_error()
        if self.process.stdin is None:
            raise UpstreamError(
                "UPSTREAM_DISCONNECTED",
                "Upstream MCP stdin is closed.",
                retryable=True,
            )
        try:
            encoded = strict_json_bytes(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ) + b"\n"
        except (TypeError, ValueError) as exc:
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream request was not valid standard JSON.",
                category="protocol",
            ) from exc

        try:
            binary_stdin = getattr(self.process.stdin, "buffer", None)
            if binary_stdin is not None:
                binary_stdin.write(encoded)
                binary_stdin.flush()
            else:
                self.process.stdin.write(encoded.decode("utf-8"))
                self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise UpstreamError(
                "UPSTREAM_DISCONNECTED",
                "Upstream MCP stdio process disconnected.",
                retryable=True,
            ) from exc

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            return
        binary_stdout = getattr(self.process.stdout, "buffer", None)
        if binary_stdout is not None:
            self._read_stdout_binary(binary_stdout)
            return
        self._read_stdout_text(self.process.stdout)

    def _read_stdout_binary(self, stream: Any) -> None:
        try:
            while True:
                raw_line = stream.readline(MAX_RESPONSE_BYTES + 3)
                if not raw_line:
                    return
                has_newline = raw_line.endswith(b"\n")
                payload = raw_line[:-1] if has_newline else raw_line
                if payload.endswith(b"\r"):
                    payload = payload[:-1]
                if len(payload) > MAX_RESPONSE_BYTES:
                    if not has_newline:
                        self._drain_binary_line(stream)
                    self._responses.put(
                        UpstreamError(
                            "UPSTREAM_RESPONSE_TOO_LARGE",
                            "Upstream response exceeded the maximum supported size.",
                            category="protocol",
                        )
                    )
                    continue
                self._queue_stdio_response(payload)
        except (OSError, ValueError):
            self._responses.put(
                UpstreamError(
                    "UPSTREAM_DISCONNECTED",
                    "Upstream MCP stdio process disconnected.",
                    retryable=True,
                )
            )

    @staticmethod
    def _drain_binary_line(stream: Any) -> None:
        while True:
            chunk = stream.readline(65_536)
            if not chunk or chunk.endswith(b"\n"):
                return

    def _read_stdout_text(self, stream: Any) -> None:
        try:
            for raw_line in stream:
                line = raw_line.rstrip("\r\n")
                try:
                    encoded = line.encode("utf-8")
                except UnicodeError:
                    self._responses.put(
                        UpstreamError(
                            "UPSTREAM_PROTOCOL_ERROR",
                            "Upstream stdio returned non-UTF-8 output.",
                            category="protocol",
                        )
                    )
                    continue
                if len(encoded) > MAX_RESPONSE_BYTES:
                    self._responses.put(
                        UpstreamError(
                            "UPSTREAM_RESPONSE_TOO_LARGE",
                            "Upstream response exceeded the maximum supported size.",
                            category="protocol",
                        )
                    )
                    continue
                self._queue_stdio_response(line)
        except UnicodeError:
            self._responses.put(
                UpstreamError(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "Upstream stdio returned non-UTF-8 output.",
                    category="protocol",
                )
            )

    def _queue_stdio_response(self, raw_line: str | bytes) -> None:
        line = raw_line.strip()
        if not line:
            return
        try:
            parsed = strict_json_loads(line)
        except (UnicodeError, ValueError):
            self._responses.put(
                UpstreamError(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "Upstream stdio returned invalid JSON.",
                    category="protocol",
                )
            )
            return
        if not isinstance(parsed, dict):
            self._responses.put(
                UpstreamError(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "Upstream stdio response was not a JSON object.",
                    category="protocol",
                )
            )
            return
        if "id" not in parsed and isinstance(parsed.get("method"), str):
            return
        self._responses.put(parsed)

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for line in self.process.stderr:
            item = line.rstrip("\r\n")[:500]
            with self._stderr_lock:
                self._stderr_lines.append(item)

    def _process_exited_error(self) -> UpstreamError:
        with self._stderr_lock:
            stderr_tail = list(self._stderr_lines)[-5:]
        return UpstreamError(
            "UPSTREAM_PROCESS_EXITED",
            "Upstream MCP stdio process exited.",
            retryable=True,
            details={"returncode": self.process.returncode, "stderr_tail": stderr_tail},
        )


class UpstreamManager:
    """Per-Runtime upstream clients with an immutable discovered tool snapshot."""

    def __init__(
        self,
        configs: Iterable[UpstreamServerConfig],
        *,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        secret_resolver: Callable[[str], str] | None = None,
        reserved_names: Collection[str] = (),
        custom_synonyms: Mapping[str, Sequence[str]] | None = None,
        result_store: ResultStore | None = None,
        catalog_template: UpstreamCatalogTemplate | None = None,
    ) -> None:
        self.protocol_version = protocol_version
        self.secret_resolver = secret_resolver
        self.configs = tuple(configs)
        self.custom_synonyms = {
            str(key): tuple(str(value) for value in values)
            for key, values in (custom_synonyms or {}).items()
        }
        self.result_store = result_store or ResultStore()
        self.statuses: dict[str, UpstreamStatus] = {}
        self._state = _registry_state()
        self._clients: dict[str, BaseUpstreamClient] = {}
        self._client_locks = {config.alias: threading.Lock() for config in self.configs}
        self._config_by_alias = {config.alias: config for config in self.configs}
        self._lifecycle_condition = threading.Condition(threading.Lock())
        self._active_call_leases = 0
        self._closed = False
        try:
            if catalog_template is None:
                self._initialize_configs(frozenset(reserved_names))
            else:
                self._initialize_from_template(catalog_template, frozenset(reserved_names))
        except BaseException:
            self.close()
            raise

    @classmethod
    def empty(
        cls,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        *,
        reserved_names: Collection[str] = (),
        result_store: ResultStore | None = None,
    ) -> "UpstreamManager":
        return cls(
            (),
            protocol_version=protocol_version,
            reserved_names=reserved_names,
            result_store=result_store,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: UpstreamConfigSnapshot,
        *,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        secret_resolver: Callable[[str], str] | None = None,
        reserved_names: Collection[str] = (),
        result_store: ResultStore | None = None,
    ) -> "UpstreamManager":
        return cls(
            snapshot.configs,
            protocol_version=protocol_version,
            secret_resolver=secret_resolver,
            reserved_names=reserved_names,
            custom_synonyms=snapshot.custom_synonyms,
            result_store=result_store,
        )

    @classmethod
    def from_template(
        cls,
        template: UpstreamCatalogTemplate,
        *,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        secret_resolver: Callable[[str], str] | None = None,
        result_store: ResultStore | None = None,
    ) -> "UpstreamManager":
        return cls(
            template.configs,
            protocol_version=protocol_version,
            secret_resolver=secret_resolver,
            reserved_names=template.reserved_names,
            custom_synonyms=template.custom_synonyms,
            result_store=result_store,
            catalog_template=template,
        )

    @property
    def state(self) -> UpstreamRegistryState:
        return self._state

    def tool_definitions(self) -> list[dict[str, Any]]:
        state = self._state
        return [
            copy.deepcopy(state.all_tools[name].public_definition)
            for name in state.direct_tool_names
        ]

    def tool_names(self) -> list[str]:
        state = self._state
        return list(state.direct_tool_names)

    def has_tool(self, name: str) -> bool:
        state = self._state
        return name in state.all_tools

    def catalog_entries(self) -> tuple[UpstreamToolCatalogEntry, ...]:
        state = self._state
        return tuple(state.catalog[name] for name in sorted(state.catalog))

    def catalog_entry(self, name: str) -> UpstreamToolCatalogEntry | None:
        state = self._state
        return state.catalog.get(name)

    def search_catalog(
        self,
        query: str,
        filters: ToolSearchFilters | None = None,
    ) -> list[ToolSearchResult]:
        state = self._state
        if state.search_index is None:
            return []
        return state.search_index.search(query, filters)

    def describe_catalog_tool(self, name: str) -> dict[str, Any] | None:
        state = self._state
        entry = state.catalog.get(name)
        tool = state.all_tools.get(name)
        if entry is None or tool is None:
            return None
        return {
            "name": entry.public_name,
            "server": entry.server_alias,
            "remote_name": entry.remote_name,
            "title": entry.title,
            "description": entry.description,
            "tags": list(entry.tags),
            "risk": entry.effective_risk,
            "schema_digest": entry.public_schema_digest,
            "definition": copy.deepcopy(tool.public_definition),
        }

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        result_owner: str | None = None,
        store_overflow: bool = False,
    ) -> dict[str, Any]:
        alias, _separator, _remote = name.partition("__")
        if not self._acquire_call_lease():
            return upstream_error_result(
                "UPSTREAM_NOT_AVAILABLE",
                "Upstream Gateway is closed.",
                retryable=True,
                alias=alias,
                tool_name=name,
            )
        try:
            return self._call_tool_with_lease(
                name,
                arguments,
                result_owner=result_owner,
                store_overflow=store_overflow,
            )
        finally:
            self._release_call_lease()

    def _call_tool_with_lease(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        result_owner: str | None,
        store_overflow: bool,
    ) -> dict[str, Any]:
        state = self._state
        tool = state.all_tools.get(name)
        if tool is None:
            return upstream_error_result(
                "UPSTREAM_TOOL_NOT_FOUND",
                f"Unknown upstream tool: {name}",
                category="validation",
            )
        alias, _separator, _remote = name.partition("__")
        try:
            client = self._get_or_create_client(alias)
            raw_result = client.call_tool_raw(tool.remote_name, arguments or {})
            normalized = normalize_tool_result(raw_result)
            serialized = result_json_bytes(normalized)
            handle: str | None = None
            if store_overflow and result_owner and len(serialized) > RESULT_INLINE_MAX:
                handle = self.result_store.store(
                    serialized.decode("utf-8"),
                    owner=result_owner,
                    server_alias=alias,
                )
            required_metadata = (
                {
                    "_result_handle": handle,
                    "_result_fetch_tool": "upstream_result_fetch",
                }
                if handle is not None
                else None
            )
            budgeted = budget_tool_result(
                normalized,
                required_structured_content=required_metadata,
            )
            if len(result_json_bytes(budgeted)) > RESULT_INLINE_MAX:
                raise UpstreamError(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "Budgeted upstream result exceeded the final envelope limit.",
                    category="protocol",
                )
            return budgeted
        except UpstreamError as exc:
            return budget_tool_result(
                upstream_error_result(
                    exc.code,
                    exc.message,
                    category=exc.category,
                    retryable=exc.retryable,
                    details=exc.details,
                    alias=alias,
                    tool_name=name,
                )
            )
        except OSError:
            return budget_tool_result(
                upstream_error_result(
                    "UPSTREAM_DISCONNECTED",
                    "Upstream MCP server disconnected.",
                    retryable=True,
                    alias=alias,
                    tool_name=name,
                )
            )
        except (RecursionError, TypeError, ValueError):
            return budget_tool_result(
                upstream_error_result(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "Upstream tool result was not valid standard JSON.",
                    category="protocol",
                    alias=alias,
                    tool_name=name,
                )
            )

    def status_payload(self) -> dict[str, Any]:
        state = self._state
        statuses: list[dict[str, Any]] = []
        for alias in sorted(self.statuses):
            payload = self.statuses[alias].payload()
            client = self._clients.get(alias)
            if isinstance(client, HttpUpstreamClient):
                payload.update(client.resilience_payload())
            elif self.statuses[alias].enabled:
                payload.update(
                    {
                        "state": UpstreamClientState.NEW.value,
                        "active_client_sessions": 0,
                        "initializing_count": 0,
                        "consecutive_failures": 0,
                        "next_retry_in_ms": 0,
                        "last_error_code": None,
                        "remote_delete_success_total": 0,
                        "remote_delete_failure_total": 0,
                        "circuit_open_total": 0,
                    }
                )
            statuses.append(payload)
        return {
            "enabled": any(status.enabled for status in self.statuses.values()),
            "server_count": len(self.statuses),
            "initialized_count": sum(1 for status in self.statuses.values() if status.initialized),
            "tool_count": len(state.direct_tool_names),
            "catalog_tool_count": len(state.catalog),
            "exposure_report": upstream_exposure_report(state),
            "snapshot_immutable": True,
            "remote_capability_boundary": "upstream_server",
            "servers": statuses,
        }

    def close(self) -> None:
        with self._lifecycle_condition:
            if self._closed:
                return
            self._closed = True
            while self._active_call_leases:
                self._lifecycle_condition.wait()
            clients = tuple(self._clients.values())
        for client in clients:
            client.close()
        self.result_store.clear()

    def live_client_count(self) -> int:
        return len(self._clients)

    def _get_or_create_client(self, alias: str) -> BaseUpstreamClient:
        existing = self._clients.get(alias)
        if existing is not None:
            return existing
        config = self._config_by_alias.get(alias)
        lock = self._client_locks.get(alias)
        if config is None or lock is None or not config.enabled:
            raise UpstreamError("UPSTREAM_NOT_AVAILABLE", f"Upstream {alias!r} is not available.", retryable=True)
        with lock:
            existing = self._clients.get(alias)
            if existing is not None:
                return existing
            with self._lifecycle_condition:
                if self._closed:
                    raise UpstreamError(
                        "UPSTREAM_NOT_AVAILABLE",
                        "Upstream Gateway is closed.",
                        retryable=True,
                    )
            client = build_client(
                _copy_upstream_config(config),
                self.protocol_version,
                secret_resolver=self.secret_resolver,
            )
            try:
                client.initialize()
            except BaseException:
                client.close()
                raise
            with self._lifecycle_condition:
                if self._closed:
                    client.close()
                    raise UpstreamError(
                        "UPSTREAM_NOT_AVAILABLE",
                        "Upstream Gateway closed during client initialization.",
                        retryable=True,
                    )
                self._clients[alias] = client
            return client

    def _acquire_call_lease(self) -> bool:
        with self._lifecycle_condition:
            if self._closed:
                return False
            self._active_call_leases += 1
            return True

    def _release_call_lease(self) -> None:
        with self._lifecycle_condition:
            self._active_call_leases = max(0, self._active_call_leases - 1)
            if self._active_call_leases == 0:
                self._lifecycle_condition.notify_all()

    def _initialize_from_template(
        self,
        template: UpstreamCatalogTemplate,
        reserved_names: frozenset[str],
    ) -> None:
        if reserved_names != template.reserved_names:
            raise UpstreamConfigError("Upstream catalog template reserved-name contract does not match this Runtime.")
        if tuple(config.alias for config in self.configs) != tuple(
            config.alias for config in template.configs
        ):
            raise UpstreamConfigError(
                "Upstream catalog template configuration does not match this Runtime."
            )
        self.statuses = {
            item.alias: UpstreamStatus(
                alias=item.alias,
                transport=item.transport,
                enabled=item.enabled,
                initialized=item.initialized,
                tool_count=item.tool_count,
                error=copy.deepcopy(dict(item.error)) if item.error else None,
                target=item.target,
            )
            for item in template.statuses
        }
        state = template.registry_state
        self._state = _registry_state(
            all_tools=state.all_tools,
            direct_tool_names=state.direct_tool_names,
            catalog=state.catalog,
            search_index=state.search_index,
            clients={},
        )

    def _initialize_configs(self, reserved_names: frozenset[str]) -> None:
        seen_public_names = set(reserved_names)
        ordered_names: list[str] = []
        next_tools: dict[str, UpstreamTool] = {}
        next_catalog: dict[str, UpstreamToolCatalogEntry] = {}
        next_clients: dict[str, BaseUpstreamClient] = {}
        try:
            for config in self.configs:
                status = UpstreamStatus(
                    alias=config.alias,
                    transport=config.transport,
                    enabled=config.enabled,
                    target=safe_target(config),
                )
                self.statuses[config.alias] = status
                if not config.enabled:
                    continue
                client: BaseUpstreamClient | None = None
                try:
                    client = build_client(
                        config,
                        self.protocol_version,
                        secret_resolver=self.secret_resolver,
                    )
                    client.initialize()
                    raw_tools = filter_tools(client.list_tools(), config)
                    registered: list[UpstreamTool] = []
                    for raw_tool in raw_tools:
                        remote_name = raw_tool.get("name")
                        if not isinstance(remote_name, str) or not remote_name:
                            raise UpstreamError(
                                "UPSTREAM_PROTOCOL_ERROR",
                                "Upstream tool definition had no valid name.",
                                category="protocol",
                            )
                        public_name = namespaced_tool_name(config.alias, remote_name)
                        if public_name in seen_public_names:
                            raise UpstreamConfigError(
                                f"Upstream tool namespace collision: {public_name!r}."
                            )
                        seen_public_names.add(public_name)
                        try:
                            raw_definition = namespaced_tool_definition(
                                public_name,
                                raw_tool,
                            )
                            public_definition = sanitize_definition(raw_definition)
                            frozen_raw_definition = _freeze_json(raw_definition)
                            frozen_public_definition = _freeze_json(public_definition)
                        except (RecursionError, TypeError, ValueError) as exc:
                            raise UpstreamError(
                                "UPSTREAM_PROTOCOL_ERROR",
                                "Upstream tool definition was not valid bounded JSON metadata.",
                                category="protocol",
                            ) from exc
                        registered.append(
                            UpstreamTool(
                                public_name=public_name,
                                remote_name=remote_name,
                                raw_definition=frozen_raw_definition,
                                public_definition=frozen_public_definition,
                                effective_risk=classify_risk(
                                    raw_tool, config.tool_policy, remote_name
                                ),
                                public_schema_digest=schema_digest(public_definition),
                                raw_schema_digest=raw_schema_digest(raw_definition),
                            )
                        )
                    next_clients[config.alias] = client
                    for tool in registered:
                        next_tools[tool.public_name] = tool
                        next_catalog[tool.public_name] = tool_catalog_entry(tool, config)
                        if (
                            config.expose_mode == "direct"
                            or tool.remote_name in config.pinned_tools
                        ):
                            ordered_names.append(tool.public_name)
                    status.initialized = True
                    status.tool_count = len(registered)
                except UpstreamConfigError:
                    if client is not None:
                        client.close()
                    raise
                except (OSError, UpstreamError) as exc:
                    if client is not None:
                        client.close()
                    status.error = error_payload(exc)
        except BaseException:
            for client in next_clients.values():
                client.close()
            raise
        search_index: SearchBackend | None = None
        if next_catalog:
            index = CatalogSearchIndex(self.custom_synonyms)
            index.build(next_catalog)
            search_index = index
        self._state = _registry_state(
            all_tools=next_tools,
            direct_tool_names=tuple(ordered_names),
            catalog=next_catalog,
            search_index=search_index,
            clients=next_clients,
        )
        self._clients = dict(next_clients)


def _copy_upstream_config(
    config: UpstreamServerConfig,
    *,
    freeze_mappings: bool = False,
) -> UpstreamServerConfig:
    env: Any = copy.deepcopy(dict(config.env))
    headers: Any = copy.deepcopy(dict(config.headers))
    tool_policy: Any = copy.deepcopy(dict(config.tool_policy))
    if freeze_mappings:
        env = _freeze_json(env)
        headers = _freeze_json(headers)
        tool_policy = _freeze_json(tool_policy)
    return UpstreamServerConfig(
        alias=config.alias,
        transport=config.transport,
        enabled=config.enabled,
        url=config.url,
        command=config.command,
        args=tuple(config.args),
        env=env,
        headers=headers,
        authorization_env=config.authorization_env,
        include_tools=tuple(config.include_tools),
        exclude_tools=tuple(config.exclude_tools),
        expose_mode=config.expose_mode,
        pinned_tools=tuple(config.pinned_tools),
        tags=tuple(config.tags),
        tool_policy=tool_policy,
        timeout_ms=config.timeout_ms,
    )


def build_upstream_catalog_template(
    snapshot: UpstreamConfigSnapshot,
    *,
    protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    secret_resolver: Callable[[str], str] | None = None,
    reserved_names: Collection[str] = (),
) -> UpstreamCatalogTemplate:
    """Discover one immutable catalog and close all temporary live sessions."""

    frozen_reserved_names = frozenset(reserved_names)
    discovery = UpstreamManager.from_snapshot(
        snapshot,
        protocol_version=protocol_version,
        secret_resolver=secret_resolver,
        reserved_names=frozen_reserved_names,
    )
    try:
        state = discovery.state
        template_state = _registry_state(
            all_tools=state.all_tools,
            direct_tool_names=state.direct_tool_names,
            catalog=state.catalog,
            search_index=state.search_index,
            clients={},
        )
        status_templates: list[UpstreamStatusTemplate] = []
        for alias in sorted(discovery.statuses):
            status = discovery.statuses[alias]
            frozen_error: Mapping[str, Any] | None = None
            if status.error is not None:
                candidate = _freeze_json(copy.deepcopy(status.error))
                if not isinstance(candidate, Mapping):
                    raise UpstreamConfigError("Upstream status error was not a JSON object.")
                frozen_error = candidate
            status_templates.append(
                UpstreamStatusTemplate(
                    alias=status.alias,
                    transport=status.transport,
                    enabled=status.enabled,
                    initialized=status.initialized,
                    tool_count=status.tool_count,
                    error=frozen_error,
                    target=status.target,
                )
            )
        cloned_configs = tuple(
            _copy_upstream_config(config, freeze_mappings=True)
            for config in snapshot.configs
        )
        return UpstreamCatalogTemplate(
            configs=cloned_configs,
            custom_synonyms=MappingProxyType(
                {
                    str(key): tuple(str(item) for item in values)
                    for key, values in snapshot.custom_synonyms.items()
                }
            ),
            registry_state=template_state,
            statuses=tuple(status_templates),
            reserved_names=frozen_reserved_names,
        )
    finally:
        discovery.close()


def _registry_state(
    *,
    all_tools: Mapping[str, UpstreamTool] | None = None,
    direct_tool_names: tuple[str, ...] = (),
    catalog: Mapping[str, UpstreamToolCatalogEntry] | None = None,
    search_index: SearchBackend | None = None,
    clients: Mapping[str, BaseUpstreamClient] | None = None,
) -> UpstreamRegistryState:
    return UpstreamRegistryState(
        all_tools=MappingProxyType(dict(all_tools or {})),
        direct_tool_names=tuple(direct_tool_names),
        catalog=MappingProxyType(dict(catalog or {})),
        search_index=search_index,
        clients=MappingProxyType(dict(clients or {})),
    )


def load_upstream_config_snapshot(path: str | Path) -> UpstreamConfigSnapshot:
    config_path = Path(path).expanduser()
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpstreamConfigError(f"Could not read upstream config {str(config_path)!r}: {exc}") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise UpstreamConfigError(
            f"Upstream config {str(config_path)!r} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise UpstreamConfigError("Upstream config must be a JSON object.")
    if "servers" in raw:
        servers = raw.get("servers")
        custom_synonyms = parse_tool_search_config(raw.get("tool_search"))
        credential_policy = parse_credential_policy(raw.get("credential_policy"))
    else:
        if "tool_search" in raw:
            raise UpstreamConfigError(
                "Upstream config with tool_search must contain a servers object."
            )
        if "credential_policy" in raw:
            raise UpstreamConfigError(
                "Upstream config with credential_policy must contain a servers object."
            )
        servers = raw
        custom_synonyms = {}
        credential_policy = DEFAULT_CREDENTIAL_POLICY
    if not isinstance(servers, dict):
        raise UpstreamConfigError("Upstream config must contain a servers object.")
    configs = tuple(parse_server_config(alias, value) for alias, value in servers.items())
    validate_credential_policy(credential_policy, configs)
    return UpstreamConfigSnapshot(
        configs=configs,
        custom_synonyms=custom_synonyms,
        credential_policy=credential_policy,
        source=str(config_path.resolve(strict=False)),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpstreamConfigError(f"Duplicate JSON key in upstream config: {key!r}.")
        result[key] = value
    return result


def parse_server_config(alias: str, value: Any) -> UpstreamServerConfig:
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias) or "__" in alias:
        raise UpstreamConfigError(
            f"Invalid upstream alias {alias!r}. Use 1-64 letters, digits, underscores, or hyphens; '__' is reserved."
        )
    if not isinstance(value, dict):
        raise UpstreamConfigError(f"Upstream {alias!r} config must be an object.")
    transport = str(value.get("transport") or "streamable_http")
    if transport == "http":
        transport = "streamable_http"
    if transport not in {"streamable_http", "stdio"}:
        raise UpstreamConfigError(
            f"Upstream {alias!r} transport must be streamable_http or stdio."
        )
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise UpstreamConfigError(f"Upstream {alias!r} enabled must be a boolean.")
    try:
        timeout_ms = int(value.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    except (TypeError, ValueError) as exc:
        raise UpstreamConfigError(f"Upstream {alias!r} timeout_ms must be positive.") from exc
    if timeout_ms <= 0:
        raise UpstreamConfigError(f"Upstream {alias!r} timeout_ms must be positive.")
    command = _optional_str(value.get("command"))
    args = tuple(
        _strip_matching_outer_quotes(arg)
        for arg in _string_tuple(value.get("args"), field_name="args", alias=alias)
    )
    if transport == "stdio":
        validate_stdio_launch(alias, command, args)
    elif not _optional_str(value.get("url")):
        raise UpstreamConfigError(
            f"Upstream {alias!r} requires url for streamable_http transport."
        )
    include_tools = _string_tuple(
        value.get("include_tools"), field_name="include_tools", alias=alias
    )
    exclude_tools = _string_tuple(
        value.get("exclude_tools"), field_name="exclude_tools", alias=alias
    )
    expose_mode = value.get("expose_mode", "direct")
    if expose_mode not in {"direct", "broker"}:
        raise UpstreamConfigError(
            f"Upstream {alias!r} expose_mode must be direct or broker."
        )
    pinned_tools = _string_tuple(
        value.get("pinned_tools"), field_name="pinned_tools", alias=alias
    )
    tags = _string_tuple(value.get("tags"), field_name="tags", alias=alias)
    _validate_bounded_strings(
        alias,
        "include_tools",
        include_tools,
        max_items=MAX_TOOL_FILTER_ITEMS,
        max_chars=MAX_TOOL_NAME_CHARS,
    )
    _validate_bounded_strings(
        alias,
        "exclude_tools",
        exclude_tools,
        max_items=MAX_TOOL_FILTER_ITEMS,
        max_chars=MAX_TOOL_NAME_CHARS,
    )
    _validate_bounded_strings(
        alias,
        "pinned_tools",
        pinned_tools,
        max_items=MAX_TOOL_FILTER_ITEMS,
        max_chars=MAX_TOOL_NAME_CHARS,
    )
    _validate_bounded_strings(
        alias,
        "tags",
        tags,
        max_items=MAX_TAG_ITEMS,
        max_chars=MAX_TAG_CHARS,
    )
    tool_policy = _tool_policy_dict(value.get("tool_policy"), alias=alias)
    overlap = sorted(set(include_tools) & set(exclude_tools))
    if overlap:
        raise UpstreamConfigError(
            f"Upstream {alias!r} cannot include and exclude the same tools: {', '.join(overlap)}."
        )
    return UpstreamServerConfig(
        alias=alias,
        transport=transport,
        enabled=enabled,
        url=_optional_str(value.get("url")),
        command=command,
        args=args,
        env=_env_dict(value.get("env"), field_name="env", alias=alias),
        headers=_string_dict(value.get("headers"), field_name="headers", alias=alias),
        authorization_env=_optional_str(value.get("authorization_env")),
        include_tools=include_tools,
        exclude_tools=exclude_tools,
        expose_mode=expose_mode,
        pinned_tools=pinned_tools,
        tags=tags,
        tool_policy=tool_policy,
        timeout_ms=timeout_ms,
    )


def parse_credential_policy(value: Any) -> str:
    if value is None:
        return DEFAULT_CREDENTIAL_POLICY
    if not isinstance(value, str) or value not in CREDENTIAL_POLICIES:
        raise UpstreamConfigError(
            "credential_policy must be local or strict."
        )
    return value


def is_sensitive_env_name(name: str) -> bool:
    return bool(SENSITIVE_ENV_NAME_RE.search(str(name)))


def validate_credential_policy(
    policy: str,
    configs: Iterable[UpstreamServerConfig],
) -> None:
    normalized = parse_credential_policy(policy)
    if normalized != "strict":
        return
    for config in configs:
        for name, value in config.env.items():
            if is_sensitive_env_name(name) and isinstance(value, str):
                raise UpstreamConfigError(
                    f"Upstream {config.alias!r} sensitive environment field {name!r} "
                    "must use env_ref or secret_ref when credential_policy is strict."
                )


def parse_tool_search_config(value: Any) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UpstreamConfigError("tool_search must be an object.")
    unknown = sorted(set(value) - {"custom_synonyms"})
    if unknown:
        raise UpstreamConfigError(
            f"tool_search contains unsupported fields: {', '.join(unknown)}."
        )
    custom = value.get("custom_synonyms")
    if custom is None:
        return {}
    if not isinstance(custom, dict):
        raise UpstreamConfigError("tool_search.custom_synonyms must be an object.")
    if len(custom) > 128:
        raise UpstreamConfigError("tool_search.custom_synonyms supports at most 128 terms.")
    normalized: dict[str, tuple[str, ...]] = {}
    for raw_key, raw_values in custom.items():
        if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 64:
            raise UpstreamConfigError(
                "tool_search.custom_synonyms keys must be non-empty strings up to 64 characters."
            )
        if any(ord(char) < 32 for char in raw_key):
            raise UpstreamConfigError(
                "tool_search.custom_synonyms keys must not contain control characters."
            )
        if (
            not isinstance(raw_values, list)
            or not raw_values
            or not all(isinstance(item, str) for item in raw_values)
        ):
            raise UpstreamConfigError(
                f"tool_search.custom_synonyms[{raw_key!r}] must be a non-empty list of strings."
            )
        if len(raw_values) > 10 or len(set(raw_values)) != len(raw_values):
            raise UpstreamConfigError(
                f"tool_search.custom_synonyms[{raw_key!r}] must contain 1-10 unique values."
            )
        if any(
            not item or len(item) > 64 or any(ord(char) < 32 for char in item)
            for item in raw_values
        ):
            raise UpstreamConfigError(
                f"tool_search.custom_synonyms[{raw_key!r}] values must be non-empty, control-free strings up to 64 characters."
            )
        normalized[raw_key] = tuple(raw_values)
    return normalized


def tool_catalog_entry(
    tool: UpstreamTool,
    config: UpstreamServerConfig,
) -> UpstreamToolCatalogEntry:
    definition = tool.public_definition
    title = definition.get("title")
    description = definition.get("description")
    input_schema = definition.get("inputSchema")
    properties = input_schema.get("properties") if isinstance(input_schema, Mapping) else None
    argument_names = tuple(properties) if isinstance(properties, Mapping) else ()
    return UpstreamToolCatalogEntry(
        public_name=tool.public_name,
        server_alias=config.alias,
        remote_name=tool.remote_name,
        title=title if isinstance(title, str) else "",
        description=(description if isinstance(description, str) else "")[:200],
        tags=tuple(config.tags),
        argument_names=argument_names,
        effective_risk=tool.effective_risk,
        public_schema_digest=tool.public_schema_digest,
    )


def build_client(
    config: UpstreamServerConfig,
    protocol_version: str,
    secret_resolver: Callable[[str], str] | None = None,
) -> BaseUpstreamClient:
    if config.transport == "stdio":
        return StdioUpstreamClient(config, protocol_version, secret_resolver=secret_resolver)
    return HttpUpstreamClient(config, protocol_version, secret_resolver=secret_resolver)


def filter_tools(
    tools: list[dict[str, Any]], config: UpstreamServerConfig
) -> list[dict[str, Any]]:
    included = set(config.include_tools)
    excluded = set(config.exclude_tools)
    result: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str):
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream tool definition had a non-string name.",
                category="protocol",
            )
        if included and name not in included:
            continue
        if name in excluded:
            continue
        result.append(tool)
    return result


def namespaced_tool_name(alias: str, remote_name: str) -> str:
    if not remote_name or any(ord(char) < 32 for char in remote_name):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tool name was empty or contained control characters.",
            category="protocol",
        )
    public_name = f"{alias}__{remote_name}"
    if len(public_name) > MAX_TOOL_NAME_CHARS:
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Namespaced upstream tool name exceeded the supported length.",
            category="protocol",
        )
    return public_name


def namespaced_tool_definition(
    public_name: str, tool: dict[str, Any]
) -> dict[str, Any]:
    # Sanitization and immutable snapshot construction own the deep traversal.
    # A shallow copy is enough to replace the public name without mutating the
    # freshly decoded upstream object.
    definition = dict(tool)
    definition["name"] = public_name
    return definition


def classify_risk(
    raw_tool: dict[str, Any],
    tool_policy: Mapping[str, str],
    remote_name: str,
) -> str:
    policy = tool_policy.get(remote_name)
    if policy in {"readonly", "mutating"}:
        return policy
    annotations = raw_tool.get("annotations")
    if isinstance(annotations, dict):
        if (
            annotations.get("readOnlyHint") is True
            and annotations.get("destructiveHint") is not True
        ):
            return "readonly"
    return "mutating"


def normalize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tools/call result was not an object.",
            category="protocol",
        )
    try:
        normalized = _clone_json_tree(result, max_depth=MAX_UPSTREAM_RESULT_DEPTH)
    except (RecursionError, TypeError, ValueError) as exc:
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tools/call result exceeded the supported JSON structure.",
            category="protocol",
        ) from exc
    if not isinstance(normalized, dict):
        raise AssertionError("JSON object clone did not preserve its root type.")
    content = normalized.get("content")
    if content is None:
        normalized["content"] = []
    elif not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tools/call content was not an array of content objects.",
            category="protocol",
        )
    structured = normalized.get("structuredContent")
    if "structuredContent" in normalized and not isinstance(structured, dict):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tools/call structuredContent was not an object.",
            category="protocol",
        )
    is_error = normalized.get("isError")
    if is_error is None:
        normalized["isError"] = False
    elif not isinstance(is_error, bool):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tools/call isError was not a boolean.",
            category="protocol",
        )
    return normalized


def upstream_error_result(
    code: str,
    message: str,
    *,
    category: str = "runtime",
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    alias: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "category": category,
        "retryable": retryable,
        "details": _bounded_error_details(details),
    }
    payload: dict[str, Any] = {"ok": False, "error": error}
    if alias is not None:
        payload["upstream_alias"] = alias
    if tool_name is not None:
        payload["tool_name"] = tool_name
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": payload,
        "isError": True,
    }


_PROTECTED_HTTP_HEADER_NAMES = frozenset(
    {"mcp-session-id", "mcp-protocol-version", "authorization"}
)


def _without_protected_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in _PROTECTED_HTTP_HEADER_NAMES
    }


def _read_bounded_response(response: Any) -> bytes:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UpstreamError(
            "UPSTREAM_RESPONSE_TOO_LARGE",
            "Upstream response exceeded the maximum supported size.",
            category="protocol",
        )
    return raw


def decode_http_rpc_response(
    raw: bytes,
    content_type: str,
    *,
    expected_id: int | None = None,
) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        return _decode_http_rpc_text(
            text,
            content_type,
            expected_id=expected_id,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream HTTP response was not valid standard JSON.",
            category="protocol",
        ) from exc


def _decode_http_rpc_text(
    text: str,
    content_type: str,
    *,
    expected_id: int | None,
) -> dict[str, Any]:
    if "text/event-stream" not in content_type.lower():
        parsed = strict_json_loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream HTTP response JSON was not an object.",
            category="protocol",
        )
    events: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line:
            if current:
                events.append("\n".join(current))
                current = []
            continue
        if line.startswith("data:"):
            current.append(line.removeprefix("data:").lstrip())
    if current:
        events.append("\n".join(current))
    if not events:
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream SSE response did not include data events.",
            category="protocol",
        )
    candidates: list[dict[str, Any]] = []
    for event in events:
        parsed = strict_json_loads(event)
        if not isinstance(parsed, dict):
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream SSE data was not a JSON object.",
                category="protocol",
            )
        candidates.append(parsed)
    if expected_id is not None:
        for candidate in candidates:
            candidate_id = candidate.get("id")
            if type(candidate_id) is int and candidate_id == expected_id:
                return candidate
    return candidates[0]


def safe_target(config: UpstreamServerConfig) -> str | None:
    if config.transport == "stdio":
        command = config.command or ""
        args = " ".join(config.args[:3])
        suffix = " ..." if len(config.args) > 3 else ""
        return f"{command} {args}{suffix}".strip()
    if not config.url:
        return None
    parsed = urllib.parse.urlsplit(config.url)
    redacted = parsed._replace(query="", fragment="")
    return urllib.parse.urlunsplit(redacted)


def validate_stdio_launch(alias: str, command: str | None, args: tuple[str, ...]) -> None:
    if not command or not command.strip():
        raise UpstreamConfigError(f"Upstream {alias!r} requires command for stdio transport.")
    command_text = command.strip()
    if "\n" in command_text or "\r" in command_text:
        raise UpstreamConfigError(
            f"Upstream {alias!r} command must be a single executable path or name."
        )
    first_word = command_text.split()[0].strip('"\'').lower()
    leaf = command_text.strip('"\'').replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first_word in FORBIDDEN_STDIO_COMMANDS or leaf in FORBIDDEN_STDIO_COMMANDS:
        raise UpstreamConfigError(
            f"Upstream {alias!r} command cannot be a shell interpreter."
        )
    if SHELL_FRAGMENT_RE.search(command_text):
        raise UpstreamConfigError(
            f"Upstream {alias!r} command cannot contain shell control syntax."
        )
    for index, arg in enumerate(args):
        if "\n" in arg or "\r" in arg or SHELL_FRAGMENT_RE.search(arg):
            raise UpstreamConfigError(
                f"Upstream {alias!r} args[{index}] cannot contain shell control syntax."
            )


def _strip_matching_outer_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def base_upstream_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in UPSTREAM_BASE_ENV_NAMES
    }


def resolve_env_config(
    env_config: dict[str, Any],
    *,
    secret_resolver: Callable[[str], str] | None = None,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for name, value in env_config.items():
        if isinstance(value, str):
            resolved[name] = value
            continue
        if not isinstance(value, dict):
            raise UpstreamConfigError(
                f"Environment value for {name!r} must be a string or reference object."
            )
        env_ref = value.get("env_ref")
        secret_ref = value.get("secret_ref")
        if isinstance(env_ref, str) and env_ref:
            resolved[name] = os.environ.get(env_ref, "")
            continue
        if isinstance(secret_ref, str) and secret_ref:
            if secret_resolver is None:
                raise UpstreamConfigError("secret_ref requires a configured secret resolver.")
            resolved[name] = secret_resolver(secret_ref)
            continue
        raise UpstreamConfigError(
            f"Environment reference for {name!r} must contain env_ref or secret_ref."
        )
    return resolved


def error_payload(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, UpstreamError):
        payload: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "category": exc.category,
            "retryable": exc.retryable,
        }
        if exc.details:
            payload["details"] = _bounded_error_details(exc.details)
        return payload
    if isinstance(exc, UpstreamConfigError):
        return {
            "code": "UPSTREAM_CONFIG_INVALID",
            "message": str(exc),
            "category": "configuration",
            "retryable": False,
        }
    return {
        "code": "UPSTREAM_INITIALIZATION_FAILED",
        "message": str(exc),
        "category": "runtime",
        "retryable": True,
    }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UpstreamConfigError("Expected string value.")
    return value


def _string_tuple(value: Any, *, field_name: str, alias: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UpstreamConfigError(
            f"Upstream {alias!r} field {field_name} must be a list of strings."
        )
    if len(set(value)) != len(value):
        raise UpstreamConfigError(
            f"Upstream {alias!r} field {field_name} must not contain duplicates."
        )
    return tuple(value)


def _validate_bounded_strings(
    alias: str,
    field_name: str,
    values: tuple[str, ...],
    *,
    max_items: int,
    max_chars: int,
) -> None:
    if len(values) > max_items:
        raise UpstreamConfigError(
            f"Upstream {alias!r} field {field_name} supports at most {max_items} entries."
        )
    if any(
        not value
        or len(value) > max_chars
        or any(ord(char) < 32 for char in value)
        for value in values
    ):
        raise UpstreamConfigError(
            f"Upstream {alias!r} field {field_name} must contain non-empty, control-free strings up to {max_chars} characters."
        )


def _tool_policy_dict(value: Any, *, alias: str) -> dict[str, str]:
    policy = _string_dict(value, field_name="tool_policy", alias=alias)
    if len(policy) > MAX_TOOL_POLICY_ITEMS:
        raise UpstreamConfigError(
            f"Upstream {alias!r} tool_policy supports at most {MAX_TOOL_POLICY_ITEMS} entries."
        )
    for remote_name, risk in policy.items():
        if (
            not remote_name
            or len(remote_name) > MAX_TOOL_NAME_CHARS
            or any(ord(char) < 32 for char in remote_name)
        ):
            raise UpstreamConfigError(
                f"Upstream {alias!r} tool_policy keys must be non-empty, control-free tool names up to {MAX_TOOL_NAME_CHARS} characters."
            )
        if risk not in {"readonly", "mutating"}:
            raise UpstreamConfigError(
                f"Upstream {alias!r} tool_policy values must be readonly or mutating."
            )
    return policy


def upstream_exposure_report(state: UpstreamRegistryState) -> dict[str, Any]:
    """Return upstream-only context and exposure metrics for operators."""

    direct_names = frozenset(state.direct_tool_names)
    definition_sizes = {
        name: len(
            strict_json_bytes(
                tool.public_definition,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        for name, tool in state.all_tools.items()
    }
    server_metrics: dict[str, dict[str, int]] = {}
    for name in sorted(state.all_tools):
        alias, _separator, _remote = name.partition("__")
        metrics = server_metrics.setdefault(
            alias,
            {
                "catalog_count": 0,
                "direct_count": 0,
                "broker_only_count": 0,
                "definition_bytes": 0,
            },
        )
        metrics["catalog_count"] += 1
        metrics["definition_bytes"] += definition_sizes[name]
        if name in direct_names:
            metrics["direct_count"] += 1
        else:
            metrics["broker_only_count"] += 1
    largest = sorted(
        state.all_tools,
        key=lambda name: (-definition_sizes[name], name),
    )[:10]
    return {
        "scope": "upstream_only",
        "excludes_local_and_admin_definitions": True,
        "direct": {
            "count": len(direct_names),
            "definition_bytes": sum(definition_sizes[name] for name in direct_names),
        },
        "catalog": {
            "count": len(state.catalog),
            "broker_only_count": len(set(state.catalog) - direct_names),
        },
        "largest_public_definitions": [
            {
                "name": name,
                "server": name.partition("__")[0],
                "remote_name": state.all_tools[name].remote_name,
                "definition_bytes": definition_sizes[name],
                "direct": name in direct_names,
            }
            for name in largest
        ],
        "servers": [
            {
                "alias": alias,
                **metrics,
            }
            for alias, metrics in sorted(server_metrics.items())
        ],
    }


def _string_dict(value: Any, *, field_name: str, alias: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise UpstreamConfigError(
            f"Upstream {alias!r} field {field_name} must be an object with string keys and values."
        )
    return dict(value)


def _env_dict(value: Any, *, field_name: str, alias: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UpstreamConfigError(
            f"Upstream {alias!r} field {field_name} must be an object."
        )
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise UpstreamConfigError(
                f"Upstream {alias!r} field {field_name} must use string keys."
            )
        if isinstance(item, str):
            result[key] = item
            continue
        if isinstance(item, dict) and len(item) == 1:
            ref_key, ref_value = next(iter(item.items()))
            if ref_key in {"env_ref", "secret_ref"} and isinstance(ref_value, str) and ref_value:
                result[key] = {ref_key: ref_value}
                continue
        raise UpstreamConfigError(
            f"Upstream {alias!r} field {field_name}.{key} must be a string, env_ref, or secret_ref."
        )
    return result
