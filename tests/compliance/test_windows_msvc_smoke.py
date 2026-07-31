from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.compliance.mcp_client import StdioMCPClient
from tests.compliance.test_support import structured_payload


def has_msvc_environment() -> bool:
    return (
        sys.platform == "win32"
        and shutil.which("cl.exe") is not None
        and bool(os.environ.get("INCLUDE"))
        and bool(os.environ.get("LIB"))
    )


class WindowsProcessSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        if sys.platform != "win32":
            self.skipTest("requires Windows")

    def test_windows_tty_request_is_explicitly_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with StdioMCPClient(workspace) as client:
                result = client.call_tool(
                    "exec_command",
                    {
                        "cmd": "echo tty-check",
                        "tty": True,
                        "timeout_ms": 5000,
                        "yield_time_ms": 0,
                    },
                )
                self.assertIs(result.get("isError"), True, result)
                payload = structured_payload(result)
                self.assertEqual(payload.get("error", {}).get("code"), "TTY_UNSUPPORTED")

    def test_windows_force_kill_cleans_background_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            command = subprocess.list2cmdline(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            with StdioMCPClient(
                workspace,
                extra_args=["--permission-mode", "trusted"],
            ) as client:
                started = assert_tool_success(
                    self,
                    client.call_tool(
                        "exec_command",
                        {
                            "cmd": command,
                            "timeout_ms": 60_000,
                            "yield_time_ms": 0,
                        },
                    ),
                )
                self.assertEqual(started.get("status"), "running", started)
                session_id = started.get("session_id")
                self.assertIsInstance(session_id, str, started)

                killed = assert_tool_success(
                    self,
                    client.call_tool(
                        "kill_session",
                        {"session_id": session_id, "signal": "KILL", "wait_ms": 5000},
                    ),
                )
                self.assertIn(killed.get("status"), {"killed", "exited"}, killed)


class WindowsMsvcEnvironmentSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        if not has_msvc_environment():
            self.skipTest("requires Windows with vcvars initialized for cl.exe")

    def test_core_env_does_not_accidentally_inherit_msvc_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_hello_c(workspace)
            with StdioMCPClient(workspace, extra_args=["--shell-env-inherit", "core"]) as client:
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
            with StdioMCPClient(workspace, extra_args=["--shell-env-inherit", "all"]) as client:
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


def assert_tool_success(testcase: unittest.TestCase, result: dict[str, Any]) -> dict[str, Any]:
    testcase.assertFalse(result.get("isError", False), result)
    payload = structured_payload(result)
    testcase.assertIsInstance(payload, dict, result)
    return payload
