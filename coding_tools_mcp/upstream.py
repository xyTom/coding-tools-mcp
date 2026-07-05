from __future__ import annotations

import atexit
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT_MS = 30_000
MAX_RESPONSE_BYTES = 1_048_576
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


class UpstreamConfigError(ValueError):
    pass


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
            result["error"] = self.error
        return result


@dataclass(frozen=True)
class LoadedUpstreamConfigs:
    configs: list[UpstreamServerConfig]
    invalid_statuses: list[UpstreamStatus] = field(default_factory=list)


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
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        return normalize_tool_result(response)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def health_payload(self) -> dict[str, Any]:
        return {"transport": self.config.transport, "running": True}

    def logs_payload(self, *, max_lines: int = 200) -> dict[str, Any]:
        return {"lines": [], "truncated": False, "max_lines": max_lines}

    def _next_request_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id


class HttpUpstreamClient(BaseUpstreamClient):
    def __init__(
        self,
        config: UpstreamServerConfig,
        protocol_version: str,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(config, protocol_version, secret_resolver=secret_resolver)
        if not config.url:
            raise UpstreamConfigError(f"Upstream {config.alias!r} requires url for streamable_http transport.")
        self.url = config.url
        self.session_id: str | None = None

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": self._next_request_id(), "method": method}
        if params is not None:
            payload["params"] = params
        response = self._send(payload, expect_response=True)
        if not isinstance(response, dict):
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "Upstream response was not a JSON object.", category="protocol")
        if "error" in response:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise UpstreamError(
                "UPSTREAM_RPC_ERROR",
                str(error.get("message") or f"Upstream RPC error from {method}."),
                category="upstream",
                details={"method": method, "error": error},
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "Upstream response result was not an object.", category="protocol")
        return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
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
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise UpstreamError(
                        "UPSTREAM_RESPONSE_TOO_LARGE",
                        "Upstream response exceeded the maximum supported size.",
                        category="protocol",
                    )
                return decode_http_rpc_response(raw, response.headers.get("Content-Type", ""))
        except TimeoutError as exc:
            raise UpstreamError("UPSTREAM_TIMEOUT", "Timed out waiting for upstream MCP server.", retryable=True) from exc
        except urllib.error.URLError as exc:
            raise UpstreamError(
                "UPSTREAM_CONNECTION_FAILED",
                f"Could not connect to upstream MCP server: {exc.reason}",
                retryable=True,
            ) from exc
        except json.JSONDecodeError as exc:
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "Upstream returned invalid JSON.", category="protocol") from exc


