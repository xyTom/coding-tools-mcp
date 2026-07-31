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
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from http.client import RemoteDisconnected
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .upstream_result import RESULT_INLINE_MAX, budget_tool_result, result_json_bytes
from .upstream_result_store import ResultStore
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
MAX_RESPONSE_BYTES = 1_048_576
MAX_TOOL_NAME_CHARS = 512
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
    source: str | None = None

    @classmethod
    def empty(cls) -> "UpstreamConfigSnapshot":
        return cls()


@dataclass(frozen=True)
class UpstreamTool:
    public_name: str
    remote_name: str
    raw_definition: dict[str, Any]
    public_definition: dict[str, Any]
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
            result["error"] = copy.deepcopy(self.error)
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
        return [copy.deepcopy(tool) for tool in tools]

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
                    "UPSTREAM_DISCONNECTED",
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
    if response.get("id") != request_id:
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
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
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
            details={"method": method, "rpc_error": copy.deepcopy(error)},
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
    ) -> None:
        super().__init__(config, protocol_version, secret_resolver=secret_resolver)
        if not config.url:
            raise UpstreamConfigError(
                f"Upstream {config.alias!r} requires url for streamable_http transport."
            )
        self.url = config.url
        self.session_id: str | None = None

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
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.config.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        token = os.environ.get(self.config.authorization_env) if self.config.authorization_env else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        timeout_s = max(self.config.timeout_ms, 1) / 1000
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                if not expect_response or response.status in {202, 204}:
                    return None
                raw = _read_bounded_response(response)
                expected_id = payload.get("id")
                return decode_http_rpc_response(
                    raw,
                    response.headers.get("Content-Type", ""),
                    expected_id=expected_id if isinstance(expected_id, int) else None,
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) <= MAX_RESPONSE_BYTES and raw:
                try:
                    return decode_http_rpc_response(
                        raw,
                        exc.headers.get("Content-Type", ""),
                        expected_id=payload.get("id") if isinstance(payload.get("id"), int) else None,
                    )
                except (UpstreamError, UnicodeDecodeError, json.JSONDecodeError):
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
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise UpstreamError(
                "UPSTREAM_DISCONNECTED",
                "Upstream MCP stdio process disconnected.",
                retryable=True,
            ) from exc

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            return
        try:
            for raw_line in self.process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    self._responses.put(
                        UpstreamError(
                            "UPSTREAM_PROTOCOL_ERROR",
                            "Upstream stdio returned invalid JSON.",
                            category="protocol",
                        )
                    )
                    continue
                if not isinstance(parsed, dict):
                    self._responses.put(
                        UpstreamError(
                            "UPSTREAM_PROTOCOL_ERROR",
                            "Upstream stdio response was not a JSON object.",
                            category="protocol",
                        )
                    )
                    continue
                if "id" not in parsed and isinstance(parsed.get("method"), str):
                    continue
                self._responses.put(parsed)
        except UnicodeError:
            self._responses.put(
                UpstreamError(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "Upstream stdio returned non-UTF-8 output.",
                    category="protocol",
                )
            )

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
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        try:
            self._initialize_configs(frozenset(reserved_names))
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
        state = self._state
        tool = state.all_tools.get(name)
        if tool is None:
            return upstream_error_result(
                "UPSTREAM_TOOL_NOT_FOUND",
                f"Unknown upstream tool: {name}",
                category="validation",
            )
        alias, _separator, _remote = name.partition("__")
        with self._lifecycle_lock:
            if self._closed:
                return upstream_error_result(
                    "UPSTREAM_DISCONNECTED",
                    "Upstream Gateway is closed.",
                    retryable=True,
                    alias=alias,
                    tool_name=name,
                )
            client = state.clients.get(alias)
            if client is None:
                return upstream_error_result(
                    "UPSTREAM_NOT_AVAILABLE",
                    f"Upstream {alias!r} is not available.",
                    retryable=True,
                    alias=alias,
                    tool_name=name,
                )
        try:
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
            budgeted = budget_tool_result(normalized)
            if handle is not None:
                structured = budgeted.get("structuredContent")
                if not isinstance(structured, dict):
                    structured = {}
                    budgeted["structuredContent"] = structured
                structured["_result_handle"] = handle
                structured["_result_fetch_tool"] = "upstream_result_fetch"
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

    def status_payload(self) -> dict[str, Any]:
        state = self._state
        statuses = [self.statuses[alias].payload() for alias in sorted(self.statuses)]
        return {
            "enabled": any(status.enabled for status in self.statuses.values()),
            "server_count": len(self.statuses),
            "initialized_count": sum(1 for status in self.statuses.values() if status.initialized),
            "tool_count": len(state.direct_tool_names),
            "catalog_tool_count": len(state.catalog),
            "snapshot_immutable": True,
            "remote_capability_boundary": "upstream_server",
            "servers": statuses,
        }

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            state = self._state
        for client in state.clients.values():
            client.close()
        self.result_store.clear()

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
                        raw_definition = namespaced_tool_definition(public_name, raw_tool)
                        public_definition = sanitize_definition(raw_definition)
                        registered.append(
                            UpstreamTool(
                                public_name=public_name,
                                remote_name=remote_name,
                                raw_definition=raw_definition,
                                public_definition=public_definition,
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
    else:
        if "tool_search" in raw:
            raise UpstreamConfigError(
                "Upstream config with tool_search must contain a servers object."
            )
        servers = raw
        custom_synonyms = {}
    if not isinstance(servers, dict):
        raise UpstreamConfigError("Upstream config must contain a servers object.")
    configs = tuple(parse_server_config(alias, value) for alias, value in servers.items())
    return UpstreamConfigSnapshot(
        configs=configs,
        custom_synonyms=custom_synonyms,
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
    args = _string_tuple(value.get("args"), field_name="args", alias=alias)
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
    _validate_nonempty_strings(alias, "pinned_tools", pinned_tools)
    _validate_nonempty_strings(alias, "tags", tags)
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
    properties = input_schema.get("properties") if isinstance(input_schema, dict) else None
    argument_names = tuple(properties) if isinstance(properties, dict) else ()
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
    definition = copy.deepcopy(tool)
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
    normalized = copy.deepcopy(result)
    content = normalized.get("content")
    if content is None:
        normalized["content"] = []
    elif not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tools/call content was not an array of content objects.",
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
        "details": copy.deepcopy(details or {}),
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
    text = raw.decode("utf-8")
    if "text/event-stream" not in content_type.lower():
        parsed = json.loads(text)
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
        parsed = json.loads(event)
        if not isinstance(parsed, dict):
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream SSE data was not a JSON object.",
                category="protocol",
            )
        candidates.append(parsed)
    if expected_id is not None:
        for candidate in candidates:
            if candidate.get("id") == expected_id:
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
            payload["details"] = copy.deepcopy(exc.details)
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


def _validate_nonempty_strings(
    alias: str,
    field_name: str,
    values: tuple[str, ...],
) -> None:
    if any(not value or any(ord(char) < 32 for char in value) for value in values):
        raise UpstreamConfigError(
            f"Upstream {alias!r} field {field_name} must contain non-empty strings without control characters."
        )


def _tool_policy_dict(value: Any, *, alias: str) -> dict[str, str]:
    policy = _string_dict(value, field_name="tool_policy", alias=alias)
    for remote_name, risk in policy.items():
        if not remote_name or any(ord(char) < 32 for char in remote_name):
            raise UpstreamConfigError(
                f"Upstream {alias!r} tool_policy keys must be non-empty tool names."
            )
        if risk not in {"readonly", "mutating"}:
            raise UpstreamConfigError(
                f"Upstream {alias!r} tool_policy values must be readonly or mutating."
            )
    return policy


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
