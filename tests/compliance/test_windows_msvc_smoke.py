from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-06-18"
ROOT = Path(__file__).resolve().parents[2]


def has_msvc_environment() -> bool:
    return (
        sys.platform == "win32"
        and shutil.which("cl.exe") is not None
        and bool(os.environ.get("INCLUDE"))
        and bool(os.environ.get("LIB"))
    )


class WindowsMsvcEnvironmentSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        if not has_msvc_environment():
            self.skipTest("requires Windows with vcvars initialized for cl.exe")

    def test_core_env_does_not_accidentally_inherit_msvc_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_hello_c(workspace)
            with StdioMCPClient(workspace, shell_env_inherit="core") as client:
                info = structured_payload(client.call_tool("server_info", {}))
                self.assertEqual(info.get("shell_env_inherit"), "core")

                result = client.call_tool(
                    "exec_command",
                    {
                        "cmd": "cl.exe /nologo hello.c",
                        "timeout_ms": 30000,
                        "yield_time_ms": 30000,
                        "max_output_bytes": 20000,
                    },
                )
                payload = assert_tool_success(self, result)
                output = (payload.get("stdout") or "") + (payload.get("stderr") or "")
                self.assertNotEqual(payload.get("exit_code"), 0, output)
                self.assertFalse((workspace / "hello.exe").exists(), output)
                self.assertRegex(output.lower(), r"(stdio\.h|c1083|include|cannot open)")

    def test_inherit_all_preserves_msvc_environment_for_single_file_compile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_hello_c(workspace)
            with StdioMCPClient(workspace, shell_env_inherit="all") as client:
                info = structured_payload(client.call_tool("server_info", {}))
                self.assertEqual(info.get("shell_env_inherit"), "all")

                compile_result = client.call_tool(
                    "exec_command",
                    {
                        "cmd": "cl.exe /nologo hello.c",
                        "timeout_ms": 30000,
                        "yield_time_ms": 30000,
                        "max_output_bytes": 20000,
                    },
                )
                compile_payload = assert_tool_success(self, compile_result)
                compile_output = (compile_payload.get("stdout") or "") + (compile_payload.get("stderr") or "")
                self.assertEqual(compile_payload.get("exit_code"), 0, compile_output)
                self.assertTrue((workspace / "hello.exe").exists(), compile_output)

                run_result = client.call_tool(
                    "exec_command",
                    {
                        "cmd": "hello.exe",
                        "timeout_ms": 30000,
                        "yield_time_ms": 30000,
                        "max_output_bytes": 20000,
                    },
                )
                run_payload = assert_tool_success(self, run_result)
                self.assertEqual(run_payload.get("exit_code"), 0, run_payload)
                self.assertIn("ok", str(run_payload.get("stdout") or ""))


def write_hello_c(workspace: Path) -> None:
    (workspace / "hello.c").write_text(
        '#include <stdio.h>\n\nint main(void) {\n    puts("ok");\n    return 0;\n}\n',
        encoding="utf-8",
    )


class StdioMCPClient:
    def __init__(self, workspace: Path, *, shell_env_inherit: str) -> None:
        self.workspace = workspace
        self.shell_env_inherit = shell_env_inherit
        self.process: subprocess.Popen[str] | None = None
        self.request_id = 0
        self.stdout_lines: queue.Queue[str] = queue.Queue()
        self.stderr_lines: list[str] = []

    def __enter__(self) -> StdioMCPClient:
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(ROOT) if not existing_pythonpath else str(ROOT) + os.pathsep + existing_pythonpath
        kwargs: dict[str, Any] = {}
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if creation_flag:
            kwargs["creationflags"] = creation_flag
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "coding_tools_mcp",
                "--workspace",
                str(self.workspace),
                "--stdio",
                "--shell-env-inherit",
                self.shell_env_inherit,
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
                "clientInfo": {"name": "windows-msvc-smoke", "version": "0.1"},
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


def assert_tool_success(testcase: unittest.TestCase, result: dict[str, Any]) -> dict[str, Any]:
    testcase.assertFalse(result.get("isError", False), result)
    payload = structured_payload(result)
    testcase.assertIsInstance(payload, dict, result)
    return payload


def structured_payload(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                parsed = json.loads(item["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}
