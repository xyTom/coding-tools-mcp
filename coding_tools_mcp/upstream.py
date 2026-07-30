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
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Any


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
    timeout_ms: int = DEFAULT_TIMEOUT_MS


@dataclass(frozen=True)
class UpstreamConfigSnapshot:
    """Configuration, enable state, and allowlists fixed before Runtime creation."""

    configs: tuple[UpstreamServerConfig, ...] = ()
    source: str | None = None

    @classmethod
    def empty(cls) -> "UpstreamConfigSnapshot":
        return cls()


@dataclass(frozen=True)
class UpstreamTool:
    public_name: str
    remote_name: str
    definition: dict[str, Any]


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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        return normalize_tool_result(response)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return None

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

    def close(self) -> None:
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
    ) -> None:
        self.protocol_version = protocol_version
        self.secret_resolver = secret_resolver
        self.configs = tuple(configs)
        self.clients: dict[str, BaseUpstreamClient] = {}
        self.statuses: dict[str, UpstreamStatus] = {}
        self._tools: dict[str, UpstreamTool] = {}
        self._tool_order: tuple[str, ...] = ()
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
    ) -> "UpstreamManager":
        return cls((), protocol_version=protocol_version, reserved_names=reserved_names)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: UpstreamConfigSnapshot,
        *,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        secret_resolver: Callable[[str], str] | None = None,
        reserved_names: Collection[str] = (),
    ) -> "UpstreamManager":
        return cls(
            snapshot.configs,
            protocol_version=protocol_version,
            secret_resolver=secret_resolver,
            reserved_names=reserved_names,
        )

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(self._tools[name].definition) for name in self._tool_order]

    def tool_names(self) -> list[str]:
        return list(self._tool_order)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return upstream_error_result(
                "UPSTREAM_TOOL_NOT_FOUND",
                f"Unknown upstream tool: {name}",
                category="validation",
            )
        if self._closed:
            return upstream_error_result(
                "UPSTREAM_DISCONNECTED",
                "Upstream Gateway is closed.",
                retryable=True,
                alias=name.partition("__")[0],
                tool_name=name,
            )
        alias, _separator, _remote = name.partition("__")
        client = self.clients.get(alias)
        if client is None:
            return upstream_error_result(
                "UPSTREAM_NOT_AVAILABLE",
                f"Upstream {alias!r} is not available.",
                retryable=True,
                alias=alias,
                tool_name=name,
            )
        try:
            return client.call_tool(tool.remote_name, arguments or {})
        except UpstreamError as exc:
            return upstream_error_result(
                exc.code,
                exc.message,
                category=exc.category,
                retryable=exc.retryable,
                details=exc.details,
                alias=alias,
                tool_name=name,
            )
        except OSError:
            return upstream_error_result(
                "UPSTREAM_DISCONNECTED",
                "Upstream MCP server disconnected.",
                retryable=True,
                alias=alias,
                tool_name=name,
            )

    def status_payload(self) -> dict[str, Any]:
        statuses = [self.statuses[alias].payload() for alias in sorted(self.statuses)]
        return {
            "enabled": any(status.enabled for status in self.statuses.values()),
            "server_count": len(self.statuses),
            "initialized_count": sum(1 for status in self.statuses.values() if status.initialized),
            "tool_count": len(self._tool_order),
            "snapshot_immutable": True,
            "remote_capability_boundary": "upstream_server",
            "servers": statuses,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in list(self.clients.values()):
            client.close()
        self.clients.clear()

    def _initialize_configs(self, reserved_names: frozenset[str]) -> None:
        seen_public_names = set(reserved_names)
        ordered_names: list[str] = []
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
                    registered.append(
                        UpstreamTool(
                            public_name=public_name,
                            remote_name=remote_name,
                            definition=namespaced_tool_definition(public_name, raw_tool),
                        )
                    )
                self.clients[config.alias] = client
                for tool in registered:
                    self._tools[tool.public_name] = tool
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
        self._tool_order = tuple(ordered_names)


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
    servers = raw.get("servers", raw)
    if not isinstance(servers, dict):
        raise UpstreamConfigError("Upstream config must contain a servers object.")
    configs = tuple(parse_server_config(alias, value) for alias, value in servers.items())
    return UpstreamConfigSnapshot(configs=configs, source=str(config_path.resolve(strict=False)))


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
        timeout_ms=timeout_ms,
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
    input_schema = tool.get("inputSchema")
    if not isinstance(input_schema, dict):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tool definition did not contain an object inputSchema.",
            category="protocol",
        )
    annotations = tool.get("annotations")
    if annotations is not None and not isinstance(annotations, dict):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tool annotations were not an object.",
            category="protocol",
        )
    output_schema = tool.get("outputSchema")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise UpstreamError(
            "UPSTREAM_PROTOCOL_ERROR",
            "Upstream tool outputSchema was not an object.",
            category="protocol",
        )
    definition = copy.deepcopy(tool)
    definition["name"] = public_name
    return definition


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