class StdioUpstreamClient(BaseUpstreamClient):
    def __init__(
        self,
        config: UpstreamServerConfig,
        protocol_version: str,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(config, protocol_version, secret_resolver=secret_resolver)
        if not config.command:
            raise UpstreamConfigError(f"Upstream {config.alias!r} requires command for stdio transport.")
        self._lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: queue.Queue[str] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=500)
        self._stderr_lock = threading.Lock()
        env = os.environ.copy()
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
            errors="replace",
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
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        with self._lock:
            self._write(payload)
            deadline = time.monotonic() + max(self.config.timeout_ms, 1) / 1000
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamError("UPSTREAM_TIMEOUT", "Timed out waiting for upstream MCP server.", retryable=True)
                try:
                    response = self._responses.get(timeout=remaining)
                except queue.Empty as exc:
                    raise UpstreamError("UPSTREAM_TIMEOUT", "Timed out waiting for upstream MCP server.", retryable=True) from exc
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response.get("error") if isinstance(response.get("error"), dict) else {}
                    raise UpstreamError(
                        "UPSTREAM_RPC_ERROR",
                        str(error.get("message") or f"Upstream RPC error from {method}."),
                        category="upstream",
                        details={"method": method, "error": error},
                    )
                result = response.get("result")
                if not isinstance(result, dict):
                    raise UpstreamError(
                        "UPSTREAM_PROTOCOL_ERROR",
                        "Upstream response result was not an object.",
                        category="protocol",
                    )
                return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
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
            stderr = drain_queue(self._stderr)[-5:]
            raise UpstreamError(
                "UPSTREAM_PROCESS_EXITED",
                "Upstream MCP stdio process exited.",
                category="runtime",
                retryable=True,
                details={"returncode": self.process.returncode, "stderr_tail": stderr},
            )
        if self.process.stdin is None:
            raise UpstreamError("UPSTREAM_PROCESS_CLOSED", "Upstream MCP stdin is closed.", retryable=True)
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                self._responses.put(parsed)

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for line in self.process.stderr:
            item = line.rstrip("\n")[:500]
            self._stderr.put(item)
            with self._stderr_lock:
                self._stderr_lines.append(item)

    def health_payload(self) -> dict[str, Any]:
        return {
            "transport": self.config.transport,
            "running": self.process.poll() is None,
            "pid": self.process.pid,
            "returncode": self.process.poll(),
        }

    def logs_payload(self, *, max_lines: int = 200) -> dict[str, Any]:
        safe_max = max(1, min(max_lines, 500))
        with self._stderr_lock:
            lines = list(self._stderr_lines)[-safe_max:]
            truncated = len(self._stderr_lines) > safe_max
        return {"lines": lines, "truncated": truncated, "max_lines": safe_max}


