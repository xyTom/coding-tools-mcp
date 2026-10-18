"""Encoding contracts at the server-to-executor boundary.

These mocks do not replace Windows Job acceptance. Native process encoding and
Job lifecycle coverage live in test_windows_job_native and the real-byte
test_subprocess_encoding_boundaries suite.
"""
from __future__ import annotations

import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp.server import Runtime
from coding_tools_mcp.errors import ToolFailure


class WindowsSubprocessEncodingTests(unittest.TestCase):
    def test_git_ignore_lookup_decodes_as_utf8(self) -> None:
        runtime = Runtime(Path.cwd())
        self.addCleanup(runtime.close)
        workspace = runtime.workspace
        workspace.git_path = "git"
        completed = subprocess.CompletedProcess(
            ["git"], 0, stdout="ignored-文件.txt\0".encode("utf-8"), stderr=b""
        )

        with patch.object(
            workspace.executor, "run", return_value=completed
        ) as run:
            ignored = workspace.git_ignored_paths(["ignored-文件.txt"])

        self.assertEqual(ignored, {"ignored-文件.txt"})
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs["text"])
        self.assertEqual(
            run.call_args.kwargs["input"], "ignored-文件.txt\0".encode("utf-8")
        )

    def test_git_text_decodes_as_utf8(self) -> None:
        runtime = Runtime(Path.cwd())
        self.addCleanup(runtime.close)
        completed = subprocess.CompletedProcess(
            ["git"], 0, "文件.txt\n".encode("utf-8"), "警告\n".encode("utf-8")
        )
        with patch.object(runtime.executor, "run", return_value=completed) as run:
            result = runtime._run_git_text(["git", "status"])
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs["text"])
        self.assertEqual(result.stdout, "文件.txt\n")
        self.assertEqual(result.stderr, "警告\n")

    def test_git_text_preserves_newline_translation(self) -> None:
        """Binary capture retains the previous text-mode newline contract."""
        runtime = Runtime(Path.cwd())
        completed = subprocess.CompletedProcess(
            ["git"], 0, b"a\r\nb\rc\n", b"warning\r\n"
        )
        try:
            with patch.object(runtime.executor, "run", return_value=completed):
                result = runtime._run_git_text(["git", "status"])
        finally:
            runtime.close()
        self.assertEqual(result.stdout, "a\nb\nc\n")
        self.assertEqual(result.stderr, "warning\n")

    def test_git_text_invalid_stderr_is_structured_error(self) -> None:
        """Undecodable diagnostics cannot become silent replacement text."""
        runtime = Runtime(Path.cwd())
        completed = subprocess.CompletedProcess(["git"], 1, b"", b"bad \xff")
        try:
            with patch.object(runtime.executor, "run", return_value=completed):
                with self.assertRaises(ToolFailure) as caught:
                    runtime._run_git_text(["git", "status"])
        finally:
            runtime.close()
        self.assertEqual(caught.exception.code, "GIT_ERROR")

    def test_fd_file_listing_decodes_as_utf8(self) -> None:
        runtime = Runtime(Path.cwd())
        runtime.workspace.git_path = None
        completed = subprocess.CompletedProcess(
            ["fd"], 0, stdout="./README.md\n", stderr=""
        )

        try:
            with (
                patch.object(server_module, "cached_which", return_value="fd"),
                patch.object(
                    runtime.executor, "run", return_value=completed
                ) as run,
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
        run.assert_called_once()
        self.assertTrue(run.call_args.kwargs["text"])
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "strict")

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
                    runtime.executor, "popen", return_value=FakeProcess()
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
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["text"])
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "strict")


if __name__ == "__main__":
    unittest.main()
