from __future__ import annotations

import json
import os
import queue
import select
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-11-25"
ROOT = Path(__file__).resolve().parents[2]

REQUIRED_TOOLS = (
    "server_info",
    "check_exec_environment",
    "get_default_cwd",
    "set_default_cwd",
    "read_file",
    "list_dir",
    "list_files",
    "search_text",
    "apply_patch",
    "exec_command",
    "write_stdin",
    "kill_session",
    "read_output",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_blame",
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
        "CODING_TOOLS_MCP_SERVER_CMD",
        "{python} -m coding_tools_mcp --workspace {workspace} --host 127.0.0.1 --port {port}",
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
            self.url = os.environ.get("CODING_TOOLS_MCP_SERVER_URL")
        if self.url:
            self.initialize()
            return self

        port = free_port()
        self.url = os.environ.get("CODING_TOOLS_MCP_URL", f"http://127.0.0.1:{port}/mcp")
        cmd = default_server_command(self.workspace, port)
        if not cmd or shutil.which(cmd[0]) is None:
            raise MCPTransportError(
                "MCP server command is unavailable. Set CODING_TOOLS_MCP_SERVER_CMD "
                "or CODING_TOOLS_MCP_SERVER_URL. Default command: "
                + " ".join(cmd or ["<empty>"])
            )

        env = safe_server_env(
            AWS_SECRET_ACCESS_KEY="COMPLIANCE_SHOULD_NOT_LEAK",
            OPENAI_API_KEY="COMPLIANCE_SHOULD_NOT_LEAK",
            CODING_TOOLS_MCP_WORKSPACE=str(self.workspace),
        )
        self.process = subprocess.Popen(
            cmd,
            cwd=str(self.workspace),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **subprocess_group_kwargs(),
        )
        deadline = time.time() + float(os.environ.get("CODING_TOOLS_MCP_STARTUP_TIMEOUT", "10"))
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
                if os.name == "nt":
                    self.process.terminate()
                else:
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        self.process.kill()
                    else:
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
                    "name": "coding-tools-mcp-compliance",
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
        auth_token = os.environ.get("CODING_TOOLS_MCP_AUTH_TOKEN")
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            request_timeout = float(os.environ.get("CODING_TOOLS_MCP_CLIENT_TIMEOUT", "30"))
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
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
        return stream_snapshot(self.process.stdout)

    def stderr_snapshot(self) -> str:
        if self.process is None or self.process.stderr is None:
            return ""
        return stream_snapshot(self.process.stderr)


def stream_snapshot(stream: Any) -> str:
    """Drain whatever is currently readable from a pipe without blocking."""
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


def prepend_repo_pythonpath(env: dict[str, str]) -> dict[str, str]:
    """Ensure spawned server processes import the in-repo coding_tools_mcp package."""
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT) if not existing else str(ROOT) + os.pathsep + existing
    return env


def safe_server_env(**overrides: str) -> dict[str, str]:
    """Env for compliance server processes: dangerous toggles scrubbed, safe mode forced."""
    env = os.environ.copy()
    for name in (
        "CODING_TOOLS_MCP_DANGEROUSLY_SKIP_ALL_PERMISSIONS",
        "CODING_TOOLS_MCP_ALLOW_NETWORK",
    ):
        env.pop(name, None)
    env["CODING_TOOLS_MCP_PERMISSION_MODE"] = "safe"
    env.update(overrides)
    return prepend_repo_pythonpath(env)


def subprocess_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creation_flag} if creation_flag else {}
    return {"start_new_session": True}


class StdioMCPClient:
    """Newline-delimited JSON-RPC client around a `coding_tools_mcp --stdio` subprocess."""

    def __init__(self, workspace: Path, *, extra_args: list[str] | None = None) -> None:
        self.workspace = workspace
        self.extra_args = list(extra_args or [])
        self.process: subprocess.Popen[str] | None = None
        self.request_id = 0
        self.stdout_lines: queue.Queue[str] = queue.Queue()
        self.stderr_lines: list[str] = []

    def __enter__(self) -> StdioMCPClient:
        env = prepend_repo_pythonpath(os.environ.copy())
        kwargs = subprocess_group_kwargs()
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "coding_tools_mcp",
                "--workspace",
                str(self.workspace),
                "--stdio",
                *self.extra_args,
            ],
            cwd=str(self.workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            **kwargs,
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "coding-tools-mcp-stdio-client", "version": "0.1"},
            },
        )
        self.notify("notifications/initialized", {})
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _drain_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.stdout_lines.put(line)

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self.stderr_lines.append(line)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params or {},
        }
        self._send(payload)
        response = self._read_response(self.request_id)
        if "error" in response:
            raise AssertionError(f"unexpected JSON-RPC error: {response!r}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise AssertionError(f"JSON-RPC result must be an object: {response!r}")
        return result

    def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise AssertionError("stdio server was not started")
        if process.poll() is not None:
            raise AssertionError(f"stdio server exited with {process.returncode}; stderr={self.stderr_tail()!r}")
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
        deadline = time.time() + 30
        while time.time() < deadline:
            process = self.process
            if process is not None and process.poll() is not None and self.stdout_lines.empty():
                raise AssertionError(f"stdio server exited with {process.returncode}; stderr={self.stderr_tail()!r}")
            try:
                line = self.stdout_lines.get(timeout=0.2)
            except queue.Empty:
                continue
            response = json.loads(line)
            if response.get("id") == request_id:
                return response
        raise AssertionError(f"timed out waiting for JSON-RPC response {request_id}; stderr={self.stderr_tail()!r}")

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def stderr_tail(self) -> str:
        return "".join(self.stderr_lines)[-4000:]


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
