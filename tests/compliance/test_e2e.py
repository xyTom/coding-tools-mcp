from __future__ import annotations

import json

from tests.compliance.mcp_client import MCPError
from tests.compliance.test_support import ComplianceTestCase


class DeterministicE2ETests(ComplianceTestCase):
    def test_js_bugfix_search_patch_test_and_diff(self) -> None:
        search = self.client.call_tool("search_text", {"query": "function add", "glob": "**/*.js"})
        self.assertIn("src/math.js", self.tool_text(search))

        source = self.client.call_tool("read_file", {"path": "src/math.js"})
        self.assertIn("return a - b", self.tool_text(source))

        patch = """*** Begin Patch
*** Update File: src/math.js
@@
 export function add(a, b) {
-  return a - b;
+  return a + b;
 }
*** End Patch
"""
        self.assert_tool_success(self.client.call_tool("apply_patch", {"patch": patch}))

        test = self.client.call_tool(
            "exec_command",
            {"cmd": "npm test", "timeout_ms": 20000, "yield_time_ms": 20000, "max_output_bytes": 20000},
        )
        self.assertEqual(self.assert_tool_success(test).get("exit_code"), 0)

        diff = self.client.call_tool("git_diff", {"max_bytes": 20000})
        text = self.tool_text(diff)
        self.assertIn("diff --git a/src/math.js b/src/math.js", text)
        self.assertIn("+  return a + b;", text)
        self.assertNotIn("package.json", text)

    def test_python_add_function_patch_test_and_diff(self) -> None:
        with self.session_for_fixture("tiny-python-project") as (_workspace, client):
            source = client.call_tool("read_file", {"path": "src/math_utils.py"})
            self.assertIn("def subtract", self.tool_text(source))

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
            test = client.call_tool(
                "exec_command",
                {"cmd": "python -m pytest tests", "timeout_ms": 10000, "max_output_bytes": 20000},
            )
            self.assertEqual(self.assert_tool_success(test).get("exit_code"), 0)
            status = client.call_tool("git_status", {})
            self.assertIn("src/math_utils.py", self.tool_text(status))
            diff = client.call_tool("git_diff", {"path": "src/math_utils.py"})
            self.assertIn("def square", self.tool_text(diff))

    def test_long_running_stdin_session(self) -> None:
        with self.session_for_fixture("long-running-project") as (_workspace, client):
            started = client.call_tool(
                "exec_command",
                {"cmd": "python repl.py", "tty": True, "timeout_ms": 1000, "max_output_bytes": 4096},
            )
            payload = self.assert_tool_success(started)
            session_id = payload.get("session_id")
            self.assertIsInstance(session_id, str)
            hello = client.call_tool("write_stdin", {"session_id": session_id, "chars": "hello\n"})
            self.assertIn("echo:hello", self.tool_text(hello))
            bye = client.call_tool("write_stdin", {"session_id": session_id, "chars": "exit\n"})
            self.assertIn("bye", self.tool_text(bye))

    def test_long_running_session_poll_exit_and_closed_stdin_error(self) -> None:
        with self.session_for_fixture("long-running-project") as (_workspace, client):
            started = client.call_tool(
                "exec_command",
                {"cmd": "python repl.py", "tty": True, "timeout_ms": 1000, "yield_time_ms": 0, "max_output_bytes": 4096},
            )
            payload = self.assert_tool_success(started)
            session_id = payload.get("session_id")
            self.assertIsInstance(session_id, str)

            poll = client.call_tool(
                "write_stdin",
                {"session_id": session_id, "chars": "", "yield_time_ms": 500, "max_output_bytes": 4096},
            )
            self.assertIn("ready", self.tool_text(started) + self.tool_text(poll))

            alpha = client.call_tool(
                "write_stdin",
                {"session_id": session_id, "chars": "alpha\n", "yield_time_ms": 1000, "max_output_bytes": 4096},
            )
            self.assertIn("echo:alpha", self.tool_text(alpha))

            closed = client.call_tool(
                "write_stdin",
                {"session_id": session_id, "chars": "exit\n", "yield_time_ms": 1000, "max_output_bytes": 4096},
            )
            self.assertIn("bye", self.tool_text(closed))
            try:
                late = client.call_tool("write_stdin", {"session_id": session_id, "chars": "late\n"})
            except MCPError:
                return
            self.assertTrue(late.get("isError"), f"write to naturally closed session must fail: {late!r}")

    def test_workspace_escape_flow_is_denied(self) -> None:
        self.assert_denied_or_permission_required("read_file", {"path": "../outside-secret.txt"})
        self.assert_denied_or_permission_required(
            "apply_patch",
            {
                "patch": "*** Begin Patch\n*** Add File: ../outside-secret.txt\n+unsafe\n*** End Patch\n",
            },
        )
        self.assert_denied_or_permission_required("exec_command", {"cmd": "cat ../outside-secret.txt"})

    def test_view_image_optional_p1_contract_when_exposed(self) -> None:
        with self.session_for_fixture("image-project") as (_workspace, client):
            names = {tool.get("name") for tool in client.list_tools()}
            if "view_image" not in names:
                self.skipTest("view_image is P1 and not exposed by this server")
            image = client.call_tool("view_image", {"path": "assets/screenshot.png"})
            payload = self.assert_tool_success(image)
            blob = self.tool_text(image)
            self.assertIn("image/png", blob)
            image_blocks = [item for item in image.get("content", []) if item.get("type") == "image"]
            self.assertEqual(len(image_blocks), 1)
            encoded = image_blocks[0].get("data")
            self.assertIsInstance(encoded, str)
            self.assertEqual(json.dumps(image).count(str(encoded)), 1)
            self.assertNotIn("base64", payload)
            self.assertNotIn("data_url", payload)
            bad = client.call_tool("view_image", {"path": "assets/not-image.txt"})
            self.assertTrue(bad.get("isError"), f"non-image input must fail: {bad!r}")
