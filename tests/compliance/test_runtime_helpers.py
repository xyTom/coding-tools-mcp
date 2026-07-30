from __future__ import annotations

import builtins
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp import processes as processes_module
from coding_tools_mcp.patching import AtomicPatchCommitter, FileBaseline, StagedFile
from coding_tools_mcp.server import (
    LANDLOCK_ACCESS_FS_IOCTL_DEV,
    LANDLOCK_ACCESS_FS_TRUNCATE,
    LANDLOCK_ACCESS_FS_WRITE_FILE,
    MAX_ACTIVE_COMMANDS,
    Runtime,
    ShellEnvPolicy,
    ToolFailure,
    exec_output_diagnostics,
    guard_allow_roots,
    identify_image,
    permission_failure_diagnostics,
    runtime_parent_root,
)
from coding_tools_mcp.textutils import truncate_text_head, truncate_text_tail
from coding_tools_mcp.tool_results import (
    MODEL_TEXT_SAFETY_LIMIT_BYTES,
    make_tool_result,
)
from tests.compliance.fixtures import git_fixture_preflight_error, init_git


@contextmanager
def fake_landlock_exec() -> Iterator[dict[str, object]]:
    """Patch landlock + Popen so exec_command runs without spawning a process.

    Yields a dict capturing the landlock write_roots and the Popen args/kwargs;
    "read_fd" holds the fd handed to the server (closed by exec_command itself).
    """
    read_fd, write_fd = os.pipe()
    original_open = server_module.open_landlock_ruleset
    original_popen = server_module.subprocess.Popen
    original_watchdog = server_module.start_command_watchdog
    captured: dict[str, object] = {"read_fd": read_fd}

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        pid = 1

        def poll(self) -> int:
            return 0

    def fake_open(_workspace: Path, _read_roots: list[str], **kwargs: object) -> int:
        captured["write_roots"] = kwargs.get("write_roots")
        return read_fd

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    server_module.open_landlock_ruleset = fake_open
    server_module.subprocess.Popen = fake_popen  # type: ignore[method-assign]
    server_module.start_command_watchdog = lambda _session: None
    try:
        yield captured
    finally:
        server_module.open_landlock_ruleset = original_open
        server_module.subprocess.Popen = original_popen  # type: ignore[method-assign]
        server_module.start_command_watchdog = original_watchdog
        os.close(write_fd)


