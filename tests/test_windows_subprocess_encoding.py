from __future__ import annotations

import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp.server import Runtime, Workspace


class WindowsSubprocessEncodingTests(unittest.TestCase):
    def test_git_ignore_lookup_decodes_as_utf8(self) -> None:
        workspace = Workspace(Path.cwd())
        workspace.git_path = "git"
        completed = subprocess.CompletedProcess(
            ["git"], 0, stdout="ignored-文件.txt\0", stderr=""
        )

        with patch.object(server_module.subprocess, "run", return_value=completed) as run:
            ignored = workspace.git_ignored_paths(["ignored-文件.txt"])

        self.assertEqual(ignored, {"ignored-文件.txt"})
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_fd_file_listing_decodes_as_utf8(self) -> None:
        runtime = Runtime(Path.cwd())
        runtime.workspace.git_path = None
        completed = subprocess.CompletedProcess(
            ["fd"], 0, stdout="./README.md\n", stderr=""
        )

        try:
            with (
                patch.object(server_module, "cached_which", return_value="fd"),
                patch.object(server_module.subprocess, "run", return_value=completed) as run,
            ):
                result = runtime._list_files_with_fd(
                    runtime.workspace.resolve_existing("."),
                    ["**"],
                    [],
                    include_hidden=False,
                    include_ignored=False,
                    max_results=10,
                    sort_key="path",
                )
        finally:
            runtime.close()

        self.assertIsNotNone(result)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_rg_search_decodes_as_utf8(self) -> None:
        runtime = Runtime(Path.cwd())

        class FakeProcess:
            returncode = 0
            stdout = io.StringIO("")

            def wait(self, *, timeout: float) -> int:
                return self.returncode

            def kill(self) -> None:
                pass

            def terminate(self) -> None:
                pass

        try:
            with (
                patch.object(server_module, "cached_which", return_value="rg"),
                patch.object(
                    server_module.subprocess, "Popen", return_value=FakeProcess()
                ) as popen,
            ):
                result = runtime._search_text_with_rg(
                    runtime.workspace.resolve_existing("."),
                    "query",
                    regex=False,
                    case_sensitive=False,
                    include_globs=[],
                    exclude_globs=[],
                    context_lines=0,
                    max_results=10,
                    max_preview_bytes=512,
                )
        finally:
            runtime.close()

        self.assertIsNotNone(result)
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
