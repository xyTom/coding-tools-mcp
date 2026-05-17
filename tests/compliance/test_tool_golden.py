from __future__ import annotations

from typing import Any

from tests.compliance.mcp_client import MCPError
from tests.compliance.test_support import ComplianceTestCase


ADD_FIX_PATCH = """*** Begin Patch
*** Update File: src/math.js
@@
 export function add(a, b) {
-  return a - b;
+  return a + b;
 }
*** End Patch
"""


class ReadFileGoldenTests(ComplianceTestCase):
    def test_read_file_normal_line_range_truncation_binary_and_escape(self) -> None:
        result = self.client.call_tool("read_file", {"path": "src/math.js"})
        payload = self.assert_tool_success(result)
        self.assertEqual(payload.get("path"), "src/math.js")
        self.assertEqual(payload.get("encoding"), "utf-8")
        self.assertEqual(payload.get("start_line"), 1)
        self.assertEqual(payload.get("total_lines"), 7)
        self.assertIs(payload.get("truncated"), False)
        self.assertIn("return a - b", self.tool_text(result))

        result = self.client.call_tool("read_file", {"path": "src/math.js", "start_line": 1, "end_line": 3})
        payload = self.assert_tool_success(result)
        self.assertEqual(payload.get("start_line"), 1)
        self.assertEqual(payload.get("end_line"), 3)
        text = self.tool_text(result)
        self.assertIn("function add", text)
        self.assertNotIn("multiply", text)

        result = self.client.call_tool("read_file", {"path": "src/large.txt", "max_bytes": 80})
        payload = self.assert_tool_success(result)
        self.assertLessEqual(len(self.tool_text(result).encode("utf-8")), 200)
        self.assertTrue(payload.get("truncated", True), f"large read should report truncation: {payload!r}")

        self.assert_denied_or_permission_required("read_file", {"path": "assets/raw.bin"})
        self.assert_denied_or_permission_required("read_file", {"path": "../outside-secret.txt"})


class ListAndSearchGoldenTests(ComplianceTestCase):
    def test_list_dir_and_list_files_exclude_defaults_and_truncate(self) -> None:
        result = self.client.call_tool("list_dir", {"path": "."})
        text = self.tool_text(result)
        self.assertIn("src", text)
        for excluded in (".git", ".reference", "node_modules", "dist", "ignored.log"):
            self.assertNotIn(excluded, text)

        files = self.client.call_tool("list_files", {"glob": "**/*.js", "max_results": 2})
        payload = self.assert_tool_success(files)
        self.assertIn("src/math.js", self.tool_text(files))
        entries = payload.get("files") or payload.get("entries") or []
        if isinstance(entries, list):
            self.assertLessEqual(len(entries), 2)
        self.assertTrue(payload.get("truncated", True), f"max_results should report truncation: {payload!r}")

        all_files = self.client.call_tool("list_files", {"glob": "**/*"})
        all_text = self.tool_text(all_files)
        self.assertNotIn("ignored.log", all_text)
        self.assertNotIn("node_modules", all_text)
        self.assert_denied_or_permission_required("list_dir", {"path": ".."})

    def test_search_text_query_glob_context_and_max_results(self) -> None:
        result = self.client.call_tool(
            "search_text",
            {"query": "function add", "path": ".", "glob": "**/*.js", "context_lines": 1, "max_results": 10},
        )
        payload = self.assert_tool_success(result)
        self.assertEqual(payload.get("query"), "function add")
        self.assertEqual(payload.get("total_matches"), 1)
        self.assertIs(payload.get("truncated"), False)
        text = self.tool_text(result)
        self.assertIn("src/math.js", text)
        self.assertIn("return a - b", text)
        assert_search_entries_have_shape(self, payload)

        miss = self.client.call_tool("search_text", {"query": "function add", "glob": "**/*.py"})
        self.assertNotIn("src/math.js", self.tool_text(miss))

        truncated = self.client.call_tool("search_text", {"query": "common-token", "max_results": 3})
        payload = self.assert_tool_success(truncated)
        self.assertTrue(payload.get("truncated", True), f"search max_results should report truncation: {payload!r}")