class RuntimeHelperTests(unittest.TestCase):
    def test_windows_tty_request_reports_explicit_unsupported_error(self) -> None:
        with TemporaryDirectory() as tmp, patch.object(processes_module.os, "name", "nt"):
            with self.assertRaises(ToolFailure) as raised:
                processes_module.spawn_process(
                    "ignored",
                    cwd=tmp,
                    shell=True,
                    env={},
                    tty=True,
                    popen_kwargs={},
                )
        self.assertEqual(raised.exception.code, "TTY_UNSUPPORTED")
        self.assertEqual(raised.exception.details.get("platform"), "nt")

    def test_windows_process_termination_distinguishes_graceful_and_force(self) -> None:
        class FakeProcess:
            pid = 123

            def __init__(self) -> None:
                self.calls: list[object] = []

            def send_signal(self, value: object) -> None:
                self.calls.append(("send_signal", value))

            def wait(self, timeout: float) -> int:
                self.calls.append(("wait", timeout))
                return 0

            def terminate(self) -> None:
                self.calls.append("terminate")

            def kill(self) -> None:
                self.calls.append("kill")

        def fake_hasattr(value: object, name: str) -> bool:
            if value is processes_module.os and name == "killpg":
                return False
            return builtins.hasattr(value, name)

        with (
            patch.object(processes_module.os, "name", "nt"),
            patch.object(processes_module, "hasattr", side_effect=fake_hasattr, create=True),
            patch.object(processes_module.signal, "CTRL_BREAK_EVENT", 999, create=True),
        ):
            graceful = FakeProcess()
            processes_module.terminate_process_group(  # type: ignore[arg-type]
                graceful,
                signal.SIGTERM,
            )
            forced = FakeProcess()
            processes_module.terminate_process_group(  # type: ignore[arg-type]
                forced,
                processes_module.HARD_KILL_SIGNAL,
                force=True,
            )

        self.assertEqual(graceful.calls, [("send_signal", 999), ("wait", 1)])
        self.assertEqual(forced.calls, ["kill", ("wait", 1)])

    def test_atomic_patch_commit_rolls_back_all_files_after_mid_commit_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first-before\n", encoding="utf-8")
            second.write_text("second-before\n", encoding="utf-8")
            changes = [
                StagedFile("first.txt", first, "first-after\n", FileBaseline.capture(first), 0o644),
                StagedFile("second.txt", second, "second-after\n", FileBaseline.capture(second), 0o644),
            ]
            real_replace = os.replace

            def fail_second_install(source: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
                source_path = Path(source)
                if source_path.name.startswith(".coding-tools-patch-") and Path(destination) == second:
                    raise OSError("injected second-file install failure")
                real_replace(source, destination)

            with patch("coding_tools_mcp.patching.os.replace", side_effect=fail_second_install):
                with self.assertRaises(OSError):
                    AtomicPatchCommitter().commit(changes)

            self.assertEqual(first.read_text(encoding="utf-8"), "first-before\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "second-before\n")
            self.assertEqual(list(root.glob(".coding-tools-*-*")), [])

    def test_atomic_patch_commit_rejects_stale_baseline(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.txt"
            path.write_text("before\n", encoding="utf-8")
            baseline = FileBaseline.capture(path)
            path.write_text("external-change\n", encoding="utf-8")
            change = StagedFile("file.txt", path, "patch-change\n", baseline, 0o644)

            with self.assertRaises(ToolFailure) as raised:
                AtomicPatchCommitter().commit([change])

            self.assertEqual(raised.exception.code, "PATCH_CONFLICT")
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(path.read_text(encoding="utf-8"), "external-change\n")

    def test_atomic_patch_commit_preserves_backup_when_rollback_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "file.txt"
            target.write_text("before\n", encoding="utf-8")
            change = StagedFile(
                "file.txt",
                target,
                "after\n",
                FileBaseline.capture(target),
                0o644,
            )
            real_replace = os.replace

            def fail_install_and_restore(
                source: os.PathLike[str] | str,
                destination: os.PathLike[str] | str,
            ) -> None:
                source_path = Path(source)
                if source_path.name.startswith(".coding-tools-patch-"):
                    raise OSError("injected install failure")
                if source_path.name.startswith(".coding-tools-backup-"):
                    raise OSError("injected rollback failure")
                real_replace(source, destination)

            with patch("coding_tools_mcp.patching.os.replace", side_effect=fail_install_and_restore):
                with self.assertRaises(ToolFailure) as raised:
                    AtomicPatchCommitter().commit([change])

            self.assertEqual(raised.exception.code, "PATCH_ROLLBACK_FAILED")
            backups = raised.exception.details.get("recovery_backups", {})
            self.assertEqual(set(backups), {"file.txt"})
            recovery_path = Path(backups["file.txt"])
            self.assertTrue(recovery_path.exists())
            self.assertEqual(recovery_path.read_text(encoding="utf-8"), "before\n")

    def test_atomic_patch_backup_cleanup_failure_does_not_rollback_committed_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "file.txt"
            target.write_text("before\n", encoding="utf-8")
            change = StagedFile(
                "file.txt",
                target,
                "after\n",
                FileBaseline.capture(target),
                0o644,
            )
            real_unlink = Path.unlink
            backup_unlinks = 0

            def fail_backup_cleanup(path: Path, *args: object, **kwargs: object) -> None:
                nonlocal backup_unlinks
                if path.name.startswith(".coding-tools-backup-"):
                    backup_unlinks += 1
                    if backup_unlinks > 1:
                        raise OSError("injected backup cleanup failure")
                real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_backup_cleanup):
                AtomicPatchCommitter().commit([change])

            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            retained_backups = list(root.glob(".coding-tools-backup-*"))
            self.assertEqual(len(retained_backups), 1)
            self.assertEqual(retained_backups[0].read_text(encoding="utf-8"), "before\n")

    def test_atomic_patch_commit_does_not_overwrite_new_target_race(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "new.txt"
            baseline = FileBaseline.capture(path)
            path.write_text("external-create\n", encoding="utf-8")

            with self.assertRaises(ToolFailure) as raised:
                AtomicPatchCommitter().commit(
                    [StagedFile("new.txt", path, "patch-create\n", baseline, None)]
                )

            self.assertEqual(raised.exception.code, "PATCH_CONFLICT")
            self.assertEqual(path.read_text(encoding="utf-8"), "external-create\n")

    def test_image_identification_reads_jpeg_and_webp_dimensions(self) -> None:
        jpeg = (
            b"\xff\xd8"
            b"\xff\xe0\x00\x02"
            b"\xff\xc0\x00\x11\x08\x00\x10\x00\x20\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
            b"\xff\xd9"
        )
        self.assertEqual(identify_image(jpeg, path=file_path("sample.jpg")), ("image/jpeg", 32, 16))

        webp = b"RIFF" + (22).to_bytes(4, "little") + b"WEBPVP8X" + (10).to_bytes(4, "little")
        webp += b"\x00\x00\x00\x00" + (63).to_bytes(3, "little") + (31).to_bytes(3, "little")
        self.assertEqual(identify_image(webp, path=file_path("sample.webp")), ("image/webp", 64, 32))

    def test_tail_truncation_keeps_recent_complete_output(self) -> None:
        result = truncate_text_tail("\n".join(f"line-{index:03d}" for index in range(80)), max_bytes=128)
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "bytes")
        self.assertIn("line-079", result.content)
        self.assertNotIn("line-000", result.content)

    def test_head_truncation_keeps_overlong_first_line_prefix(self) -> None:
        result = truncate_text_head("a" * 200, max_bytes=20)
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "bytes")
        self.assertEqual(result.content, "a" * 20)
        self.assertEqual(result.output_bytes, 20)
        self.assertTrue(result.first_line_exceeds_limit)

    def test_head_truncation_keeps_utf8_boundary(self) -> None:
        result = truncate_text_head("é" * 100, max_bytes=21)
        self.assertTrue(result.truncated)
        self.assertTrue(result.content)
        self.assertLessEqual(len(result.content.encode("utf-8")), 21)
        self.assertNotIn("\ufffd", result.content)

    def test_tail_truncation_keeps_long_line_before_trailing_newline(self) -> None:
        result = truncate_text_tail(("a" * 200) + "\n", max_bytes=20)
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "bytes")
        self.assertEqual(result.content, "a" * 20)
        self.assertTrue(result.last_line_partial)

    def test_command_policy_allows_literal_patterns(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "index.html").write_text("</html>\n", encoding="utf-8")
            runtime = Runtime(workspace)
            runtime._check_command_policy("grep '</html>' index.html", {})
            runtime._check_command_policy('echo "https://example.com/a/b"', {})

    def test_package_module_entrypoint_exposes_help(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "coding_tools_mcp", "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--workspace", result.stdout)
        self.assertIn("--shell-env-inherit", result.stdout)
        self.assertIn("--permission-mode", result.stdout)
        self.assertIn("--allow-network", result.stdout)

    def test_workspace_init_tolerates_missing_home_lookup(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(server_module.Path, "home", side_effect=RuntimeError("home unavailable")):
                runtime = Runtime(Path(tmp))

        self.assertEqual(runtime.workspace.root, Path(tmp).resolve())

    def test_kill_command_keeps_unresponsive_command(self) -> None:
        class StillRunningProcess:
            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> None:
                raise subprocess.TimeoutExpired(cmd="still-running", timeout=timeout or 0)

        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            command = runtime._make_command(StillRunningProcess())  # type: ignore[arg-type]
            runtime.commands[command.command_id] = command
            with patch.object(server_module, "terminate_process_group", return_value=None):
                result = runtime.kill_command({"command_id": command.command_id, "wait_ms": 0, "kill_wait_ms": 0})

        self.assertFalse(result.get("killed"), result)
        self.assertEqual(result.get("status"), "terminating", result)
        self.assertFalse(result.get("evicted"), result)
        self.assertIn(command.command_id, runtime.commands)
        self.assertTrue(any("command retained" in warning for warning in result.get("warnings", [])), result)

    def test_command_policy_gates_inline_interpreter_code(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            for command in (
                "python3 -c \"print('</html>')\"",
                "bash -lc \"printf '</html>'\"",
                "node -e \"console.log('</div>')\"",
                "ruby -e \"puts '</html>'\"",
                "perl -e \"print '</html>'\"",
                "env FOO=bar python3 -c \"print('</html>')\"",
                "python3 -",
            ):
                with self.subTest(command=command):
                    with self.assertRaises(ToolFailure) as cm:
                        runtime._check_command_policy(command, {})
                    self.assertEqual(cm.exception.code, "PERMISSION_REQUIRED")
                    self.assertEqual(cm.exception.details.get("permission"), "inline_script")

    def test_command_policy_still_blocks_explicit_external_paths_and_network_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            for command in ("cat /etc/passwd", "echo hi > /tmp/out", "curl https://example.com"):
                with self.subTest(command=command):
                    with self.assertRaises(ToolFailure) as cm:
                        runtime._check_command_policy(command, {})
                    self.assertEqual(cm.exception.code, "PERMISSION_REQUIRED")

    def test_command_policy_allows_standard_special_devices_only(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            runtime._check_command_policy("echo hi >/dev/null", {})
            runtime._check_command_policy("dd if=/dev/null of=/dev/null bs=1 count=0", {})
            with self.assertRaises(ToolFailure):
                runtime._check_command_policy("echo hi >/dev/not-a-standard-device", {})

    def test_allow_network_only_opens_network_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), allow_network=True)
            runtime._check_command_policy("curl https://example.com", {})
            for command in ("git reset --hard", "python3 -c \"print(1)\""):
                with self.subTest(command=command):
                    with self.assertRaises(ToolFailure) as cm:
                        runtime._check_command_policy(command, {})
                    self.assertEqual(cm.exception.code, "PERMISSION_REQUIRED")

    def test_command_env_core_is_not_windows_toolchain_specific(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace)
            host_env = {
                "Path": r"C:\VS\VC\Tools\MSVC\bin;C:\Windows\System32",
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                "SystemRoot": r"C:\Windows",
                "ComSpec": r"C:\Windows\System32\cmd.exe",
                "INCLUDE": r"C:\VS\VC\Tools\MSVC\include;C:\SDK\Include",
                "LIB": r"C:\VS\VC\Tools\MSVC\lib;C:\SDK\Lib",
                "LIBPATH": r"C:\VS\VC\Tools\MSVC\libpath",
                "WindowsSdkDir": r"C:\Program Files (x86)\Windows Kits\10\\",
                "VCToolsInstallDir": r"C:\VS\VC\Tools\MSVC\14.99.99999\\",
                "VSCMD_ARG_TGT_ARCH": "x64",
                "UNRELATED": "drop-me",
                "VSCMD_SECRET": "drop-me-too",
            }
            with (
                patch.object(server_module.os, "name", "nt"),
                patch.dict(server_module.os.environ, host_env, clear=True),
            ):
                env = runtime._command_env({"CUSTOM": "ok", "OPENAI_API_KEY": "sk-test-secret-value"})

            self.assertEqual(env.get("Path"), host_env["Path"])
            self.assertEqual(env.get("PATHEXT"), host_env["PATHEXT"])
            self.assertEqual(env.get("SystemRoot"), host_env["SystemRoot"])
            self.assertEqual(env.get("ComSpec"), host_env["ComSpec"])
            self.assertEqual(env.get("CUSTOM"), "ok")
            self.assertEqual(env.get("HOME"), str(runtime.command_home_dir()))
            self.assertEqual(env.get("TEMP"), str(runtime.command_tmp_dir()))
            self.assertEqual(env.get("TMP"), str(runtime.command_tmp_dir()))
            self.assertNotIn("INCLUDE", env)
            self.assertNotIn("LIB", env)
            self.assertNotIn("LIBPATH", env)
            self.assertNotIn("WindowsSdkDir", env)
            self.assertNotIn("VCToolsInstallDir", env)
            self.assertNotIn("VSCMD_ARG_TGT_ARCH", env)
            self.assertNotIn("UNRELATED", env)
            self.assertNotIn("VSCMD_SECRET", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertTrue(runtime.command_home_dir().is_dir())
            self.assertTrue(runtime.command_tmp_dir().is_dir())
            self.assertTrue(runtime.cache_dir.is_dir())
            self.assertFalse((workspace / ".coding-tools").exists())

    def test_command_env_uses_external_home_tmp_and_cache_without_ecosystem_cache_vars(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace, shell_env_policy=ShellEnvPolicy(inherit="all"))
            host_env = {
                "PATH": "/usr/bin",
                "MAVEN_USER_HOME": "/host/m2",
                "GRADLE_USER_HOME": "/host/gradle",
                "npm_config_cache": "/host/npm",
                "PIP_CACHE_DIR": "/host/pip",
                "GOCACHE": "/host/go-build",
                "GOMODCACHE": "/host/go-mod",
                "CARGO_HOME": "/host/cargo",
                "RUSTUP_HOME": "/host/rustup",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({})

            self.assertEqual(env.get("HOME"), str(runtime.command_home_dir()))
            self.assertEqual(env.get("TMPDIR"), str(runtime.command_tmp_dir()))
            self.assertEqual(runtime.runtime_dir.parent.parent, runtime_parent_root())
            for key in (
                "MAVEN_USER_HOME",
                "GRADLE_USER_HOME",
                "npm_config_cache",
                "PIP_CACHE_DIR",
                "GOCACHE",
                "GOMODCACHE",
                "CARGO_HOME",
                "RUSTUP_HOME",
            ):
                self.assertNotIn(key, env)
            self.assertTrue(runtime.cache_dir.is_dir())
            self.assertFalse((workspace / ".coding-tools").exists())

    def test_runtime_and_server_info_do_not_create_exec_dirs(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace)
            self.assertFalse((workspace / ".coding-tools").exists())
            self.assertFalse(runtime.runtime_dir.exists())

            info = runtime.server_info_payload()
            self.assertEqual(info.get("runtime_dir"), str(runtime.runtime_dir))
            self.assertEqual(info.get("home"), str(runtime.command_home_dir()))
            self.assertFalse((workspace / ".coding-tools").exists())
            self.assertFalse(runtime.runtime_dir.exists())

            check = runtime.check_exec_environment({})
            self.assertTrue(check.get("ok"))
            self.assertEqual(check.get("runtime_dir"), str(runtime.runtime_dir))
            self.assertEqual(check.get("cache_dir"), str(runtime.cache_dir))
            self.assertFalse((workspace / ".coding-tools").exists())
            self.assertFalse(runtime.runtime_dir.exists())

    def test_server_info_and_check_exec_environment_expose_exec_state(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace)
            info = runtime.server_info_payload()
            self.assertEqual(info.get("permission_mode"), "safe")
            self.assertEqual(info.get("runtime_dir"), str(runtime.runtime_dir))
            self.assertEqual(info.get("home"), str(runtime.command_home_dir()))
            self.assertEqual(info.get("tmpdir"), str(runtime.command_tmp_dir()))
            self.assertEqual(info.get("cache_dir"), str(runtime.cache_dir))
            self.assertEqual(info.get("network_allowed"), False)
            self.assertIsInstance(info.get("landlock"), dict)
            self.assertEqual(info.get("exec_policy", {}).get("shell_expansion"), "blocked")
            self.assertEqual(info.get("exec_policy", {}).get("inline_script"), "blocked")
            self.assertEqual(info.get("exec_policy", {}).get("global_tmp_write"), "blocked")
            check = runtime.check_exec_environment({})
            self.assertTrue(check.get("ok"))
            self.assertEqual(check.get("permission_mode"), "safe")
            self.assertEqual(check.get("runtime_dir"), str(runtime.runtime_dir))
            self.assertEqual(check.get("home"), str(runtime.command_home_dir()))

    def test_permission_modes_apply_expected_gates(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            safe = Runtime(workspace)
            with self.assertRaises(ToolFailure):
                safe._check_command_policy("python3 -c \"print(1)\"", {})
            with self.assertRaises(ToolFailure):
                safe._check_command_policy("echo $(pwd)", {})
            with self.assertRaises(ToolFailure):
                safe._check_command_policy("curl https://example.com", {})

            trusted = Runtime(workspace, permission_mode="trusted")
            trusted._check_command_policy("python3 -c \"print(1)\"", {})
            trusted._check_command_policy("echo $(pwd)", {})
            trusted._check_command_policy("curl https://example.com", {})
            self.assertEqual(trusted.global_tmp_write_policy(), "tmp-prefix")
            self.assertEqual(trusted.command_tmp_dir().parent, trusted.runtime_dir)
            self.assertEqual(trusted.runtime_dir.parent.parent, runtime_parent_root())
            with self.assertRaises(ToolFailure):
                trusted._check_command_policy("git reset --hard", {})

            dangerous = Runtime(workspace, permission_mode="dangerous")
            dangerous._check_command_policy("cat /etc/passwd", {})
            dangerous._check_command_policy("git reset --hard", {})
            self.assertFalse(dangerous.landlock_enabled())
            self.assertEqual(dangerous.global_tmp_write_policy(), "allowed")

    def test_command_env_all_preserves_toolchain_environment_but_filters_sensitive_values(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace, shell_env_policy=ShellEnvPolicy(inherit="all"))
            host_env = {
                "PATH": "/toolchain/bin:/usr/bin",
                "INCLUDE": r"C:\VS\VC\Tools\MSVC\include",
                "LIB": r"C:\VS\VC\Tools\MSVC\lib",
                "LIBPATH": r"C:\VS\VC\Tools\MSVC\libpath",
                "CUDA_PATH": "/opt/cuda",
                "ONEAPI_ROOT": "/opt/intel/oneapi",
                "OPENAI_API_KEY": "sk-test-secret-value",
                "PYTHONPATH": "/tmp/injected",
                "DYLD_LIBRARY_PATH": "/tmp/injected",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({})

            self.assertEqual(env.get("INCLUDE"), host_env["INCLUDE"])
            self.assertEqual(env.get("LIB"), host_env["LIB"])
            self.assertEqual(env.get("LIBPATH"), host_env["LIBPATH"])
            self.assertEqual(env.get("CUDA_PATH"), host_env["CUDA_PATH"])
            self.assertEqual(env.get("ONEAPI_ROOT"), host_env["ONEAPI_ROOT"])
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("DYLD_LIBRARY_PATH", env)

    def test_command_env_dangerous_all_preserves_sensitive_inherited_environment(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                permission_mode="dangerous",
                shell_env_policy=ShellEnvPolicy(inherit="all"),
            )
            host_env = {
                "OPENAI_API_KEY": "sk-test-secret-value",
                "LD_PRELOAD": "/tmp/injected.so",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({})

            self.assertEqual(env.get("OPENAI_API_KEY"), "sk-test-secret-value")
            self.assertEqual(env.get("LD_PRELOAD"), "/tmp/injected.so")

    def test_runtime_root_stays_posix_tmp_when_process_tmpdir_is_workspace_local(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX /tmp semantics do not apply on Windows")
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            drifted_tmp = workspace / ".coding-tools" / "tmp"
            drifted_tmp.mkdir(parents=True)
            with patch.dict(server_module.os.environ, {"TMPDIR": str(drifted_tmp)}, clear=True):
                safe = Runtime(workspace)
                trusted = Runtime(workspace, permission_mode="trusted")
            self.assertEqual(safe.runtime_dir.parent.parent, runtime_parent_root())
            self.assertEqual(trusted.runtime_dir.parent.parent, runtime_parent_root())
            self.assertEqual(safe.command_tmp_dir().parent, safe.runtime_dir)
            self.assertEqual(trusted.command_tmp_dir().parent, trusted.runtime_dir)

    def test_command_env_include_exclude_and_set_are_applied_in_order(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                shell_env_policy=ShellEnvPolicy(
                    inherit="all",
                    include_only=("PATH", "KEEP_*", "SET_BY_POLICY"),
                    exclude=("KEEP_DROP",),
                    set={"SET_BY_POLICY": "configured"},
                ),
            )
            host_env = {
                "PATH": "/usr/bin",
                "KEEP_THIS": "yes",
                "KEEP_DROP": "no",
                "OTHER": "drop",
            }
            with patch.dict(server_module.os.environ, host_env, clear=True):
                env = runtime._command_env({})

            self.assertEqual(env.get("PATH"), "/usr/bin")
            self.assertEqual(env.get("KEEP_THIS"), "yes")
            self.assertEqual(env.get("SET_BY_POLICY"), "configured")
            self.assertNotIn("KEEP_DROP", env)
            self.assertNotIn("OTHER", env)

    def test_command_policy_unwraps_env_before_path_checks(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            for command in (
                "env cat /tmp/secret",
                "env FOO=bar cat ../outside-secret.txt",
                "env -i --unset FOO cat /tmp/secret",
                "env --chdir /tmp cat secret",
                "env --ignore-signal cat /tmp/secret",
                'env -S "cat /tmp/secret"',
            ):
                with self.subTest(command=command):
                    with self.assertRaises(ToolFailure) as cm:
                        runtime._check_command_policy(command, {})
                    self.assertEqual(cm.exception.code, "PERMISSION_REQUIRED")

    def test_exec_command_warns_and_runs_when_landlock_is_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            original = server_module.open_landlock_ruleset

            def unavailable(_workspace: Path, _read_roots: list[str], **_kwargs: object) -> int:
                raise ToolFailure("SANDBOX_UNAVAILABLE", "test landlock unavailable", category="security")

            server_module.open_landlock_ruleset = unavailable
            try:
                result = runtime.exec_command({"cmd": "printf ok", "timeout_ms": 5000, "yield_time_ms": 1000})
            finally:
                server_module.open_landlock_ruleset = original

            self.assertTrue(result["ok"])
            self.assertEqual(result["stdout"], "ok")
            self.assertTrue(any("Landlock" in warning for warning in result.get("warnings", [])))

    def test_exec_command_uses_landlock_wrapper_without_preexec_fn(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            with fake_landlock_exec() as captured:
                runtime.exec_command({"cmd": "printf ok", "timeout_ms": 5000, "yield_time_ms": 0})

            kwargs = captured["kwargs"]
            self.assertIsInstance(kwargs, dict)
            self.assertFalse(kwargs.get("shell"))
            self.assertNotIn("preexec_fn", kwargs)
            if os.name == "nt":
                self.assertIn("creationflags", kwargs)
            else:
                self.assertIn("start_new_session", kwargs)
            self.assertEqual(kwargs.get("pass_fds"), (captured["read_fd"],))
            self.assertEqual(captured.get("write_roots"), [runtime.runtime_dir])
            popen_args = captured["args"]
            self.assertIsInstance(popen_args, tuple)
            argv = popen_args[0]
            self.assertIsInstance(argv, list)
            self.assertTrue(str(argv[1]).endswith("landlock_exec.py"))

    def test_exec_command_passes_runtime_write_root_to_landlock(self) -> None:
        for permission_mode in ("safe", "trusted"):
            with self.subTest(permission_mode=permission_mode), TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp), permission_mode=permission_mode)
                with fake_landlock_exec() as captured:
                    runtime.exec_command({"cmd": "printf ok", "timeout_ms": 5000, "yield_time_ms": 0})

                self.assertEqual(captured.get("write_roots"), [runtime.runtime_dir])

    def test_dangerously_skip_all_permissions_auto_grants_permission_gates(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            default_runtime = Runtime(workspace)
            with self.assertRaises(ToolFailure) as cm:
                default_runtime._check_command_policy("curl https://example.com", {})
            self.assertEqual(cm.exception.code, "PERMISSION_REQUIRED")

            dangerous_runtime = Runtime(workspace, permission_mode="dangerous")
            dangerous_runtime._check_command_policy("curl https://example.com", {})
            grant = dangerous_runtime.request_permissions(
                {
                    "tool_name": "exec_command",
                    "permission": "network",
                    "reason": "test dangerous mode",
                    "arguments": {"cmd": "curl https://example.com"},
                }
            )
            self.assertTrue(grant.get("ok"))
            self.assertEqual(grant.get("status"), "granted")

            filtered_env = default_runtime._command_env({"OPENAI_API_KEY": "sk-test-secret-value"})
            dangerous_env = dangerous_runtime._command_env({"OPENAI_API_KEY": "sk-test-secret-value"})
            self.assertNotIn("OPENAI_API_KEY", filtered_env)
            self.assertEqual(dangerous_env.get("OPENAI_API_KEY"), "sk-test-secret-value")

    def test_landlock_device_access_includes_truncate_and_ioctl_bits(self) -> None:
        handled = server_module.landlock_handled_access(5)
        device_access = server_module.landlock_device_access(handled)
        self.assertTrue(device_access & LANDLOCK_ACCESS_FS_WRITE_FILE)
        self.assertTrue(device_access & LANDLOCK_ACCESS_FS_TRUNCATE)
        self.assertTrue(device_access & LANDLOCK_ACCESS_FS_IOCTL_DEV)

    def test_guard_allow_roots_include_dns_toolchain_path_and_java_home(self) -> None:
        with TemporaryDirectory() as tmp:
            java_home = Path(tmp) / "jdk"
            explicit_root = Path(tmp) / "explicit-root"
            private_path_dir = Path(tmp) / "bin"
            java_home.mkdir()
            explicit_root.mkdir()
            private_path_dir.mkdir()
            with patch.dict(
                server_module.os.environ,
                {
                    "PATH": str(private_path_dir),
                    "JAVA_HOME": str(java_home),
                    "CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS": str(explicit_root),
                },
                clear=True,
            ):
                roots = set(guard_allow_roots())
        self.assertIn("/etc/resolv.conf", roots)
        self.assertIn("/etc/hosts", roots)
        self.assertIn("/usr", roots)
        self.assertIn("/usr/local/sdkman/candidates", roots)
        self.assertIn("/etc/gitconfig", roots)
        self.assertIn("/etc/gitconfig.d", roots)
        self.assertIn(str(java_home.resolve()), roots)
        self.assertIn(str(explicit_root.resolve()), roots)
        self.assertNotIn(str(private_path_dir.resolve()), roots)

    def test_safe_exec_git_init_and_local_config_reads_system_git_config_roots(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace)
            with patch.dict(server_module.os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                self.assertNotIn("GIT_CONFIG_NOSYSTEM", runtime._command_env({}))
                result = runtime.exec_command(
                    {
                        "cmd": (
                            "git init -q tmp-git-repo && "
                            "git -C tmp-git-repo config user.email test@example.invalid && "
                            "git -C tmp-git-repo config user.name Test"
                        ),
                        "timeout_ms": 10000,
                        "yield_time_ms": 30000,
                        "max_output_bytes": 20000,
                    }
                )
        self.assertEqual(result.get("status"), "exited", result)
        self.assertEqual(result.get("exit_code"), 0, result)
        self.assertNotIn("unable to access '/etc/gitconfig'", str(result.get("stderr", "")))

    def test_exec_diagnostics_classify_common_failures(self) -> None:
        self.assertEqual(
            exec_output_diagnostics({"stderr": "mvn: cannot create /dev/null: Permission denied"})[0]["code"],
            "DEV_NULL_DENIED",
        )
        self.assertEqual(
            exec_output_diagnostics({"stderr": "curl: (6) Could not resolve host: example.com"})[0]["code"],
            "DNS_RESOLUTION_FAILED",
        )
        self.assertEqual(
            exec_output_diagnostics({"status": "timeout", "timed_out": True})[0]["code"],
            "COMMAND_TIMED_OUT",
        )
        self.assertEqual(
            exec_output_diagnostics({"truncated": True})[0]["code"],
            "OUTPUT_TRUNCATED",
        )

    def test_exec_diagnostics_do_not_treat_maven_home_as_unwritable_home(self) -> None:
        output = """warning: unable to access '/etc/gitconfig': Permission denied
fatal: unknown error occurred while reading the configuration files
Maven home: /usr/share/maven
"""
        codes = [item["code"] for item in exec_output_diagnostics({"stderr": output})]
        self.assertIn("LANDLOCK_READ_ROOT_BLOCKED", codes)
        self.assertNotIn("HOME_NOT_WRITABLE", codes)

    def test_exec_diagnostics_treat_eacces_home_path_as_unwritable_home(self) -> None:
        output = "Error: EACCES: permission denied, mkdir '/work/.coding-tools/home/.cache'"
        codes = [item["code"] for item in exec_output_diagnostics({"stderr": output})]
        self.assertIn("HOME_NOT_WRITABLE", codes)

    def test_permission_failure_diagnostics_classify_policy_gates(self) -> None:
        cases = [
            ("network", "NETWORK_PERMISSION_REQUIRED"),
            ("shell_expansion", "SHELL_EXPANSION_PERMISSION_REQUIRED"),
            ("inline_script", "INLINE_SCRIPT_PERMISSION_REQUIRED"),
            ("sensitive_env", "SECRET_ENV_REJECTED"),
        ]
        for permission, expected in cases:
            with self.subTest(permission=permission):
                exc = ToolFailure(
                    "PERMISSION_REQUIRED",
                    "test",
                    category="permission",
                    details={"permission": permission},
                )
                self.assertEqual(permission_failure_diagnostics(exc)[0]["code"], expected)

    def test_runtime_exposes_one_stable_truthfully_annotated_tool_catalog(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = Runtime(workspace).list_tools()["tools"]
            second = Runtime(workspace).list_tools()["tools"]
            self.assertEqual(first, second)
            names = {tool["name"] for tool in first}
            self.assertIn("apply_patch", names)
            self.assertIn("exec_command", names)
            self.assertIn("read_file", names)
            self.assertNotIn("edit_file", names)
            apply_patch_tool = next(tool for tool in first if tool["name"] == "apply_patch")
            self.assertIs(apply_patch_tool["annotations"].get("destructiveHint"), True)
            self.assertIs(apply_patch_tool["annotations"].get("readOnlyHint"), False)

    def test_agent_text_matches_per_tool_limits_without_renderer_truncation(self) -> None:
        # Per-call tool limits (here read_file max_bytes) are the only budget:
        # the renderer must not apply a second, hidden truncation layer.
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            content = ("x" * 49_999) + "!"
            (workspace / "large.txt").write_text(content, encoding="utf-8")
            result = Runtime(workspace).call_tool(
                "read_file",
                {"path": "large.txt", "max_bytes": 60_000},
            )

            payload = result["structuredContent"]
            model_text = "\n".join(
                item["text"]
                for item in result["content"]
                if item.get("type") == "text"
            )
            self.assertEqual(payload["content"], content)
            self.assertEqual(model_text, content)
            self.assertNotIn("preview truncated", model_text)

    def agent_text(self, result: dict[str, object]) -> str:
        return "\n".join(
            str(item.get("text"))
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        )

    def test_agent_text_has_an_emergency_safety_ceiling(self) -> None:
        oversized_context = "x" * (MODEL_TEXT_SAFETY_LIMIT_BYTES + 1024)
        result = make_tool_result(
            "search_text",
            {
                "matches": [
                    {
                        "path": "large.txt",
                        "line": 2,
                        "column": 1,
                        "preview": "needle",
                        "before": [oversized_context],
                        "after": [],
                    }
                ],
                "truncated": False,
            },
            is_error=False,
        )
        model_text = self.agent_text(result)
        self.assertLessEqual(
            len(model_text.encode("utf-8")),
            MODEL_TEXT_SAFETY_LIMIT_BYTES,
        )
        self.assertIn("model text reached", model_text)
        self.assertIn("safety ceiling", model_text)

    @unittest.skipIf(os.name == "nt", "POSIX shell command syntax")
    def test_exec_model_text_always_carries_exit_status(self) -> None:
        # A failing command whose stdout looks like success must still be
        # legible as a failure from the model text alone.
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {"cmd": "echo All checks completed.; exit 7", "timeout_ms": 10000},
            )
            model_text = self.agent_text(result)
            self.assertIn("Status: exited", model_text)
            self.assertIn("exit code 7", model_text)
            self.assertIn("All checks completed.", model_text)

    def test_exec_truncated_model_text_names_a_real_output_ref(self) -> None:
        with TemporaryDirectory() as tmp:
            command = f"{sys.executable} -c \"print('x' * 5000)\""
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {"cmd": command, "timeout_ms": 10000, "max_output_bytes": 512},
            )
            model_text = self.agent_text(result)
            self.assertIn('read_output(output_ref="command:', model_text)
            self.assertIn(":stdout", model_text)

    @unittest.skipIf(os.name == "nt", "POSIX shell redirection syntax")
    def test_exec_truncation_continues_the_stream_that_was_truncated(self) -> None:
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {
                    "cmd": "printf ok; printf 12345678901234567890 >&2",
                    "timeout_ms": 10000,
                    "max_output_bytes": 5,
                },
            )
            payload = result["structuredContent"]
            self.assertIs(payload.get("stdout_truncated"), False)
            self.assertIs(payload.get("stderr_truncated"), True)
            self.assertEqual(payload.get("output_stream"), "stderr")
            self.assertEqual(payload.get("truncated_output_streams"), ["stderr"])
            self.assertTrue(str(payload.get("output_ref", "")).endswith(":stderr"))
            next_ref = payload.get("next_action", {}).get("arguments", {}).get("output_ref")
            self.assertTrue(str(next_ref).endswith(":stderr"))
            model_text = self.agent_text(result)
            self.assertIn("stderr output truncated", model_text)
            self.assertIn(":stderr", model_text)
            self.assertNotIn('output_ref="command:' + str(payload["command_id"]) + ':stdout"', model_text)

    @unittest.skipIf(os.name == "nt", "POSIX shell redirection syntax")
    def test_exec_truncation_names_both_stream_continuations(self) -> None:
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {
                    "cmd": "printf 1234567890; printf abcdefghij >&2",
                    "timeout_ms": 10000,
                    "max_output_bytes": 5,
                },
            )
            payload = result["structuredContent"]
            self.assertEqual(
                payload.get("truncated_output_streams"),
                ["stdout", "stderr"],
            )
            next_actions = payload.get("next_actions")
            self.assertIsInstance(next_actions, list)
            self.assertEqual(len(next_actions), 2)
            action_refs = [
                action.get("arguments", {}).get("output_ref")
                for action in next_actions
                if isinstance(action, dict)
            ]
            self.assertTrue(str(action_refs[0]).endswith(":stdout"))
            self.assertTrue(str(action_refs[1]).endswith(":stderr"))
            model_text = self.agent_text(result)
            self.assertIn("stdout output truncated", model_text)
            self.assertIn("stderr output truncated", model_text)

    def test_exec_running_model_text_names_the_poll_call(self) -> None:
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {"cmd": "sleep 1", "timeout_ms": 10000, "yield_time_ms": 0},
            )
            model_text = self.agent_text(result)
            self.assertIn("Status: running", model_text)
            self.assertIn('write_stdin(command_id="', model_text)

    def test_read_file_truncation_is_visible_with_continuation(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            content = "\n".join(f"line-{index}" for index in range(1, 2501)) + "\n"
            (workspace / "long.txt").write_text(content, encoding="utf-8")
            result = Runtime(workspace).call_tool("read_file", {"path": "long.txt"})
            model_text = self.agent_text(result)
            self.assertIn("Showing lines 1-2000 of 2500", model_text)
            self.assertIn(
                'continue with read_file(path="long.txt", start_line=2001',
                model_text,
            )
            self.assertIn("line-2000", model_text)
            self.assertNotIn("line-2001\n", model_text)

    def test_read_file_continuation_preserves_default_cwd_relative_path(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            nested = workspace / "nested"
            nested.mkdir()
            (nested / "long.txt").write_text(
                "".join(f"line-{index}\n" for index in range(1, 20)),
                encoding="utf-8",
            )
            runtime = Runtime(workspace)
            runtime.set_default_cwd({"path": "nested"})
            first = runtime.call_tool(
                "read_file",
                {"path": "long.txt", "max_bytes": 16},
            )
            first_payload = first["structuredContent"]
            action = first_payload.get("next_action")
            self.assertIsInstance(action, dict)
            self.assertEqual(action.get("tool"), "read_file")
            self.assertEqual(action.get("arguments", {}).get("path"), "long.txt")
            second = runtime.call_tool(action["tool"], action["arguments"])
            self.assertIs(second.get("isError"), False)
            self.assertEqual(
                second["structuredContent"].get("start_line"),
                first_payload.get("next_start_line"),
            )

    def test_read_file_partial_single_line_is_not_rendered_as_complete(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "single-line.txt").write_text("x" * 100, encoding="utf-8")
            result = Runtime(workspace).call_tool(
                "read_file",
                {"path": "single-line.txt", "max_bytes": 16},
            )
            payload = result["structuredContent"]
            self.assertIs(payload.get("truncated"), True)
            self.assertIs(payload.get("first_line_exceeds_limit"), True)
            self.assertIsNone(payload.get("next_start_line"))
            model_text = self.agent_text(result)
            self.assertIn("content truncated", model_text)
            self.assertIn("raise max_bytes", model_text)

    def test_git_pagination_actions_are_rendered(self) -> None:
        error = git_fixture_preflight_error()
        if error is not None:
            self.skipTest(error)
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tracked = workspace / "tracked.txt"
            tracked.write_text("one\ntwo\nthree\n", encoding="utf-8")
            init_git(workspace)
            tracked.write_text("one changed\ntwo\nthree\n", encoding="utf-8")
            committed = subprocess.run(
                ["git", "commit", "-q", "-am", "second commit"],
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            runtime = Runtime(workspace)

            log_result = runtime.call_tool("git_log", {"max_count": 1})
            log_payload = log_result["structuredContent"]
            self.assertIs(log_payload.get("truncated"), True)
            self.assertEqual(
                log_payload.get("next_action", {}).get("arguments", {}).get("skip"),
                1,
            )
            log_text = self.agent_text(log_result)
            self.assertIn("more commits available", log_text)
            self.assertIn("git_log(", log_text)
            self.assertIn("skip=1", log_text)

            blame_result = runtime.call_tool(
                "git_blame",
                {
                    "path": "tracked.txt",
                    "start_line": 1,
                    "end_line": 3,
                    "max_lines": 1,
                },
            )
            blame_payload = blame_result["structuredContent"]
            self.assertIs(blame_payload.get("truncated"), True)
            self.assertEqual(
                blame_payload.get("next_action", {}).get("arguments", {}).get("start_line"),
                2,
            )
            blame_text = self.agent_text(blame_result)
            self.assertIn("blame lines truncated", blame_text)
            self.assertIn("git_blame(", blame_text)
            self.assertIn("start_line=2", blame_text)

    def test_search_truncation_reports_match_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "data.txt").write_text("needle\n" * 5, encoding="utf-8")
            result = Runtime(workspace).call_tool(
                "search_text",
                {"query": "needle", "max_results": 2},
            )
            model_text = self.agent_text(result)
            self.assertIn("showing 2 of", model_text)
            self.assertIn("max_results", model_text)

    def test_exec_command_tool_errors_use_failed_status(self) -> None:
        with TemporaryDirectory() as tmp:
            result = Runtime(Path(tmp), permission_mode="trusted").call_tool(
                "exec_command",
                {"cmd": "pwd", "workdir": "missing"},
            )
            self.assertIs(result.get("isError"), True)
            self.assertEqual(result.get("structuredContent", {}).get("status"), "failed")

    @unittest.skipIf(os.name == "nt", "POSIX signal status test")
    def test_exec_command_reports_signal_exit_as_terminated(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            result = runtime.exec_command(
                {"cmd": "kill -TERM $$", "timeout_ms": 5_000, "yield_time_ms": 5_000}
            )
            self.assertEqual(result.get("status"), "terminated", result)
            self.assertEqual(result.get("signal"), "SIGTERM", result)

    def test_active_process_limit_counts_running_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            command_ids: list[str] = []
            try:
                for _ in range(MAX_ACTIVE_COMMANDS):
                    result = runtime.exec_command(
                        {"cmd": "sleep 5", "timeout_ms": 10_000, "yield_time_ms": 0}
                    )
                    command_ids.append(str(result["command_id"]))
                with self.assertRaises(ToolFailure) as raised:
                    runtime.exec_command(
                        {"cmd": "sleep 5", "timeout_ms": 10_000, "yield_time_ms": 0}
                    )
                self.assertEqual(raised.exception.code, "COMMAND_LIMIT_REACHED")
            finally:
                for command_id in command_ids:
                    try:
                        runtime.kill_command(
                            {"command_id": command_id, "signal": "KILL", "wait_ms": 1000}
                        )
                    except ToolFailure:
                        pass
                runtime.close()

    def test_initialize_injects_root_instructions_and_indexes_nested_instructions(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("Run the focused test suite.\n", encoding="utf-8")
            nested = workspace / "packages" / "api" / "AGENTS.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("API-only nested rule.\n", encoding="utf-8")

            initialized = Runtime(workspace).initialize()
            instructions = initialized.get("instructions", "")
            self.assertIn("Run the focused test suite.", instructions)
            self.assertIn("packages/api/AGENTS.md", instructions)
            self.assertNotIn("API-only nested rule.", instructions)
            self.assertIn("apply_patch", instructions)

    def test_exec_command_compact_preview_and_read_output(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            result = runtime.exec_command(
                {
                    "cmd": "printf 'alpha\nbeta\n'",
                    "timeout_ms": 5000,
                    "yield_time_ms": 30000,
                    "verbosity": "preview",
                    "preview_bytes": 64,
                }
            )
            self.assertEqual(result.get("status"), "exited", result)
            self.assertEqual(result.get("exit_code"), 0, result)
            self.assertIn("summary", result)
            self.assertIn("preview", result)
            self.assertIn("output_ref", result)
            self.assertIn("output_refs", result)
            self.assertEqual(result.get("output_stream"), "stdout")
            self.assertNotIn("stdout", result)
            page = runtime.read_output({"output_ref": result["output_ref"], "offset": 0, "limit": 128})
            self.assertIn("alpha", page.get("content", ""))
            self.assertIn("beta", page.get("content", ""))
            self.assertEqual(page.get("stream"), "stdout")
            self.assertIsNone(page.get("next_offset"))

    @unittest.skipIf(os.name == "nt", "this build explicitly reports ConPTY as unsupported")
    def test_exec_command_tty_uses_a_real_pseudo_terminal(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            script = "import os; print(os.isatty(0), os.isatty(1), os.isatty(2), flush=True)"
            result = runtime.exec_command(
                {
                    "cmd": f"{sys.executable} -c {script!r}",
                    "tty": True,
                    "timeout_ms": 5000,
                    "yield_time_ms": 5000,
                }
            )
            # The tty fast-path intentionally returns as soon as the first
            # output arrives, which can race the exit becoming observable.
            # Follow the documented next_action contract and poll to completion.
            stdout = str(result.get("stdout", ""))
            deadline = time.time() + 5
            while result.get("status") == "running" and time.time() < deadline:
                result = runtime.write_stdin(
                    {"command_id": result["command_id"], "chars": "", "yield_time_ms": 500}
                )
                stdout += str(result.get("stdout", ""))
            self.assertEqual(result.get("status"), "exited", result)
            self.assertIn("True True True", stdout)

    def test_completed_commands_are_evicted_from_active_storage(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            command_ids: list[str] = []
            for _ in range(20):
                result = runtime.exec_command(
                    {"cmd": "sleep 0.02", "timeout_ms": 2000, "yield_time_ms": 0, "max_output_bytes": 64}
                )
                command_ids.append(str(result["command_id"]))
            # Poll instead of a fixed sleep: the short-lived processes finish
            # on their own schedule, and eviction only requires that a prune
            # after exit moves them out of active storage.
            eviction_deadline = time.time() + 5
            while time.time() < eviction_deadline:
                runtime._prune_commands()
                if not runtime.commands:
                    break
                time.sleep(0.05)
            self.assertEqual(runtime.commands, {})
            self.assertLessEqual(len(runtime.output_commands), 20)
            self.assertTrue(set(runtime.output_commands).issubset(set(command_ids)))
            deadline = time.time() + 1
            while time.time() < deadline and any(
                thread.name.startswith("coding-tools-watchdog-")
                for thread in threading.enumerate()
            ):
                time.sleep(0.01)
            self.assertFalse(
                any(
                    thread.name.startswith("coding-tools-watchdog-")
                    for thread in threading.enumerate()
                )
            )

    def test_running_and_truncated_commands_return_explicit_next_actions(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            running = runtime.exec_command(
                {"cmd": "sleep 1", "timeout_ms": 5000, "yield_time_ms": 0, "max_output_bytes": 64}
            )
            self.assertEqual(running.get("status"), "running")
            self.assertEqual(running.get("next_action", {}).get("tool"), "write_stdin")
            runtime.kill_command({"command_id": running["command_id"], "signal": "KILL"})

            truncated = runtime.exec_command(
                {
                    "cmd": "printf 'abcdefghijklmnopqrstuvwxyz'",
                    "timeout_ms": 5000,
                    "yield_time_ms": 5000,
                    "max_output_bytes": 8,
                }
            )
            self.assertTrue(truncated.get("output_truncated"), truncated)
            self.assertEqual(truncated.get("next_action", {}).get("tool"), "read_output")
            self.assertIn("output_ref", truncated)

    def test_read_output_pages_streams_independently(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            script = (
                "import sys,time;"
                "sys.stderr.write('err1\\nerr2\\n'); sys.stderr.flush();"
                "sys.stdout.write('out1\\n'); sys.stdout.flush();"
                "time.sleep(0.4);"
                "sys.stdout.write('out2\\n'); sys.stdout.flush();"
                "time.sleep(1)"
            )
            result = runtime.exec_command(
                {
                    "cmd": f"{sys.executable} -c {script!r}",
                    "timeout_ms": 5000,
                    "yield_time_ms": 100,
                    "verbosity": "preview",
                    "preview_bytes": 64,
                }
            )
            self.assertEqual(result.get("status"), "running", result)
            output_refs = result.get("output_refs")
            self.assertIsInstance(output_refs, dict)
            stderr_ref = output_refs["stderr"]

            first: dict[str, object] = {}
            for _ in range(10):
                first = runtime.read_output({"output_ref": stderr_ref, "offset": 0, "limit": 5})
                if first.get("content"):
                    break
                time.sleep(0.05)
            self.assertEqual(first.get("content"), "err1\n")
            self.assertEqual(first.get("next_offset"), 5)
            time.sleep(0.6)
            second = runtime.read_output({"output_ref": stderr_ref, "offset": first["next_offset"], "limit": 64})
            self.assertEqual(second.get("offset"), first.get("next_offset"))
            self.assertEqual(second.get("content"), "err2\n")
            self.assertNotIn("out2", second.get("content", ""))
            runtime.kill_command({"command_id": result["command_id"], "wait_ms": 1000})

    def test_read_output_uses_absolute_stream_offsets_after_buffer_drop(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            # The context manager closes the stdout/stderr pipes and waits, so
            # the test does not leak pipe file objects (ResourceWarning).
            with subprocess.Popen([sys.executable, "-c", ""], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                command = server_module.CommandRun(command_id="manual-output", process=process, buffer_limit=4)
                command.append_stdout(b"abcdef")
                runtime._remember_output_command(command)

                page = runtime.read_output({"output_ref": "command:manual-output:stdout", "offset": 0, "limit": 10})
                self.assertEqual(page.get("offset"), 2)
                self.assertEqual(page.get("requested_offset"), 0)
                self.assertEqual(page.get("content"), "cdef")
                self.assertEqual(page.get("omitted_bytes"), 2)
                self.assertEqual(page.get("retained_start_offset"), 2)

                command.stdout_cursor = 0
                snapshot = command.snapshot_since_cursor(10)
                self.assertEqual(snapshot.get("stdout"), "cdef")
                self.assertEqual(snapshot.get("stdout_omitted_bytes"), 2)
                self.assertIs(snapshot.get("truncated"), True)

    def test_default_cwd_and_git_convenience_tools(self) -> None:
        if server_module.shutil.which("git") is None:
            self.skipTest("git is not available")
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            (workspace / "src" / "hello.txt").write_text("hello\n", encoding="utf-8")
            for cmd in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "Runtime Test"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "initial commit"],
            ):
                completed = subprocess.run(cmd, cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if completed.returncode != 0:
                    self.skipTest(f"git fixture setup failed: {completed.stderr.strip()}")

            runtime = Runtime(workspace)
            cwd = runtime.set_default_cwd({"path": "src"})
            self.assertEqual(cwd.get("default_cwd"), "src")
            read = runtime.read_file({"path": "hello.txt"})
            self.assertEqual(read.get("content"), "hello\n")

            log = runtime.git_log({"max_count": 5})
            self.assertTrue(log.get("is_repo"))
            self.assertEqual(log.get("commits", [])[0].get("subject"), "initial commit")

            show = runtime.git_show({"include_diff": False, "max_bytes": 4096})
            self.assertTrue(show.get("is_repo"))
            self.assertIn("initial commit", show.get("content", ""))

            blame = runtime.git_blame({"path": "hello.txt", "max_lines": 5})
            self.assertTrue(blame.get("is_repo"))
            self.assertEqual(blame.get("lines", [])[0].get("content"), "hello")

            with self.assertRaises(ToolFailure):
                runtime.set_default_cwd({"path": "../outside"})

    def test_boundary_regressions_for_aliases_and_command_scanning(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "nested").mkdir()
            (workspace / "sample.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
            runtime = Runtime(workspace, permission_mode="trusted")

            cwd_result = runtime.exec_command(
                {"cmd": "pwd", "cwd": "nested", "timeout_ms": 5000, "max_output_bytes": 4096}
            )
            self.assertEqual(cwd_result.get("exit_code"), 0)
            self.assertEqual(Path(str(cwd_result.get("stdout", "")).strip()).name, "nested")

            with self.assertRaises(ToolFailure):
                runtime.exec_command({"cmd": "pwd", "workdir": ".", "cwd": "nested"})

            read = runtime.read_file({"path": "sample.txt", "start_line": 2, "max_lines": 1})
            self.assertEqual(read.get("content"), "two\n")
            self.assertEqual(read.get("end_line"), 2)

            tag = "model" + "Version"
            xml_heredoc = (
                "cat > pom.xml <<'EOF'\n"
                "<project>\n"
                f"  <{tag}>4.0.0</{tag}>\n"
                "</project>\n"
                "EOF"
            )
            runtime.exec_command({"cmd": xml_heredoc, "timeout_ms": 5000, "max_output_bytes": 4096})
            self.assertIn(tag, (workspace / "pom.xml").read_text(encoding="utf-8"))

    def test_heredoc_payload_stripping_keeps_live_shell_code_scanned(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")

            # Redirection target on the heredoc operator's own line is live code.
            with self.assertRaises(ToolFailure) as ctx:
                runtime.exec_command({"cmd": "cat <<EOF > /etc/cron.d/evil\nbody\nEOF"})
            self.assertEqual(ctx.exception.details.get("path"), "/etc/cron.d/evil")

            # Commands after the closing delimiter are live code.
            with self.assertRaises(ToolFailure) as ctx:
                runtime.exec_command(
                    {"cmd": "cat <<'EOF'\nbody\nEOF\ncp /etc/shadow stolen.txt"}
                )
            self.assertEqual(ctx.exception.details.get("path"), "/etc/shadow")

            # A here-string consumes only one word; chained commands stay live.
            with self.assertRaises(ToolFailure) as ctx:
                runtime.exec_command({"cmd": "grep x <<< hi && cat /etc/passwd"})
            self.assertEqual(ctx.exception.details.get("path"), "/etc/passwd")

    def test_git_helpers_use_command_environment(self) -> None:
        preflight_error = git_fixture_preflight_error()
        if preflight_error is not None:
            self.skipTest(preflight_error)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            (workspace / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            init_git(workspace)

            # GIT_TEST_ASSUME_DIFFERENT_OWNER makes git treat the repo as owned
            # by another user, reproducing the dubious-ownership failure that
            # motivated routing helper subprocesses through the command env.
            probe = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
                env={**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "GIT_CONFIG_GLOBAL": os.devnull},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if probe.returncode == 0:
                self.skipTest("git does not honor GIT_TEST_ASSUME_DIFFERENT_OWNER")

            def runtime_with_git_config(config: Path) -> Runtime:
                return Runtime(
                    workspace,
                    shell_env_policy=ShellEnvPolicy(
                        set={"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "GIT_CONFIG_GLOBAL": str(config)}
                    ),
                )

            without_safe = root / "gitconfig-empty"
            without_safe.write_text("", encoding="utf-8")
            status = runtime_with_git_config(without_safe).git_status({"max_entries": 5})
            self.assertFalse(status.get("is_repo"))
            self.assertTrue(
                any("dubious ownership" in warning for warning in status.get("warnings", [])),
                status.get("warnings"),
            )

            with_safe = root / "gitconfig-safe"
            with_safe.write_text(f"[safe]\n\tdirectory = {workspace.as_posix()}\n", encoding="utf-8")
            runtime = runtime_with_git_config(with_safe)
            status = runtime.git_status({"max_entries": 5})
            self.assertTrue(status.get("is_repo"))
            log = runtime.git_log({"max_count": 1})
            self.assertTrue(log.get("is_repo"))
            self.assertEqual(log.get("commits", [])[0].get("subject"), "baseline fixture")


class FakeReadonlyAnnotationTests(unittest.TestCase):
    """The tools/list annotation override exists for clients that gate on
    annotations, which no server-side permission mode can influence. It is only
    defensible while the lie stays confined to tools/list, so these tests pin
    both halves: what it changes, and what it must never change."""

    MUTATING_TOOLS = ("apply_patch", "exec_command", "write_stdin", "kill_command")

    def test_default_runtime_reports_truthful_annotations(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="dangerous")
            self.assertFalse(runtime.fake_readonly_annotations)
            annotations = {tool["name"]: tool["annotations"] for tool in runtime.list_tools()["tools"]}
            for name in self.MUTATING_TOOLS:
                with self.subTest(tool=name):
                    self.assertFalse(annotations[name]["readOnlyHint"])
            self.assertTrue(annotations["exec_command"]["destructiveHint"])
            self.assertTrue(annotations["exec_command"]["openWorldHint"])
            self.assertIsNone(runtime.server_info_payload()["annotation_override"])

    def test_override_makes_every_listed_tool_report_read_only(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="dangerous", fake_readonly_annotations=True)
            annotations = {tool["name"]: tool["annotations"] for tool in runtime.list_tools()["tools"]}
            self.assertEqual(set(annotations), set(runtime.exposed_tool_names()))
            for name, annotation in annotations.items():
                with self.subTest(tool=name):
                    self.assertIs(annotation["readOnlyHint"], True)
                    self.assertIs(annotation["destructiveHint"], False)
                    self.assertIs(annotation["openWorldHint"], False)

    def test_override_is_disclosed_without_faking_server_info_or_card_annotations(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="dangerous", fake_readonly_annotations=True)
            info = runtime.server_info_payload()
            self.assertEqual(info["annotation_override"], "fake_readonly")

            card_tools = server_module.server_card_payload(runtime)["tools"]
            self.assertEqual(card_tools["annotationOverride"], "fake_readonly")
            for name in self.MUTATING_TOOLS:
                with self.subTest(tool=name):
                    self.assertIn(name, card_tools["readOnlyHintFalse"])
                    self.assertNotIn(name, card_tools["readOnlyHintTrue"])

    def test_override_still_executes_and_mutates(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace, permission_mode="dangerous", fake_readonly_annotations=True)
            result = runtime.exec_command({"cmd": "echo ran > ran.txt", "timeout_ms": 30000, "yield_time_ms": 30000})
            self.assertEqual(result.get("status"), "exited", result)
            self.assertTrue((workspace / "ran.txt").exists(), "read-only annotation must not stop execution")

    def test_override_is_reported_by_check_exec_environment(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="dangerous", fake_readonly_annotations=True)
            warnings = runtime.check_exec_environment({})["warnings"]
            self.assertTrue(
                any("faked as read-only" in warning for warning in warnings),
                warnings,
            )

    def test_override_requires_dangerous_permission_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            for mode in ("safe", "trusted"):
                with self.subTest(permission_mode=mode):
                    with self.assertRaises(ToolFailure):
                        Runtime(Path(tmp), permission_mode=mode, fake_readonly_annotations=True)

    def test_policy_from_args_requires_dangerous_permission_mode(self) -> None:
        parser = server_module.build_parser()
        args = parser.parse_args(["--dangerously-fake-readonly-annotations", "--permission-mode", "trusted"])
        with self.assertRaises(ValueError):
            server_module.runtime_policy_from_args(args)

        args = parser.parse_args(["--dangerously-fake-readonly-annotations", "--permission-mode", "dangerous"])
        self.assertTrue(server_module.runtime_policy_from_args(args).fake_readonly_annotations)

    def test_policy_from_args_reads_the_environment_switch(self) -> None:
        parser = server_module.build_parser()
        args = parser.parse_args(["--permission-mode", "dangerous"])
        with patch.dict(
            os.environ,
            {"CODING_TOOLS_MCP_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS": "1"},
            clear=False,
        ):
            self.assertTrue(server_module.runtime_policy_from_args(args).fake_readonly_annotations)
        self.assertFalse(server_module.runtime_policy_from_args(args).fake_readonly_annotations)

    def test_override_over_http_requires_authentication(self) -> None:
        # A tunnel forwards to a loopback bind, so the bind host cannot tell a
        # private sandbox from a public one. Authentication is the real gate.
        # Ambient CODING_TOOLS_MCP_* vars (e.g. from the devcontainer) feed the
        # parser defaults and would auto-enable auth, turning the expected
        # refusal into a live server that hangs the suite — scrub them first.
        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "CODING_TOOLS_MCP_AUTH_TOKEN",
                "CODING_TOOLS_MCP_HOST",
                "CODING_TOOLS_MCP_PORT",
                "CODING_TOOLS_MCP_GENERATE_AUTH_TOKEN",
            ):
                os.environ.pop(name, None)
            parser = server_module.build_parser()
            with TemporaryDirectory() as tmp:
                argv = [
                    "--workspace",
                    tmp,
                    "--permission-mode",
                    "dangerous",
                    "--dangerously-fake-readonly-annotations",
                ]
                args = parser.parse_args(argv)
                self.assertEqual(server_module.run_http(args), 2)


def file_path(name: str):
    return Path(name)
