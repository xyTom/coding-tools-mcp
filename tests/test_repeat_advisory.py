from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coding_tools_mcp.protocol import JsonRpcError, RequestContext
from coding_tools_mcp.server import Runtime


class RepeatAdvisoryTests(unittest.TestCase):
    def test_ten_identical_failures_execute_and_show_advice_in_text(self) -> None:
        """Repeated failures remain real handler outcomes, with visible nonblocking advice."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                handler = Mock(wraps=runtime._tool_handlers["read_file"])
                with patch.dict(runtime._tool_handlers, {"read_file": handler}):
                    results = [runtime.call_tool("read_file", {"path": "missing"}) for _ in range(10)]
                    self.assertEqual(handler.call_count, 10)
                for result in results:
                    self.assertTrue(result["isError"])
                    self.assertEqual(result["structuredContent"]["error"]["code"], "NOT_FOUND")
                for number, result in enumerate(results[1:], 2):
                    details = result["structuredContent"]["error"]["details"]
                    self.assertEqual(details["recent_identical_failures"], number)
                    self.assertIn(details["repeat_warning"], result["content"][0]["text"])
                    self.assertNotIn("will be refused", result["content"][0]["text"])
            finally:
                runtime.close()

    def test_external_creation_recovers_immediately_and_success_resets_advice(self) -> None:
        """An external state change needs neither changed arguments nor a cooldown."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root, permission_mode="safe")
            try:
                for _ in range(3):
                    runtime.call_tool("read_file", {"path": "late"})
                (root / "late").write_text("ready\n", encoding="utf-8")
                for _ in range(10):
                    self.assertFalse(runtime.call_tool("read_file", {"path": "late"})["isError"])
                (root / "late").unlink()
                result = runtime.call_tool("read_file", {"path": "late"})
                self.assertNotIn("repeat_warning", result["structuredContent"]["error"]["details"])
            finally:
                runtime.close()

    def test_one_clients_failures_do_not_block_anothers_first_call(self) -> None:
        """Shared diagnostics never turn another client's first call into a refusal."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                first = RequestContext(client_info={"name": "client-A", "version": "1"})
                second = RequestContext(client_info={"name": "client-B", "version": "1"})
                for _ in range(2):
                    runtime.call_tool("read_file", {"path": "missing"}, context=first)
                result = runtime.call_tool("read_file", {"path": "missing"}, context=second)
                self.assertEqual(result["structuredContent"]["error"]["code"], "NOT_FOUND")
                self.assertIn("server", result["structuredContent"]["error"]["details"]["repeat_warning"])
            finally:
                runtime.close()

    def test_repetition_never_bypasses_path_or_required_field_validation(self) -> None:
        """Removing the duplicate veto leaves actual access and schema checks intact."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                for _ in range(10):
                    result = runtime.call_tool("read_file", {"path": "../outside"})
                    self.assertTrue(result["isError"])
                    self.assertEqual(result["structuredContent"]["error"]["code"], "PATH_OUTSIDE_WORKSPACE")
                    with self.assertRaises(JsonRpcError):
                        runtime.call_tool("read_file", {})
            finally:
                runtime.close()