class ApplyPatchGoldenTests(ComplianceTestCase):
    def test_apply_patch_add_update_delete_move_and_context_mismatch(self) -> None:
        add = """*** Begin Patch
*** Add File: docs/NOTES.md
+# Notes
+
+Added by apply_patch golden test.
*** End Patch
"""
        self.assert_tool_success(self.client.call_tool("apply_patch", {"patch": add}))
        self.assertIn("Added by apply_patch", self.tool_text(self.client.call_tool("read_file", {"path": "docs/NOTES.md"})))
        self.assert_tool_error("apply_patch", {"patch": add})

        with self.session_for_fixture("tiny-js-project") as (_workspace, client):
            dry_run_add = """*** Begin Patch
*** Add File: dry-run/new/NOPE.md
+dry run only
*** End Patch
"""
            self.assert_tool_success(client.call_tool("apply_patch", {"patch": dry_run_add, "dry_run": True}))
            self.assert_tool_error("read_file", {"path": "dry-run/new/NOPE.md"})
            self.assert_tool_error("list_dir", {"path": "dry-run"})

        self.assert_tool_success(self.client.call_tool("apply_patch", {"patch": ADD_FIX_PATCH}))
        self.assertIn("return a + b", self.tool_text(self.client.call_tool("read_file", {"path": "src/math.js"})))

        delete = """*** Begin Patch
*** Delete File: TODO.md
*** End Patch
"""
        self.assert_tool_success(self.client.call_tool("apply_patch", {"patch": delete}))
        self.assert_tool_error("read_file", {"path": "TODO.md"})

        with self.session_for_fixture("tiny-js-project") as (_workspace, client):
            move = """*** Begin Patch
*** Update File: TODO.md
*** Move to: docs/TODO.md
*** End Patch
"""
            self.assert_tool_success(client.call_tool("apply_patch", {"patch": move}))
            moved = client.call_tool("read_file", {"path": "docs/TODO.md"})
            self.assertIn("Keep this file available", self.tool_text(moved))

        mismatch = """*** Begin Patch
*** Update File: src/math.js
@@
-  return no_such_context;
+  return a + b;
*** End Patch
"""
        self.assert_tool_error("apply_patch", {"patch": mismatch})

    def test_apply_patch_rejects_absolute_traversal_and_symlink_escape(self) -> None:
        absolute = f"""*** Begin Patch
*** Add File: {self.workspace.outside_secret}
+unsafe
*** End Patch
"""
        self.assert_denied_or_permission_required("apply_patch", {"patch": absolute})

        traversal = """*** Begin Patch
*** Add File: ../outside-secret.txt
+unsafe
*** End Patch
"""
        self.assert_denied_or_permission_required("apply_patch", {"patch": traversal})

        with self.session_for_fixture("malicious-project") as (_workspace, client):
            symlink = """*** Begin Patch
*** Update File: outside-link.txt
@@
-TOP_SECRET_DO_NOT_READ
+unsafe
*** End Patch
"""
            try:
                result = client.call_tool("apply_patch", {"patch": symlink})
            except MCPError:
                return
            self.assertTrue(result.get("isError"), f"symlink escape patch must fail: {result!r}")