class UpstreamManager:
    def __init__(
        self,
        configs: list[UpstreamServerConfig],
        *,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        secret_resolver: Callable[[str], str] | None = None,
        invalid_statuses: list[UpstreamStatus] | None = None,
    ) -> None:
        self.protocol_version = protocol_version
        self.secret_resolver = secret_resolver
        self.configs = configs
        self.clients: dict[str, BaseUpstreamClient] = {}
        self.tools: dict[str, UpstreamTool] = {}
        self.statuses: dict[str, UpstreamStatus] = {status.alias: status for status in invalid_statuses or []}
        self._tool_lock = threading.Lock()
        self._initialize_configs()

    @classmethod
    def empty(
        cls,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        *,
        secret_resolver: Callable[[str], str] | None = None,
        invalid_statuses: list[UpstreamStatus] | None = None,
    ) -> "UpstreamManager":
        instance = cls.__new__(cls)
        instance.protocol_version = protocol_version
        instance.secret_resolver = secret_resolver
        instance.configs = []
        instance.clients = {}
        instance.tools = {}
        instance.statuses = {status.alias: status for status in invalid_statuses or []}
        instance._tool_lock = threading.Lock()
        return instance

    @classmethod
    def from_config_file(
        cls,
        path: str | None,
        *,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> "UpstreamManager":
        if not path:
            return cls.empty(protocol_version, secret_resolver=secret_resolver)
        loaded = load_upstream_configs_tolerant(path)
        return cls(
            loaded.configs,
            protocol_version=protocol_version,
            secret_resolver=secret_resolver,
            invalid_statuses=loaded.invalid_statuses,
        )

    def tool_definitions(self, *, tool_profile: str) -> list[dict[str, Any]]:
        with self._tool_lock:
            return [
                profiled_definition(tool.definition, tool_profile)
                for tool in self.tools.values()
                if self._visible(tool, tool_profile)
            ]

    def tool_names(self, *, tool_profile: str) -> list[str]:
        with self._tool_lock:
            return [name for name, tool in self.tools.items() if self._visible(tool, tool_profile)]

    def has_tool(self, name: str, *, tool_profile: str) -> bool:
        with self._tool_lock:
            tool = self.tools.get(name)
            return tool is not None and self._visible(tool, tool_profile)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._tool_lock:
            tool = self.tools.get(name)
        if tool is None:
            return upstream_error_result("UPSTREAM_TOOL_NOT_FOUND", f"Unknown upstream tool: {name}", category="validation")
        alias, _separator, _remote = name.partition("__")
        client = self.clients.get(alias)
        if client is None:
            return upstream_error_result("UPSTREAM_NOT_AVAILABLE", f"Upstream {alias!r} is not available.", retryable=True)
        try:
            return client.call_tool(tool.remote_name, arguments or {})
        except UpstreamError as exc:
            return upstream_error_result(exc.code, exc.message, category=exc.category, retryable=exc.retryable, details=exc.details)

    def status_payload(self) -> dict[str, Any]:
        statuses = [status.payload() for status in self.statuses.values()]
        return {
            "enabled": bool(self.statuses),
            "server_count": len(self.statuses),
            "initialized_count": sum(1 for status in self.statuses.values() if status.initialized),
            "tool_count": len(self.tools),
            "servers": statuses,
        }

    def health_payload(self, alias: str | None = None) -> dict[str, Any]:
        aliases = [alias] if alias else sorted(self.statuses)
        servers: list[dict[str, Any]] = []
        for item_alias in aliases:
            status = self.statuses.get(item_alias)
            if status is None:
                servers.append({"alias": item_alias, "error": error_payload(UpstreamError("UPSTREAM_NOT_FOUND", "Unknown upstream MCP server."))})
                continue
            payload = status.payload()
            client = self.clients.get(item_alias)
            payload["health"] = client.health_payload() if client is not None else {"running": False}
            servers.append(payload)
        return {
            "ok": True,
            "server_count": len(servers),
            "running_count": sum(1 for item in servers if item.get("health", {}).get("running") is True),
            "servers": servers,
        }

    def logs_payload(self, alias: str | None = None, *, max_lines: int = 200) -> dict[str, Any]:
        aliases = [alias] if alias else sorted(self.statuses)
        servers: list[dict[str, Any]] = []
        for item_alias in aliases:
            status = self.statuses.get(item_alias)
            if status is None:
                servers.append({"alias": item_alias, "error": error_payload(UpstreamError("UPSTREAM_NOT_FOUND", "Unknown upstream MCP server."))})
                continue
            client = self.clients.get(item_alias)
            logs = client.logs_payload(max_lines=max_lines) if client is not None else {"lines": [], "truncated": False, "max_lines": max_lines}
            servers.append({"alias": item_alias, "transport": status.transport, "logs": logs})
        return {"ok": True, "servers": servers}

    def start_server(self, alias: str) -> dict[str, Any]:
        config = self._config_by_alias(alias)
        if config is None:
            return upstream_error_result("UPSTREAM_NOT_FOUND", f"Unknown upstream MCP server: {alias}", category="validation")
        if not config.enabled:
            return upstream_error_result("UPSTREAM_DISABLED", f"Upstream MCP server {alias!r} is disabled.", category="configuration")
        self.stop_server(alias)
        seen_public_names = {name for name in self.tools if not name.startswith(f"{alias}__")}
        self._initialize_config(config, seen_public_names)
        return {"ok": True, "status": self.statuses[alias].payload()}

    def stop_server(self, alias: str) -> dict[str, Any]:
        status = self.statuses.get(alias)
        if status is None:
            return upstream_error_result("UPSTREAM_NOT_FOUND", f"Unknown upstream MCP server: {alias}", category="validation")
        client = self.clients.pop(alias, None)
        if client is not None:
            client.close()
        with self._tool_lock:
            for name in [name for name in self.tools if name.startswith(f"{alias}__")]:
                self.tools.pop(name, None)
        status.initialized = False
        status.tool_count = 0
        status.error = {"code": "UPSTREAM_STOPPED", "message": "Upstream MCP server is stopped.", "category": "runtime", "retryable": True}
        return {"ok": True, "status": status.payload()}

    def close(self) -> None:
        for alias in list(self.clients):
            self.stop_server(alias)

    def _initialize_configs(self) -> None:
        seen_public_names: set[str] = set()
        for config in self.configs:
            self._initialize_config(config, seen_public_names)

    def _initialize_config(self, config: UpstreamServerConfig, seen_public_names: set[str]) -> None:
        status = UpstreamStatus(
            alias=config.alias,
            transport=config.transport,
            enabled=config.enabled,
            target=safe_target(config),
        )
        self.statuses[config.alias] = status
        if not config.enabled:
            return
        client: BaseUpstreamClient | None = None
        try:
            client = build_client(config, self.protocol_version, secret_resolver=self.secret_resolver)
            client.initialize()
            raw_tools = filter_tools(client.list_tools(), config)
            registered: list[UpstreamTool] = []
            for raw_tool in raw_tools:
                remote_name = raw_tool.get("name")
                if not isinstance(remote_name, str) or not remote_name:
                    continue
                public_name = f"{config.alias}__{remote_name}"
                if public_name in seen_public_names:
                    raise UpstreamError(
                        "UPSTREAM_TOOL_COLLISION",
                        f"Duplicate upstream tool name after namespacing: {public_name}",
                        category="configuration",
                    )
                seen_public_names.add(public_name)
                registered.append(
                    UpstreamTool(
                        public_name=public_name,
                        remote_name=remote_name,
                        definition=namespaced_tool_definition(config.alias, public_name, raw_tool),
                    )
                )
            self.clients[config.alias] = client
            with self._tool_lock:
                for tool in registered:
                    self.tools[tool.public_name] = tool
            status.initialized = True
            status.tool_count = len(registered)
        except (OSError, UpstreamError, UpstreamConfigError) as exc:
            if client is not None:
                client.close()
            status.error = error_payload(exc)

    def _config_by_alias(self, alias: str) -> UpstreamServerConfig | None:
        for config in self.configs:
            if config.alias == alias:
                return config
        return None

    def _visible(self, tool: UpstreamTool, tool_profile: str) -> bool:
        if tool_profile == "read-only":
            annotations = tool.definition.get("annotations")
            return isinstance(annotations, dict) and annotations.get("readOnlyHint") is True
        return True


def load_upstream_configs(path: str) -> list[UpstreamServerConfig]:
    servers = _read_upstream_servers(path)
    return [parse_server_config(alias, value) for alias, value in servers.items()]


def load_upstream_configs_tolerant(path: str) -> LoadedUpstreamConfigs:
    try:
        servers = _read_upstream_servers(path)
    except UpstreamConfigError as exc:
        return LoadedUpstreamConfigs(
            configs=[],
            invalid_statuses=[
                UpstreamStatus(
                    alias="__config__",
                    transport="configuration",
                    enabled=False,
                    error=error_payload(exc),
                    target=str(Path(path).expanduser()),
                )
            ],
        )
    configs: list[UpstreamServerConfig] = []
    invalid_statuses: list[UpstreamStatus] = []
    for index, (alias, value) in enumerate(servers.items(), start=1):
        try:
            configs.append(parse_server_config(alias, value))
        except UpstreamConfigError as exc:
            status_alias = alias if isinstance(alias, str) and alias else f"__invalid_{index}"
            invalid_statuses.append(invalid_config_status(status_alias, value, exc))
    return LoadedUpstreamConfigs(configs=configs, invalid_statuses=invalid_statuses)


def _read_upstream_servers(path: str) -> dict[str, Any]:
    config_path = Path(path).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UpstreamConfigError(f"Could not read upstream config {path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UpstreamConfigError(f"Upstream config {path!r} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise UpstreamConfigError("Upstream config must be a JSON object.")
    servers = raw.get("servers", raw)
    if not isinstance(servers, dict):
        raise UpstreamConfigError("Upstream config must contain a servers object.")
    return servers


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
        raise UpstreamConfigError(f"Upstream {alias!r} transport must be streamable_http or stdio.")
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
        raise UpstreamConfigError(f"Upstream {alias!r} requires url for streamable_http transport.")
    return UpstreamServerConfig(
        alias=alias,
        transport=transport,
        enabled=bool(value.get("enabled", True)),
        url=_optional_str(value.get("url")),
        command=command,
        args=args,
        env=_env_dict(value.get("env"), field_name="env", alias=alias),
        headers=_string_dict(value.get("headers"), field_name="headers", alias=alias),
        authorization_env=_optional_str(value.get("authorization_env")),
        include_tools=_string_tuple(value.get("include_tools"), field_name="include_tools", alias=alias),
        exclude_tools=_string_tuple(value.get("exclude_tools"), field_name="exclude_tools", alias=alias),
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


def filter_tools(tools: list[dict[str, Any]], config: UpstreamServerConfig) -> list[dict[str, Any]]:
    included = set(config.include_tools)
    excluded = set(config.exclude_tools)
    result: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        if included and name not in included:
            continue
        if name in excluded:
            continue
        result.append(tool)
    return result


def namespaced_tool_definition(alias: str, public_name: str, tool: dict[str, Any]) -> dict[str, Any]:
    definition = dict(tool)
    remote_name = str(tool.get("name"))
    definition["name"] = public_name
    title = tool.get("title")
    definition["title"] = str(title) if isinstance(title, str) and title else f"{alias}: {remote_name}"
    description = tool.get("description")
    prefix = f"Proxied upstream MCP tool {remote_name!r} from {alias!r}."
    definition["description"] = f"{prefix} {description}" if isinstance(description, str) and description else prefix
    if not isinstance(definition.get("inputSchema"), dict):
        definition["inputSchema"] = loose_object_schema()
    annotations = definition.get("annotations")
    definition["annotations"] = dict(annotations) if isinstance(annotations, dict) else {}
    return definition


def profiled_definition(definition: dict[str, Any], tool_profile: str) -> dict[str, Any]:
    result = json.loads(json.dumps(definition))
    if tool_profile == "compat-readonly-all":
        annotations = result.get("annotations")
        if not isinstance(annotations, dict):
            annotations = {}
            result["annotations"] = annotations
        annotations.update({"readOnlyHint": True, "destructiveHint": False})
    return result


def normalize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return upstream_error_result("UPSTREAM_PROTOCOL_ERROR", "Upstream tools/call result was not an object.", category="protocol")
    normalized = dict(result)
    content = normalized.get("content")
    if not isinstance(content, list):
        structured = normalized.get("structuredContent")
        normalized["content"] = [
            {"type": "text", "text": json.dumps(structured if structured is not None else normalized, ensure_ascii=False)}
        ]
    if "isError" not in normalized:
        normalized["isError"] = False
    return normalized


def upstream_error_result(
    code: str,
    message: str,
    *,
    category: str = "runtime",
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "code": code,
        "message": message,
        "category": category,
        "retryable": retryable,
    }
    if details:
        payload["details"] = details
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": payload,
        "isError": True,
    }


def decode_http_rpc_response(raw: bytes, content_type: str) -> dict[str, Any]:
    text = raw.decode("utf-8")
    if "text/event-stream" not in content_type:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "Upstream HTTP response JSON was not an object.", category="protocol")
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if not data_lines:
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "Upstream SSE response did not include data lines.", category="protocol")
    parsed = json.loads("\n".join(data_lines))
    if not isinstance(parsed, dict):
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "Upstream SSE data was not a JSON object.", category="protocol")
    return parsed


