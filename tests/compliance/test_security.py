from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from tests.compliance.mcp_client import MCPClient
from tests.compliance.test_support import ComplianceTestCase


class SecurityComplianceTests(ComplianceTestCase):
    fixture_name = "malicious-project"

    def test_path_traversal_absolute_paths_and_symlink_escape_are_rejected(self) -> None:
        self.assert_denied_or_permission_required("read_file", {"path": "../outside-secret.txt"})
        self.assert_denied_or_permission_required("read_file", {"path": str(self.workspace.outside_secret)})
        self.assert_denied_or_permission_required("read_file", {"path": "outside-link.txt"})

        absolute_patch = f"""*** Begin Patch
*** Add File: {self.workspace.outside_secret}
+unsafe
*** End Patch
"""
        self.assert_denied_or_permission_required("apply_patch", {"patch": absolute_patch})

        traversal_patch = """*** Begin Patch
*** Update File: ../outside-secret.txt
@@
-TOP_SECRET_DO_NOT_READ
+unsafe
*** End Patch
"""
        self.assert_denied_or_permission_required("apply_patch", {"patch": traversal_patch})

        symlink_patch = """*** Begin Patch
*** Update File: outside-link.txt
@@
-TOP_SECRET_DO_NOT_READ
+unsafe
*** End Patch
"""
        self.assert_denied_or_permission_required("apply_patch", {"patch": symlink_patch})

    def test_exec_workdir_shell_escape_destructive_and_network_are_rejected(self) -> None:
        self.assert_denied_or_permission_required("exec_command", {"cmd": "pwd", "workdir": ".."})
        self.assert_denied_or_permission_required("exec_command", {"cmd": "cat ../outside-secret.txt"})
        self.assert_denied_or_permission_required("exec_command", {"cmd": "cat outside-link.txt"})
        self.assert_denied_or_permission_required("exec_command", {"cmd": "rm -rf /"})
        self.assert_denied_or_permission_required("exec_command", {"cmd": "git reset --hard"})
        self.assert_denied_or_permission_required("exec_command", {"cmd": "chmod -R 777 /"})
        self.assert_denied_or_permission_required(
            "exec_command",
            {
                "cmd": "python -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=1)\"",
                "timeout_ms": 3000,
            },
        )

    def test_exec_command_rejects_interpreter_mediated_outside_reads(self) -> None:
        outside_secret = repr(str(self.workspace.outside_secret))
        self.assert_denied_or_permission_required(
            "exec_command",
            {
                "cmd": (
                    "python -c "
                    f"\"from pathlib import Path; print(Path({outside_secret}).read_text())\""
                ),
                "timeout_ms": 5000,
                "max_output_bytes": 4096,
            },
        )
        computed = self.client.call_tool(
            "exec_command",
            {
                "cmd": (
                    "python -c "
                    "\"import os; "
                    "print(open(os.path.dirname(os.getcwd()) + chr(47) + 'outside-secret.txt').read())\""
                ),
                "timeout_ms": 5000,
                "max_output_bytes": 4096,
            },
        )
        payload = self.assert_tool_success(computed)
        self.assertNotEqual(payload.get("exit_code"), 0, f"computed outside read should fail: {payload!r}")
        self.assertNotIn("TOP_SECRET_DO_NOT_READ", self.tool_text(computed))
        direct_syscall = self.client.call_tool(
            "exec_command",
            {
                "cmd": (
                    "python -c "
                    "\"import os, ctypes as c; "
                    "lib=c.CDLL(None, use_errno=True); "
                    "p=(os.path.dirname(os.getcwd()) + chr(47) + 'outside-secret.txt').encode(); "
                    "fd=lib.syscall(257, -100, c.c_char_p(p), 0); "
                    "raise OSError(c.get_errno(), 'blocked') if fd < 0 else print(os.read(fd, 80))\""
                ),
                "timeout_ms": 5000,
                "max_output_bytes": 4096,
            },
        )
        syscall_payload = self.assert_tool_success(direct_syscall)
        self.assertNotEqual(syscall_payload.get("exit_code"), 0, f"direct syscall outside read should fail: {syscall_payload!r}")
        self.assertNotIn("TOP_SECRET_DO_NOT_READ", self.tool_text(direct_syscall))
        direct_syscall_write = self.client.call_tool(
            "exec_command",
            {
                "cmd": (
                    "python -c "
                    "\"import os, ctypes as c; "
                    "lib=c.CDLL(None, use_errno=True); "
                    "p=(os.path.dirname(os.getcwd()) + chr(47) + 'outside-secret.txt').encode(); "
                    "fd=lib.syscall(257, -100, c.c_char_p(p), os.O_WRONLY | os.O_TRUNC); "
                    "raise OSError(c.get_errno(), 'blocked') if fd < 0 else os.write(fd, b'MODIFIED')\""
                ),
                "timeout_ms": 5000,
                "max_output_bytes": 4096,
            },
        )
        syscall_write_payload = self.assert_tool_success(direct_syscall_write)
        self.assertNotEqual(
            syscall_write_payload.get("exit_code"),
            0,
            f"direct syscall outside write should fail: {syscall_write_payload!r}",
        )
        self.assertEqual(self.workspace.outside_secret.read_text(encoding="utf-8"), "TOP_SECRET_DO_NOT_READ\n")
        self.assert_denied_or_permission_required(
            "exec_command",
            {
                "cmd": "cat $(dirname \"$PWD\")/outside-secret.txt",
                "timeout_ms": 5000,
                "max_output_bytes": 4096,
            },
        )

    def test_exec_command_long_non_path_tokens_do_not_become_internal_errors(self) -> None:
        result = self.client.call_tool(
            "exec_command",
            {"cmd": "python -c " + ("a" * 300), "timeout_ms": 5000, "max_output_bytes": 4096},
        )
        payload = self.assert_tool_success(result)
        self.assertEqual(payload.get("status"), "exited", payload)
        self.assertNotEqual(payload.get("exit_code"), 0, payload)

    def test_exec_command_rejects_destructive_workspace_mutations(self) -> None:
        dangerous_commands = [
            "rm -rf src",
            "git -C . reset --hard",
            "find . -maxdepth 1 -type f -delete",
        ]
        for cmd in dangerous_commands:
            with self.subTest(cmd=cmd):
                with self.session_for_fixture("tiny-js-project") as (_workspace, client):
                    result = client.call_tool(
                        "exec_command",
                        {"cmd": cmd, "timeout_ms": 5000, "max_output_bytes": 4096},
                    )
                    self.assertTrue(result.get("isError", False), f"destructive command must be denied: {result!r}")

    def test_exec_command_rejects_obfuscated_network_access(self) -> None:
        result = self.client.call_tool(
            "exec_command",
            {
                "cmd": (
                    "python -c "
                    "\"import http.client; "
                    "c=http.client.HTTPConnection('127.0.0.1', 9, timeout=0.2); "
                    "c.request('GET', '/')\""
                ),
                "timeout_ms": 3000,
                "max_output_bytes": 4096,
            },
        )
        self.assertTrue(result.get("isError", False), f"network command must be denied before execution: {result!r}")

    def test_exec_command_rejects_shell_startup_and_loader_environment(self) -> None:
        risky_env = {
            "BASH_ENV": "malicious-startup.sh",
            "ENV": "malicious-startup.sh",
            "LD_PRELOAD": "malicious-loader.so",
            "PYTHONPATH": ".",
        }
        for key, value in risky_env.items():
            with self.subTest(env=key):
                result = self.client.call_tool("exec_command", {"cmd": "true", "env": {key: value}})
                self.assertTrue(result.get("isError", False), f"{key} must require permission or be denied: {result!r}")

    def test_exec_command_timeout_is_enforced_after_running_session_is_returned(self) -> None:
        started = self.client.call_tool(
            "exec_command",
            {
                "cmd": "python -c \"import time; time.sleep(5)\"",
                "timeout_ms": 100,
                "yield_time_ms": 0,
                "max_output_bytes": 4096,
            },
        )
        payload = self.assert_tool_success(started)
        session_id = payload.get("session_id")
        self.assertIsInstance(session_id, str, f"running command should expose a session id: {payload!r}")

        try:
            time.sleep(0.35)
            polled = self.client.call_tool(
                "write_stdin",
                {"session_id": session_id, "chars": "", "yield_time_ms": 0, "max_output_bytes": 4096},
            )
            poll_payload = self.assert_tool_success(polled)
            self.assertNotEqual(
                poll_payload.get("status"),
                "running",
                f"command should have timed out after the configured deadline: {poll_payload!r}",
            )
            self.assertTrue(
                poll_payload.get("timed_out") or poll_payload.get("status") in {"timeout", "exited"},
                f"timeout should be explicit after deadline: {poll_payload!r}",
            )
        finally:
            if isinstance(session_id, str):
                self.client.call_tool("kill_session", {"session_id": session_id, "signal": "KILL"})

    def test_exec_command_timeout_is_enforced_without_client_polling(self) -> None:
        started = self.client.call_tool(
            "exec_command",
            {
                "cmd": "python -c \"import time; time.sleep(2)\"",
                "tty": True,
                "timeout_ms": 100,
                "yield_time_ms": 0,
                "max_output_bytes": 4096,
            },
        )
        payload = self.assert_tool_success(started)
        session_id = payload.get("session_id")
        self.assertIsInstance(session_id, str, payload)

        time.sleep(0.35)
        polled = self.client.call_tool(
            "write_stdin",
            {"session_id": session_id, "chars": "", "yield_time_ms": 0, "max_output_bytes": 4096},
        )
        poll_payload = self.assert_tool_success(polled)
        self.assertEqual(poll_payload.get("status"), "timeout", poll_payload)
        self.assertIs(poll_payload.get("timed_out"), True, poll_payload)

    def test_exec_command_long_running_output_buffer_is_bounded(self) -> None:
        started = self.client.call_tool(
            "exec_command",
            {
                "cmd": (
                    "python -c \"import sys, time; "
                    "sys.stdout.write('x' * 1500000); sys.stdout.flush(); time.sleep(2)\""
                ),
                "tty": True,
                "timeout_ms": 5000,
                "yield_time_ms": 0,
                "max_output_bytes": 1024,
            },
        )
        payload = self.assert_tool_success(started)
        session_id = payload.get("session_id")
        self.assertIsInstance(session_id, str, payload)
        time.sleep(0.5)
        polled = self.client.call_tool(
            "write_stdin",
            {"session_id": session_id, "chars": "", "yield_time_ms": 0, "max_output_bytes": 1024},
        )
        poll_payload = self.assert_tool_success(polled)
        self.assertGreater(poll_payload.get("stdout_dropped_bytes", 0), 0, poll_payload)
        self.assertTrue(poll_payload.get("truncated"), poll_payload)
        self.client.call_tool("kill_session", {"session_id": session_id, "signal": "KILL"})

    def test_sensitive_environment_is_not_leaked_to_child_processes(self) -> None:
        result = self.client.call_tool(
            "exec_command",
            {
                "cmd": "python -c \"import os; print(os.environ.get('AWS_SECRET_ACCESS_KEY', ''))\"",
                "timeout_ms": 5000,
                "max_output_bytes": 4096,
            },
        )
        payload = self.assert_tool_success(result)
        self.assertEqual(payload.get("exit_code"), 0)
        self.assertNotIn("COMPLIANCE_SHOULD_NOT_LEAK", self.tool_text(result))

    def test_read_only_tools_do_not_follow_symlink_escape_fixture(self) -> None:
        listed = self.client.call_tool("list_dir", {"path": ".", "include_hidden": True})
        self.assert_tool_success(listed)
        self.assertNotIn("TOP_SECRET_DO_NOT_READ", self.tool_text(listed))

        files = self.client.call_tool("list_files", {"glob": "**/*", "include_hidden": True})
        payload = self.assert_tool_success(files)
        paths = {entry.get("path") for entry in payload.get("files", []) if isinstance(entry, dict)}
        self.assertNotIn("outside-link.txt", paths)
        self.assertNotIn("TOP_SECRET_DO_NOT_READ", self.tool_text(files))

        search = self.client.call_tool("search_text", {"query": "TOP_SECRET_DO_NOT_READ", "path": "."})
        payload = self.assert_tool_success(search)
        self.assertEqual(payload.get("matches"), [])
        self.assertEqual(payload.get("total_matches"), 0)

    def test_request_permissions_does_not_silently_grant_dangerous_operations(self) -> None:
        result = self.client.call_tool(
            "request_permissions",
            {
                "tool_name": "exec_command",
                "permission": "network",
                "reason": "verify compliance denial shape",
                "arguments": {"cmd": "curl https://example.com"},
            },
        )
        self.assertTrue(result.get("isError"), f"permission request must not silently grant access: {result!r}")
        payload = self.assert_denied_or_permission_required(
            "request_permissions",
            {
                "tool_name": "exec_command",
                "permission": "destructive_command",
                "reason": "verify compliance denial shape",
                "arguments": {"cmd": "git reset --hard"},
            },
        )
        self.assertFalse(payload.get("ok", True))

    def test_stdout_json_rpc_pollution_is_absent(self) -> None:
        stdout = self.client.stdout_snapshot()
        self.assertEqual(stdout, "", f"stdout must stay clean for JSON-RPC compatibility: {stdout!r}")

    def test_concurrent_read_only_tool_calls_are_stable(self) -> None:
        def call(index: int) -> str:
            with MCPClient(self.workspace.root, url=self.client.url) as client:
                if index % 2 == 0:
                    result = client.call_tool("read_file", {"path": "inside.txt"})
                else:
                    result = client.call_tool("search_text", {"query": "safe workspace", "path": "."})
                return self.tool_text(result)

        with ThreadPoolExecutor(max_workers=6) as executor:
            outputs = list(executor.map(call, range(12)))
        self.assertEqual(len(outputs), 12)
        self.assertTrue(all("safe workspace" in output for output in outputs))