class ExecAndGitGoldenTests(ComplianceTestCase):
    def test_exec_command_success_nonzero_timeout_output_cap_and_permissions(self) -> None:
        result = self.client.call_tool(
            "exec_command",
            {"cmd": "python -c \"print('hello from exec')\"", "timeout_ms": 5000, "max_output_bytes": 4096},
        )
        payload = self.assert_tool_success(result)
        self.assertEqual(payload.get("exit_code"), 0)
        self.assertIn("hello from exec", self.tool_text(result))

        self.assert_tool_success(self.client.call_tool("apply_patch", {"patch": ADD_FIX_PATCH}))
        npm = self.client.call_tool(
            "exec_command",
            {"cmd": "npm test", "timeout_ms": 20000, "yield_time_ms": 20000, "max_output_bytes": 20000},
        )
        self.assertEqual(self.assert_tool_success(npm).get("exit_code"), 0)

        with self.session_for_fixture("tiny-python-project") as (_workspace, client):
            patch = """*** Begin Patch
*** Update File: src/math_utils.py
@@
 def subtract(a: int, b: int) -> int:
     return a - b
+
+
+def square(value: int) -> int:
+    return value * value
*** End Patch
"""
            self.assert_tool_success(client.call_tool("apply_patch", {"patch": patch}))
            pytest = client.call_tool(
                "exec_command",
                {"cmd": "python -m pytest tests", "timeout_ms": 10000, "max_output_bytes": 20000},
            )
            self.assertEqual(self.assert_tool_success(pytest).get("exit_code"), 0)

        nonzero = self.client.call_tool("exec_command", {"cmd": "python -c \"import sys; sys.exit(7)\""})
        payload = self.assert_tool_success(nonzero)
        self.assertEqual(payload.get("exit_code"), 7)

        timeout = self.client.call_tool(
            "exec_command",
            {"cmd": "python -c \"import time; time.sleep(5)\"", "timeout_ms": 200},
        )
        timeout_payload = self.assert_tool_success(timeout)
        self.assertTrue(timeout_payload.get("timed_out", True), f"timeout should be explicit: {timeout_payload!r}")

        capped = self.client.call_tool(
            "exec_command",
            {
                "cmd": "python -c \"print('x' * 10000)\"",
                "timeout_ms": 5000,
                "max_output_bytes": 128,
            },
        )
        capped_payload = self.assert_tool_success(capped)
        self.assertTrue(capped_payload.get("truncated", True), f"output cap should be explicit: {capped_payload!r}")
        self.assertLessEqual(len(self.tool_text(capped).encode("utf-8")), 512)

        self.assert_denied_or_permission_required("exec_command", {"cmd": "pwd", "workdir": ".."})
        self.assert_denied_or_permission_required("exec_command", {"cmd": "rm -rf /"})
        self.assert_denied_or_permission_required(
            "exec_command",
            {"cmd": "python -c \"import urllib.request; urllib.request.urlopen('https://example.com')\""},
        )

    def test_write_stdin_kill_session_git_status_and_git_diff(self) -> None:
        with self.session_for_fixture("long-running-project") as (_workspace, client):
            started = client.call_tool(
                "exec_command",
                {"cmd": "python repl.py", "tty": True, "timeout_ms": 1000, "max_output_bytes": 4096},
            )
            payload = self.assert_tool_success(started)
            session_id = payload.get("session_id")
            self.assertIsInstance(session_id, str, f"long-running command must return session_id: {payload!r}")
            self.assertIn("ready", self.tool_text(started))
            hello = client.call_tool("write_stdin", {"session_id": session_id, "chars": "hello\n"})
            self.assertIn("echo:hello", self.tool_text(hello))
            client.call_tool("write_stdin", {"session_id": session_id, "chars": "exit\n"})
            killed = client.call_tool("kill_session", {"session_id": session_id})
            self.assertIn("content", killed)

        self.assert_tool_success(self.client.call_tool("apply_patch", {"patch": ADD_FIX_PATCH}))
        status = self.client.call_tool("git_status", {})
        self.assertIn("src/math.js", self.tool_text(status))
        diff = self.client.call_tool("git_diff", {"path": "src/math.js", "max_bytes": 20000})
        diff_text = self.tool_text(diff)
        self.assertIn("-  return a - b;", diff_text)
        self.assertIn("+  return a + b;", diff_text)
        filtered = self.client.call_tool("git_diff", {"path": "package.json"})
        self.assertNotIn("src/math.js", self.tool_text(filtered))


def assert_search_entries_have_shape(testcase: ComplianceTestCase, payload: dict[str, Any]) -> None:
    entries = payload.get("matches") or payload.get("results") or []
    testcase.assertIsInstance(entries, list, f"search result should expose matches/results: {payload!r}")
    if not entries:
        return
    first = entries[0]
    testcase.assertIsInstance(first.get("path"), str)
    testcase.assertIsInstance(first.get("line"), int)
    testcase.assertIsInstance(first.get("preview"), str)
