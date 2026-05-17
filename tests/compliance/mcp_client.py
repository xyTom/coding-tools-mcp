from __future__ import annotations

import json
import os
import select
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-06-18"

REQUIRED_TOOLS = (
    "read_file",
    "list_dir",
    "list_files",
    "search_text",
    "apply_patch",
    "exec_command",
    "write_stdin",
    "kill_session",
    "git_status",
    "git_diff",
    "request_permissions",
    "view_image",
)

FORBIDDEN_TOOL_NAMES = {
    "codex",
    "codex_reply",
    "codex-reply",
    "web_search",
    "browser.search",
    "image_generation",
    "subagent",
    "spawn_subagent",
    "model_selection",
    "plugin_marketplace",
}

FORBIDDEN_TOOL_TERMS = (
    "memory",
    "login",
    "keyring",
    "cloud_task",
    "remote_task",
    "web_search",
    "image_generation",
    "connector_install",
    "marketplace",
)


class MCPTransportError(AssertionError):
    """Raised when the compliance harness cannot connect to the MCP server."""


class MCPError(Exception):
    def __init__(self, error: dict[str, Any]):
        self.error = error
        super().__init__(json.dumps(error, sort_keys=True))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def default_server_command(workspace: Path, port: int) -> list[str]:
    template = os.environ.get(
        "CODEX_TOOL_RUNTIME_SERVER_CMD",
        "{python} -m codex_tool_runtime_mcp --workspace {workspace} --host 127.0.0.1 --port {port}",
    )
    rendered = template.format(
        python=shlex.quote(sys.executable),
        workspace=shlex.quote(str(workspace)),
        port=port,
    )
    return shlex.split(rendered)


@dataclass
class MCPClient:
    workspace: Path
    url: str | None = None
    process: subprocess.Popen[str] | None = None
    session_id: str | None = None
    request_id: int = 0
    initialized: bool = False

    def __enter__(self) -> "MCPClient":
        if self.url is None:
            self.url = os.environ.get("CODEX_TOOL_RUNTIME_SERVER_URL")
        if self.url:
            self.initialize()
            return self

        port = free_port()
        self.url = os.environ.get("CODEX_TOOL_RUNTIME_MCP_URL", f"http://127.0.0.1:{port}/mcp")
        cmd = default_server_command(self.workspace, port)
        if not cmd or shutil.which(cmd[0]) is None:
            raise MCPTransportError(
                "MCP server command is unavailable. Set CODEX_TOOL_RUNTIME_SERVER_CMD "
                "or CODEX_TOOL_RUNTIME_SERVER_URL. Default command: "
                + " ".join(cmd or ["<empty>"])
            )

        env = os.environ.copy()
        env.update(
            {
                "AWS_SECRET_ACCESS_KEY": "COMPLIANCE_SHOULD_NOT_LEAK",
                "OPENAI_API_KEY": "COMPLIANCE_SHOULD_NOT_LEAK",
                "CODEX_TOOL_RUNTIME_WORKSPACE": str(self.workspace),
            }
        )
        self.process = subprocess.Popen(
            cmd,
            cwd=str(self.workspace),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.time() + float(os.environ.get("CODEX_TOOL_RUNTIME_STARTUP_TIMEOUT", "10"))
        last_error: Exception | None = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise MCPTransportError(self._process_exit_message(cmd))
            try:
                self.initialize()
                return self
            except Exception as exc:  # noqa: BLE001 - startup retry needs the last failure
                last_error = exc
                time.sleep(0.1)
        raise MCPTransportError(
            f"Timed out waiting for MCP server at {self.url}; last error={last_error!r}; "
            f"command={' '.join(cmd)}"
        )

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _process_exit_message(self, cmd: list[str]) -> str:
        stdout = ""
        stderr = ""
        if self.process is not None:
            try:
                stdout, stderr = self.process.communicate(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        return (
            "MCP server exited during startup. "
            f"command={' '.join(cmd)} stdout={stdout[-1000:]!r} stderr={stderr[-1000:]!r}"
        )

    def close(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=2)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()

    def initialize(self) -> dict[str, Any]:
        if self.initialized:
            return {}
        result = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "codex-tool-runtime-compliance",
                    "version": "0.1",
                },
            },
        )
        self.notify("notifications/initialized", {})
        self.initialized = True
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.rpc("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise AssertionError(f"tools/list result must contain tools array, got {result!r}")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.request_id += 1
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params or {},
            }
        )
        if "error" in response:
            raise MCPError(response["error"])
        if "result" not in response:
            raise AssertionError(f"JSON-RPC response lacks result/error: {response!r}")
        result = response["result"]
        if not isinstance(result, dict):
            raise AssertionError(f"JSON-RPC result must be an object, got {result!r}")
        return result

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.url is None:
            raise MCPTransportError("MCP client URL was not initialized")
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                body = response.read()
                if response.status in (202, 204) or not body:
                    return {}
                content_type = response.headers.get("Content-Type", "")
                text = body.decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as parse_exc:
                raise MCPTransportError(f"HTTP {exc.code} from MCP server: {body[:1000]!r}") from parse_exc
            return parsed
        except OSError as exc:
            raise MCPTransportError(f"Could not POST to MCP server at {self.url}: {exc}") from exc

        if "text/event-stream" in content_type:
            return parse_sse_json(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPTransportError(f"MCP server returned non-JSON response: {text[:1000]!r}") from exc

    def stdout_snapshot(self) -> str:
        if self.process is None or self.process.stdout is None:
            return ""
        return self._stream_snapshot(self.process.stdout)

    def stderr_snapshot(self) -> str:
        if self.process is None or self.process.stderr is None:
            return ""
        return self._stream_snapshot(self.process.stderr)

    def _stream_snapshot(self, stream: Any) -> str:
        chunks: list[str] = []
        while True:
            readable, _, _ = select.select([stream], [], [], 0)
            if not readable:
                break
            chunk = os.read(stream.fileno(), 4096).decode("utf-8", errors="replace")
            if not chunk:
                break
            chunks.append(chunk)
        return "".join(chunks)


def parse_sse_json(text: str) -> dict[str, Any]:
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            value = line.removeprefix("data:").strip()
            if value and value != "[DONE]":
                data_lines.append(value)
    if not data_lines:
        return {}
    try:
        return json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        for line in data_lines:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise MCPTransportError(f"Could not parse JSON from SSE response: {text[:1000]!r}")