def loose_object_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": True}


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


def invalid_config_status(alias: str, value: Any, exc: BaseException) -> UpstreamStatus:
    return UpstreamStatus(
        alias=alias,
        transport=best_effort_transport(value),
        enabled=best_effort_enabled(value),
        error=error_payload(exc),
        target=best_effort_target(value),
    )


def best_effort_transport(value: Any) -> str:
    if isinstance(value, dict):
        transport = value.get("transport")
        if isinstance(transport, str) and transport:
            return "streamable_http" if transport == "http" else transport
    return "configuration"


def best_effort_enabled(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("enabled", True))
    return False


def best_effort_target(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    transport = best_effort_transport(value)
    if transport == "stdio":
        command = value.get("command")
        if not isinstance(command, str):
            return None
        args = value.get("args")
        preview_args = [item for item in args[:3] if isinstance(item, str)] if isinstance(args, list) else []
        suffix = " ..." if isinstance(args, list) and len(args) > 3 else ""
        return f"{command} {' '.join(preview_args)}{suffix}".strip()
    url = value.get("url")
    if not isinstance(url, str) or not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    redacted = parsed._replace(query="", fragment="")
    return urllib.parse.urlunsplit(redacted)


def validate_stdio_launch(alias: str, command: str | None, args: tuple[str, ...]) -> None:
    if not command or not command.strip():
        raise UpstreamConfigError(f"Upstream {alias!r} requires command for stdio transport.")
    command_text = command.strip()
    if "\n" in command_text or "\r" in command_text:
        raise UpstreamConfigError(f"Upstream {alias!r} command must be a single executable path or name.")
    first_word = command_text.split()[0].strip('"\'').lower()
    leaf = command_text.strip('"\'').replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first_word in FORBIDDEN_STDIO_COMMANDS or leaf in FORBIDDEN_STDIO_COMMANDS:
        raise UpstreamConfigError(f"Upstream {alias!r} command cannot be a shell interpreter.")
    if SHELL_FRAGMENT_RE.search(command_text):
        raise UpstreamConfigError(f"Upstream {alias!r} command cannot contain shell control syntax.")
    for index, arg in enumerate(args):
        if "\n" in arg or "\r" in arg or SHELL_FRAGMENT_RE.search(arg):
            raise UpstreamConfigError(f"Upstream {alias!r} args[{index}] cannot contain shell control syntax.")


def resolve_env_config(env_config: dict[str, Any], *, secret_resolver: Callable[[str], str] | None = None) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for name, value in env_config.items():
        if isinstance(value, str):
            resolved[name] = value
            continue
        if not isinstance(value, dict):
            raise UpstreamConfigError(f"Environment value for {name!r} must be a string or reference object.")
        env_ref = value.get("env_ref")
        secret_ref = value.get("secret_ref")
        if isinstance(env_ref, str) and env_ref:
            resolved[name] = os.environ.get(env_ref, "")
            continue
        if isinstance(secret_ref, str) and secret_ref:
            if secret_resolver is None:
                raise UpstreamConfigError("secret_ref requires a configured secret vault.")
            resolved[name] = secret_resolver(secret_ref)
            continue
        raise UpstreamConfigError(f"Environment reference for {name!r} must contain env_ref or secret_ref.")
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
            payload["details"] = exc.details
        return payload
    if isinstance(exc, UpstreamConfigError):
        return {"code": "UPSTREAM_CONFIG_INVALID", "message": str(exc), "category": "configuration", "retryable": False}
    return {"code": "UPSTREAM_INITIALIZATION_FAILED", "message": str(exc), "category": "runtime", "retryable": True}


def drain_queue(items: queue.Queue[str]) -> list[str]:
    drained: list[str] = []
    while True:
        try:
            drained.append(items.get_nowait())
        except queue.Empty:
            return drained


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
        raise UpstreamConfigError(f"Upstream {alias!r} field {field_name} must be a list of strings.")
    return tuple(value)


def _string_dict(value: Any, *, field_name: str, alias: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise UpstreamConfigError(f"Upstream {alias!r} field {field_name} must be an object with string keys and values.")
    return dict(value)


def _env_dict(value: Any, *, field_name: str, alias: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UpstreamConfigError(f"Upstream {alias!r} field {field_name} must be an object.")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise UpstreamConfigError(f"Upstream {alias!r} field {field_name} must use string keys.")
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
