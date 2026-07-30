from __future__ import annotations

import json

from tests.compliance.fixtures import ROOT, workspace_from_fixture
from tests.compliance.mcp_client import MCPClient, MCPError
from tests.compliance.test_support import ComplianceTestCase


VECTORS = ROOT / "tests" / "compliance" / "runtime_semantics" / "semantic_vectors.json"


class RuntimeSemanticsTests(ComplianceTestCase):
    def test_apply_patch_envelope_semantic_vectors(self) -> None:
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
        for case in vectors["apply_patch"]:
            with self.subTest(case=case["name"]):
                with workspace_from_fixture("tiny-js-project") as workspace:
                    with MCPClient(workspace.root) as client:
                        try:
                            result = client.call_tool("apply_patch", {"patch": case["patch"]})
                        except MCPError:
                            result = {"isError": True, "content": []}
                        if case["expect_success"]:
                            self.assert_tool_success(result)
                        else:
                            self.assertTrue(result.get("isError"), f"case should fail: {case!r} -> {result!r}")
                        for path, expected in case.get("expect_paths", {}).items():
                            if path.startswith("/"):
                                continue
                            read = client.call_tool("read_file", {"path": path})
                            self.assertIn(expected, self.tool_text(read))
                        for deleted in case.get("expect_deleted", []):
                            try:
                                read = client.call_tool("read_file", {"path": deleted})
                            except MCPError:
                                continue
                            self.assertTrue(read.get("isError"), f"deleted path should be unreadable: {deleted}")

    def test_command_semantics_match_runtime_exec_and_stdin(self) -> None:
        with self.session_for_fixture("long-running-project") as (_workspace, client):
            started = client.call_tool(
                "exec_command",
                {"cmd": "python repl.py", "tty": True, "timeout_ms": 1000, "max_output_bytes": 4096},
            )
            payload = self.assert_tool_success(started)
            command_id = payload.get("command_id")
            self.assertIsInstance(command_id, str)
            output = client.call_tool("write_stdin", {"command_id": command_id, "chars": "compat\n"})
            self.assertIn("echo:compat", self.tool_text(output))
            client.call_tool("write_stdin", {"command_id": command_id, "chars": "exit\n"})

    def test_missing_and_closed_commands_return_structured_errors(self) -> None:
        self.assert_denied_or_permission_required("write_stdin", {"command_id": "missing-command", "chars": "hello\n"})
        with self.session_for_fixture("long-running-project") as (_workspace, client):
            started = client.call_tool(
                "exec_command",
                {"cmd": "python repl.py", "tty": True, "timeout_ms": 1000, "max_output_bytes": 4096},
            )
            command_id = self.assert_tool_success(started).get("command_id")
            client.call_tool("kill_command", {"command_id": command_id})
            try:
                closed = client.call_tool("write_stdin", {"command_id": command_id, "chars": "after-close\n"})
            except MCPError:
                return
            self.assertTrue(closed.get("isError"), f"write to closed command must fail: {closed!r}")

    def test_view_image_semantics_when_p1_tool_is_present(self) -> None:
        with self.session_for_fixture("image-project") as (_workspace, client):
            names = {tool.get("name") for tool in client.list_tools()}
            if "view_image" not in names:
                self.skipTest("view_image P1 tool is not exposed")
            image = client.call_tool("view_image", {"path": "assets/screenshot.png"})
            self.assertRegex(self.tool_text(image), r"(data:image/png;base64|image/png)")
            try:
                denied = client.call_tool("view_image", {"path": "../outside-secret.txt"})
            except MCPError:
                return
            self.assertTrue(denied.get("isError"), f"view_image traversal must fail: {denied!r}")
