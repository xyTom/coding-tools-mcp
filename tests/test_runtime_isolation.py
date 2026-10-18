"""Strict structured-tool integration; native process acceptance is separate."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.file_broker import broker_supported
from coding_tools_mcp.policy import IsolationConfig
from coding_tools_mcp.project_context import ProjectContext
from coding_tools_mcp.server import Runtime, WorkspaceMutationPolicy


@unittest.skipUnless(broker_supported(), "POSIX handle-relative file broker")
class RuntimeIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.secret = self.root / "service-private"
        self.secret.mkdir()
        (self.secret / "credential").write_text("never disclose")
        (self.root / "source.txt").write_text("hello\n")
        self.runtime = Runtime(self.root, isolation=IsolationConfig(mode="strict", deny_roots=(self.secret,)),
                               project_context=ProjectContext((), (), ()))
        self.addCleanup(self.runtime.close)
        self.runtime.workspace.git_path = None

    def test_read_and_write_tools_share_denied_roots(self) -> None:
        self.assertEqual(self.runtime.read_file({"path": "source.txt"})["content"], "hello\n")
        for action in (
            lambda: self.runtime.read_file({"path": "service-private/credential"}),
            lambda: self.runtime.apply_patch({"patch": "*** Begin Patch\n*** Add File: service-private/new\n+evil\n*** End Patch"}),
        ):
            with self.assertRaises(ToolFailure):
                action()
        self.assertFalse((self.secret / "new").exists())
        self.assertEqual((self.secret / "credential").read_text(), "never disclose")

    def test_listing_and_python_search_cannot_disclose_denied_files(self) -> None:
        listing = self.runtime.list_dir({"recursive": True, "max_depth": 3, "include_hidden": True, "include_ignored": True})
        self.assertNotIn("service-private", {entry["path"] for entry in listing["entries"]})
        with patch("coding_tools_mcp.server.cached_which", return_value=None):
            files = self.runtime.list_files({"include_hidden": True, "include_ignored": True, "patterns": ["**"]})
            search = self.runtime.search_text({"query": "never"})
        self.assertEqual([entry["path"] for entry in files["files"]], ["source.txt"])
        self.assertEqual(search["matches"], [])

    def test_staged_patch_commit_uses_broker(self) -> None:
        result = self.runtime.apply_patch({"patch": "*** Begin Patch\n*** Update File: source.txt\n@@\n-hello\n+world\n*** Add File: nested/new.txt\n+created\n*** End Patch"})
        self.assertIn("affected_files", result)
        self.assertEqual(self.runtime.read_file({"path": "source.txt"})["content"], "world\n")
        self.assertEqual((self.root / "nested/new.txt").read_text(), "created\n")

    def test_symlink_targets_never_reach_content_or_image_readers(self) -> None:
        outside = self.root.parent / "outside"
        outside.write_text("secret")
        (self.root / "link").symlink_to(outside)
        for tool in (self.runtime.read_file, self.runtime.view_image):
            with self.assertRaises(ToolFailure):
                tool({"path": "link"})
        self.assertEqual(outside.read_text(), "secret")

    def test_strict_startup_refuses_unavailable_backend(self) -> None:
        with patch("coding_tools_mcp.executor.WorkspaceExecutor._backend", side_effect=ToolFailure("SANDBOX_UNAVAILABLE", "test backend", category="security")):
            with self.assertRaises(ToolFailure) as caught:
                Runtime(self.root, isolation=IsolationConfig(mode="strict"))
        self.assertEqual(caught.exception.code, "SANDBOX_UNAVAILABLE")

    def test_unresolvable_write_path_fails_before_command_state_is_allocated(self) -> None:
        loop = self.root / "loop"
        loop.symlink_to(loop)
        with patch("coding_tools_mcp.server.WorkspaceCommandManager") as manager:
            with self.assertRaises(ToolFailure) as caught:
                Runtime(self.root, isolation=IsolationConfig(mode="strict"),
                        workspace_mutation=WorkspaceMutationPolicy(mode="structured-only", write_paths=("loop",)))
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        manager.assert_not_called()

    def test_compatibility_still_drops_unresolvable_write_paths(self) -> None:
        loop = self.root / "loop"
        loop.symlink_to(loop)
        runtime = Runtime(self.root, project_context=ProjectContext((), (), ()),
                          workspace_mutation=WorkspaceMutationPolicy(mode="structured-only", write_paths=("loop",)))
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.workspace_write_paths(), [])

    def test_runtime_maintenance_cannot_follow_command_created_symlinks(self) -> None:
        import os
        import stat
        self.runtime._ensure_runtime_dirs()
        outside = self.root.parent / "outside-private"
        outside.mkdir(mode=0o755)
        os.chmod(outside, 0o755)
        for name in ("home", "tmp", "cache"):
            with self.subTest(name=name):
                target = self.runtime.runtime_dir / name
                target.rmdir()
                target.symlink_to(outside, target_is_directory=True)
                try:
                    with self.assertRaises(ToolFailure) as caught:
                        self.runtime._ensure_runtime_dirs()
                    self.assertEqual(caught.exception.code, "RUNTIME_DIR_UNWRITABLE")
                    self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
                finally:
                    target.unlink()
                    target.mkdir(mode=0o700)

    def test_fd_output_is_rechecked_by_broker_before_metadata_disclosure(self) -> None:
        import subprocess
        result = subprocess.CompletedProcess(["fd"], 0, "service-private/credential\nsource.txt\n", "")
        with patch("coding_tools_mcp.server.cached_which", return_value="fd"), \
                patch.object(self.runtime.executor, "run", return_value=result):
            listing = self.runtime.list_files({"include_hidden": True, "include_ignored": True, "patterns": ["**"]})
        self.assertEqual([item["path"] for item in listing["files"]], ["source.txt"])

    def test_missing_file_retains_public_not_found_error(self) -> None:
        for path in ("missing", "missing-parent/missing"):
            with self.subTest(path=path), self.assertRaises(ToolFailure) as caught:
                self.runtime.read_file({"path": path})
            self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_older_fd_retry_stays_on_the_unified_executor(self) -> None:
        import subprocess
        for parse_error_code in (1, 2):
            with self.subTest(parse_error_code=parse_error_code):
                responses = [subprocess.CompletedProcess(["fd"], parse_error_code, "", "unknown option --no-require-git"),
                             subprocess.CompletedProcess(["fd"], 0, "source.txt\n", "")]
                with patch("coding_tools_mcp.server.cached_which", return_value="fd"), \
                        patch.object(self.runtime.executor, "run", side_effect=responses) as run:
                    result = self.runtime.list_files({"patterns": ["**"], "include_ignored": True})
                self.assertEqual(result["engine"], "fd")
                self.assertEqual(run.call_count, 2)
                self.assertNotIn("--no-require-git", run.call_args.args[0])

    def test_diagnostics_do_not_claim_unconfirmed_or_offline_capabilities(self) -> None:
        self.runtime.allow_network = True
        status = self.runtime.server_info_payload()
        self.assertFalse(status["network_allowed"])
        self.assertEqual(status["home"], "/tmp/home")
        self.assertEqual(status["tmpdir"], "/tmp")
        self.assertFalse(status["execution_isolation"]["last_launch_confirmed"])
        self.assertFalse(status["workspace_mutation_policy"]["enforced"])
