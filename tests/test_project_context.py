"""Project-context fallback stays bounded without losing relevant instructions."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.file_broker import FileBroker, broker_supported
from coding_tools_mcp.project_context import load_project_context


@unittest.skipUnless(broker_supported(), "POSIX handle-relative file broker")
class ProjectContextBrokerTests(unittest.TestCase):
    def test_skipped_and_deep_directories_do_not_consume_scan_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "app").mkdir()
            (root / "app" / "AGENTS.md").write_text("relevant instructions")
            for relative in ("node_modules", "zdeep/child/too-deep"):
                directory = root / relative
                directory.mkdir(parents=True)
                for index in range(3):
                    (directory / str(index)).write_text("irrelevant")
            with FileBroker(root) as broker, \
                    patch("coding_tools_mcp.project_context._git_context_files", return_value=None), \
                    patch("coding_tools_mcp.project_context.MAX_CONTEXT_SCAN_FILES", 2), \
                    patch("coding_tools_mcp.project_context.MAX_CONTEXT_SCAN_DEPTH", 2):
                context = load_project_context(root, broker=broker)
            self.assertEqual(context.nested_files, ("app/AGENTS.md",))
            self.assertEqual(context.warnings, ())
