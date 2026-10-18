"""Real Git regressions for executable configuration and immutable scopes."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.executor import WorkspaceExecutor
from coding_tools_mcp.git_helpers import GitHelperView, GitProbeFailure, _copy_metadata, _MetadataBudget, create_git_helper_view
from coding_tools_mcp.policy import IsolationConfig, compile_policy
from coding_tools_mcp.server import Runtime


@unittest.skipUnless(shutil.which("git"), "Git is required")
class GitHelperSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="git-helper-security-")
        self.addCleanup(self.temporary.cleanup)
        # macOS exposes its temp directory through /var -> /private/var. Use
        # the real location so broker fixtures do not traverse that alias.
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.git = shutil.which("git") or "git"
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.marker = self.base / "outside-workspace-marker"
        self.executor = WorkspaceExecutor(
            lambda purpose: compile_policy(IsolationConfig(), self.workspace, self.base / "runtime", purpose=purpose),
            lambda: dict(self.env),
        )

    def git_run(self, *arguments: str, cwd: Path | None = None, env: dict[str, str] | None = None,
                check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([self.git, "-C", str(cwd or self.workspace), *arguments],
                              env=env or self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    def initialize(self, *, object_format: str | None = None, cwd: Path | None = None) -> None:
        arguments = ["init", "-q"]
        if object_format:
            arguments.append(f"--object-format={object_format}")
        self.git_run(*arguments, cwd=cwd)
        self.git_run("config", "user.name", "Synthetic Security Fixture", cwd=cwd)
        self.git_run("config", "user.email", "fixture@example.invalid", cwd=cwd)
        # Diff assertions intentionally exercise byte output. Define fixture
        # bytes explicitly instead of Windows write_text newline translation.
        (cwd or self.workspace).joinpath("sample.txt").write_bytes(b"one\n")
        self.git_run("add", "sample.txt", cwd=cwd)
        self.git_run("commit", "-qm", "Synthetic baseline", cwd=cwd)

    def filter_command(self, *, marker: Path | None = None, cwd: Path | None = None) -> str:
        script = (cwd or self.workspace) / "synthetic_filter.py"
        script.write_text("from pathlib import Path\nimport shutil, sys\n"
                          f"Path({str(marker or self.marker)!r}).write_text('executed')\n"
                          "shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)\n")
        # Git executes configuration strings with its POSIX shell on Windows
        # too. These are fixture paths, not commands supplied by the user.
        return shlex.quote(sys.executable.replace("\\", "/")) + " " + shlex.quote(script.as_posix())

    def make_view(self, *arguments: str, cwd: Path | None = None,
                  env: dict[str, str] | None = None) -> tuple[GitHelperView, list[str]]:
        actual_cwd = cwd or self.workspace
        command, clean_env, _ = self.executor._prepare(
            [self.git, "-C", str(actual_cwd), *arguments], "read-helper", env or self.env)

        def probe(argv: list[str], probe_env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(argv, cwd=actual_cwd, env=probe_env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        view = create_git_helper_view(command, clean_env, cwd=actual_cwd, run_probe=probe)
        self.assertIsNotNone(view)
        assert view is not None
        self.addCleanup(view.cleanup)
        return view, command

    def test_clean_and_process_filters_never_run_for_public_git_tools(self) -> None:
        self.initialize()
        command = self.filter_command()
        (self.workspace / "sample.txt").write_bytes(b"two\n")
        self.git_run("config", "filter.clean-driver.clean", command)
        self.git_run("config", "filter.clean-driver.required", "true")
        self.git_run("config", "filter.process-driver.process", command)
        self.git_run("config", "filter.process-driver.required", "true")
        (self.workspace / ".gitattributes").write_text("*.txt filter=clean-driver\n")
        info = self.workspace / ".git" / "info" / "attributes"
        for driver in ("clean-driver", "process-driver"):
            with self.subTest(driver=driver):
                info.write_text(f"[attr]synthetic filter={driver}\nsample.txt synthetic\n")
                with patch.dict(os.environ, self.env, clear=True):
                    runtime = Runtime(self.workspace)
                    try:
                        self.assertTrue(runtime.git_status({})["is_repo"])
                        diff = runtime.git_diff({"include_untracked": False})["diff"]
                        self.assertIn("-one\n+two", diff)
                        self.assertTrue(runtime.git_log({})["commits"])
                        self.assertIn("Synthetic baseline", runtime.git_show({})["content"])
                        self.assertTrue(runtime.git_blame({"path": "sample.txt", "end_line": 1})["lines"])
                    finally:
                        runtime.close()
                self.assertFalse(self.marker.exists(), driver)

    def test_config_include_and_new_driver_after_snapshot_cannot_reenable_execution(self) -> None:
        self.initialize()
        include = self.base / "included-config"
        include.write_text('[diff]\n algorithm = patience\n[filter "initial"]\n clean = false\n')
        self.git_run("config", "include.path", str(include))
        (self.workspace / "sample.txt").write_bytes(b"two\n")
        view, command = self.make_view("diff")
        original_config = self.workspace / ".git" / "config"
        original_bytes = original_config.read_bytes()
        # This deterministic mutation occurs after all config probes and before
        # the final Git launch, including a previously unseen driver name.
        malicious = self.filter_command()
        self.git_run("config", "filter.new-driver.process", malicious)
        self.git_run("config", "filter.new-driver.clean", malicious)
        self.git_run("config", "filter.new-driver.required", "true")
        include.write_text('[filter "included-new-driver"]\n clean = ' + malicious + '\n required = true\n')
        (self.workspace / ".gitattributes").write_text("*.txt filter=new-driver\n")
        (self.workspace / ".git" / "info" / "attributes").write_text("*.txt filter=included-new-driver\n")
        completed = subprocess.run(command, cwd=self.workspace, env=view.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertIn(b"-one\n+two", completed.stdout)
        self.assertFalse(self.marker.exists())
        config = self.git_run("config", "--null", "--list", "--show-scope", env=view.env).stdout
        self.assertIn(b"local\0diff.algorithm\npatience\0", config)
        self.assertNotIn(b"filter.", config)
        self.assertNotIn(b"include.path", config)
        # Only the fixture's explicit mutations changed the source config.
        self.assertTrue(original_config.read_bytes().startswith(original_bytes))

    def test_scope_snapshot_preserves_protected_trust_and_value_order(self) -> None:
        self.initialize()
        system = self.base / "system-config"
        global_config = self.base / "global-config"
        system.write_text('[safe]\n directory = /synthetic-system\n[fixture]\n value = system\n')
        global_config.write_text('[safe]\n directory =\n directory = /synthetic-global\n'
                                 '[fixture]\n value = global\n')
        self.git_run("config", "safe.directory", "*")
        self.git_run("config", "fixture.value", "local")
        source = {**self.env, "GIT_CONFIG_SYSTEM": str(system), "GIT_CONFIG_GLOBAL": str(global_config),
                  "GIT_CONFIG_NOSYSTEM": "0"}
        view, _ = self.make_view("status", env=source)
        config = self.git_run("config", "--null", "--list", "--show-scope", env=view.env).stdout
        for record in (b"system\0fixture.value\nsystem\0", b"global\0fixture.value\nglobal\0",
                       b"local\0fixture.value\nlocal\0", b"local\0safe.directory\n*\0"):
            self.assertIn(record, config)
        self.assertIn(b"global\0safe.directory\n\0global\0safe.directory\n/synthetic-global\0", config)
        self.assertEqual(system.read_text(), '[safe]\n directory = /synthetic-system\n[fixture]\n value = system\n')

    def test_local_safe_directory_is_not_promoted_to_a_protected_scope(self) -> None:
        self.initialize()
        self.git_run("config", "safe.directory", "*")
        source = {**self.env, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
        with self.assertRaises(ToolFailure) as caught:
            self.make_view("diff", env=source)
        self.assertEqual(caught.exception.code, "GIT_ERROR")
        self.assertIsInstance(caught.exception, GitProbeFailure)
        assert isinstance(caught.exception, GitProbeFailure)
        self.assertIn(b"dubious ownership", caught.exception.completed.stderr)

    def test_protected_safe_directory_remains_effective(self) -> None:
        self.initialize()
        system = self.base / "system-config"
        system.write_text('[safe]\n directory = ' + str(self.workspace).replace("\\", "/") + '\n')
        source = {**self.env, "GIT_CONFIG_SYSTEM": str(system), "GIT_CONFIG_NOSYSTEM": "0",
                  "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
        view, command = self.make_view("status", "--short", env=source)
        completed = subprocess.run(command, cwd=self.workspace, env=view.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_original_worktree_config_is_not_reopened(self) -> None:
        self.initialize()
        self.git_run("config", "extensions.worktreeConfig", "true")
        worktree_config = self.workspace / ".git" / "config.worktree"
        worktree_config.write_text('[diff]\n algorithm = histogram\n')
        (self.workspace / "sample.txt").write_bytes(b"two\n")
        view, command = self.make_view("diff")
        worktree_config.write_text('[filter "worktree-driver"]\n clean = ' + self.filter_command() + '\n required = true\n')
        (self.workspace / ".gitattributes").write_text("*.txt filter=worktree-driver\n")
        completed = subprocess.run(command, cwd=self.workspace, env=view.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertIn(b"-one\n+two", completed.stdout)
        self.assertFalse(self.marker.exists())
        config = self.git_run("config", "--null", "--list", "--show-scope", env=view.env).stdout
        self.assertIn(b"local\0diff.algorithm\nhistogram\0", config)
        self.assertNotIn(b"worktree\0", config)

    def test_sha256_and_linked_worktree_preserve_index_head_and_objects(self) -> None:
        for object_format, expected_length in (("sha1", 40), ("sha256", 64)):
            with self.subTest(object_format=object_format):
                source = self.base / object_format
                source.mkdir()
                self.initialize(object_format=object_format, cwd=source)
                linked = self.base / (object_format + "-linked")
                self.git_run("worktree", "add", "-qb", "linked", str(linked), cwd=source)
                (linked / "sample.txt").write_bytes(b"two\n")
                view, command = self.make_view("diff", cwd=linked)
                completed = subprocess.run(command, cwd=linked, env=view.env,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                self.assertIn(b"-one\n+two", completed.stdout)
                head = self.git_run("rev-parse", "HEAD", cwd=linked, env=view.env).stdout.strip()
                self.assertEqual(len(head), expected_length)
                top = self.git_run("rev-parse", "--show-toplevel", cwd=linked, env=view.env).stdout.strip()
                self.assertEqual(Path(os.fsdecode(top)).resolve(strict=True), linked.resolve(strict=True))

    def test_non_repository_no_index_uses_sanitized_global_config(self) -> None:
        global_config = self.base / "global-config"
        global_config.write_text('[filter "global-driver"]\n clean = ' + self.filter_command() + '\n required = true\n')
        (self.workspace / ".gitattributes").write_text("*.txt filter=global-driver\n")
        (self.workspace / "sample.txt").write_bytes(b"synthetic untracked\n")
        view, command = self.make_view("diff", "--no-index", "--", os.devnull, "sample.txt",
                                       env={**self.env, "GIT_CONFIG_GLOBAL": str(global_config)})
        self.assertNotIn("GIT_COMMON_DIR", view.env)
        completed = subprocess.run(command, cwd=self.workspace, env=view.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn(b"synthetic untracked", completed.stdout)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.workspace / ".git").exists())

    def test_bare_repository_remains_bare_with_pinned_git_directory(self) -> None:
        self.git_run("init", "--bare", "-q")
        view, command = self.make_view("rev-parse", "--is-bare-repository", "--is-inside-work-tree")
        self.assertEqual(Path(view.env["GIT_DIR"]).resolve(strict=True), self.workspace.resolve(strict=True))
        completed = subprocess.run(command, cwd=self.workspace, env=view.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertEqual(completed.stdout, b"true\nfalse\n")

    def test_gitfile_change_after_discovery_cannot_redirect_head_or_index(self) -> None:
        self.initialize()
        expected_head = self.git_run("rev-parse", "HEAD").stdout.strip()
        linked = self.base / "linked"
        self.git_run("worktree", "add", "-qb", "linked", str(linked))
        alternate = self.base / "alternate"
        alternate.mkdir()
        self.initialize(cwd=alternate)
        (alternate / "sample.txt").write_bytes(b"different index\n")
        self.git_run("commit", "-am", "Different synthetic head", cwd=alternate)
        (linked / "sample.txt").write_bytes(b"two\n")
        view, command = self.make_view("diff", cwd=linked)
        gitfile = linked / ".git"
        # Windows Git metadata can carry hidden/read-only attributes. Open
        # the existing file instead of CREATE_ALWAYS, which refuses a hidden
        # destination; still change the real pointer before the final launch.
        gitfile.chmod(0o600)
        with gitfile.open("r+b") as stream:
            stream.write(("gitdir: " + (alternate / ".git").as_posix() + "\n").encode("utf-8"))
            stream.truncate()
        completed = subprocess.run(command, cwd=linked, env=view.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertIn(b"-one\n+two", completed.stdout)
        self.assertEqual(self.git_run("rev-parse", "HEAD", cwd=linked, env=view.env).stdout.strip(), expected_head)

    def test_malformed_protected_config_fails_without_launching_original_diff(self) -> None:
        self.initialize()
        system = self.base / "system-config"
        system.write_text("[broken\n")
        with self.assertRaises(ToolFailure) as caught:
            self.make_view("diff", env={**self.env, "GIT_CONFIG_SYSTEM": str(system), "GIT_CONFIG_NOSYSTEM": "0"})
        self.assertEqual(caught.exception.code, "GIT_ERROR")
        self.assertIsInstance(caught.exception, GitProbeFailure)
        assert isinstance(caught.exception, GitProbeFailure)
        self.assertIn(b"bad config line", caught.exception.completed.stderr)

    def test_submodule_filter_is_not_executed_by_parent_status_or_diff(self) -> None:
        self.initialize()
        child = self.workspace / "submodule"
        child.mkdir()
        self.initialize(cwd=child)
        child_head = self.git_run("rev-parse", "HEAD", cwd=child).stdout.decode().strip()
        self.git_run("update-index", "--add", "--cacheinfo", "160000," + child_head + ",submodule")
        self.git_run("commit", "-qm", "Track synthetic submodule")
        self.git_run("config", "filter.child-driver.clean", self.filter_command(cwd=child), cwd=child)
        self.git_run("config", "filter.child-driver.required", "true", cwd=child)
        (child / ".gitattributes").write_text("*.txt filter=child-driver\n")
        (child / "sample.txt").write_bytes(b"two\n")
        with patch.dict(os.environ, self.env, clear=True):
            runtime = Runtime(self.workspace)
            try:
                self.assertTrue(runtime.git_status({})["is_repo"])
                runtime.git_diff({"include_untracked": False})
            finally:
                runtime.close()
        self.assertFalse(self.marker.exists())

    def test_private_view_is_deleted_and_does_not_mutate_repository_config(self) -> None:
        self.initialize()
        original = (self.workspace / ".git" / "config").read_bytes()
        view, _ = self.make_view("status")
        self.assertFalse(view.root.is_relative_to(self.workspace))
        if os.name != "nt":
            self.assertEqual(view.root.stat().st_mode & 0o777, 0o700)
        root = view.root
        view.cleanup()
        self.assertFalse(root.exists())
        self.assertEqual((self.workspace / ".git" / "config").read_bytes(), original)

    def test_storage_validator_runs_before_any_metadata_is_referenced(self) -> None:
        self.initialize()
        command, env, _ = self.executor._prepare([self.git, "-C", str(self.workspace), "status"],
                                                "read-helper", self.env)

        def probe(argv: list[str], probe_env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(argv, env=probe_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def deny(git_dir: Path, common: Path) -> None:
            self.assertEqual(git_dir, (self.workspace / ".git").resolve())
            self.assertEqual(common, git_dir)
            raise ToolFailure("EXTERNAL_GIT_STORAGE_DENIED", "Synthetic policy denial", category="security")

        with patch("coding_tools_mcp.git_helpers._metadata_reference") as reference:
            with self.assertRaises(ToolFailure) as caught:
                create_git_helper_view(command, env, cwd=self.workspace, run_probe=probe, validate_storage=deny)
            self.assertEqual(caught.exception.code, "EXTERNAL_GIT_STORAGE_DENIED")
            reference.assert_not_called()

    def test_metadata_copy_rejects_links_and_has_a_shared_size_limit(self) -> None:
        source = self.base / "metadata"
        source.mkdir()
        outside = self.base / "outside-reference"
        outside.write_bytes(b"must not be copied")
        link = source / "external-link"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Host cannot create the synthetic metadata symlink")
        with self.assertRaises(ToolFailure) as caught:
            _copy_metadata(source, self.base / "copied-metadata", _MetadataBudget())
        self.assertEqual(caught.exception.code, "GIT_ERROR")
        self.assertFalse((self.base / "copied-metadata" / "external-link").exists())
        link.unlink()
        (source / "one-ref").write_bytes(b"12345678")
        (source / "two-ref").write_bytes(b"12345678")
        with patch("coding_tools_mcp.git_helpers._MAX_METADATA_BYTES", 12):
            with self.assertRaises(ToolFailure) as limited:
                _copy_metadata(source, self.base / "limited-copy", _MetadataBudget())
        self.assertEqual(limited.exception.code, "GIT_ERROR")

    def test_snapshot_preserves_quoted_subsection_and_multiline_values(self) -> None:
        self.initialize()
        key = 'fixture.subsection.with"quote.value'
        value = 'unicode 文件\nwith "quotes", tab\tand \\backslash'
        self.git_run("config", key, value)
        view, _ = self.make_view("status")
        actual = self.git_run("config", "--get", key, env=view.env).stdout.decode().rstrip("\n")
        self.assertEqual(actual, value)


if __name__ == "__main__":
    unittest.main()
