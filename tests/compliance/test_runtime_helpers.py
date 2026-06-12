from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp.server import (
    LANDLOCK_ACCESS_FS_IOCTL_DEV,
    LANDLOCK_ACCESS_FS_TRUNCATE,
    LANDLOCK_ACCESS_FS_WRITE_FILE,
    Runtime,
    ShellEnvPolicy,
    ToolFailure,
    exec_output_diagnostics,
    guard_allow_roots,
    identify_image,
    permission_failure_diagnostics,
    runtime_parent_root,
    truncate_text_head,
    truncate_text_tail,
)


@contextmanager
def fake_landlock_exec() -> Iterator[dict[str, object]]:
    """Patch landlock + Popen so exec_command runs without spawning a process.

    Yields a dict capturing the landlock write_roots and the Popen args/kwargs;
    "read_fd" holds the fd handed to the server (closed by exec_command itself).
    """
    read_fd, write_fd = os.pipe()
    original_open = server_module.open_landlock_ruleset
    original_popen = server_module.subprocess.Popen
    original_watchdog = server_module.start_session_watchdog
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
    server_module.start_session_watchdog = lambda _session: None
    try:
        yield captured
    finally:
        server_module.open_landlock_ruleset = original_open
        server_module.subprocess.Popen = original_popen  # type: ignore[method-assign]
        server_module.start_session_watchdog = original_watchdog
        os.close(write_fd)


class RuntimeHelperTests(unittest.TestCase):
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

    def test_kill_session_keeps_unresponsive_session(self) -> None:
        class StillRunningProcess:
            def poll(self) -> None:
                return None

        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            session = runtime._make_session(StillRunningProcess())  # type: ignore[arg-type]
            runtime.sessions[session.session_id] = session
            with patch.object(runtime, "_terminate_process_group", return_value=None):
                result = runtime.kill_session({"session_id": session.session_id, "wait_ms": 0, "kill_wait_ms": 0})

        self.assertFalse(result.get("killed"), result)
        self.assertEqual(result.get("status"), "terminating", result)
        self.assertFalse(result.get("evicted"), result)
        self.assertIn(session.session_id, runtime.sessions)
        self.assertTrue(any("session retained" in warning for warning in result.get("warnings", [])), result)

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
                dangerously_skip_all_permissions=True,
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

            dangerous_runtime = Runtime(workspace, dangerously_skip_all_permissions=True)
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

    def test_tool_profiles_filter_tools_and_compat_annotations(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            full = Runtime(workspace, tool_profile="full")
            full_tools = full.list_tools()["tools"]
            full_names = {tool["name"] for tool in full_tools}
            self.assertIn("apply_patch", full_names)
            self.assertIn("git_log", full_names)
            self.assertIn("server_info", full_names)

            read_only = Runtime(workspace, tool_profile="read-only")
            read_only_names = {tool["name"] for tool in read_only.list_tools()["tools"]}
            self.assertIn("server_info", read_only_names)
            self.assertIn("set_default_cwd", read_only_names)
            self.assertIn("git_blame", read_only_names)
            self.assertNotIn("apply_patch", read_only_names)
            self.assertNotIn("exec_command", read_only_names)
            self.assertNotIn("write_stdin", read_only_names)
            self.assertNotIn("request_permissions", read_only_names)

            compat = Runtime(workspace, tool_profile="compat-readonly-all")
            compat_tools = compat.list_tools()["tools"]
            self.assertEqual({tool["name"] for tool in compat_tools}, full_names)
            for tool in compat_tools:
                annotations = tool["annotations"]
                self.assertIs(annotations.get("readOnlyHint"), True)
                self.assertIs(annotations.get("destructiveHint"), False)
                self.assertIs(annotations.get("openWorldHint"), False)

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
        if server_module.shutil.which("git") is None:
            self.skipTest("git is not available")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            (workspace / "tracked.txt").write_text("tracked\n", encoding="utf-8")
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

            without_safe = root / "gitconfig-empty"
            without_safe.write_text("", encoding="utf-8")
            runtime = Runtime(
                workspace,
                shell_env_policy=server_module.ShellEnvPolicy(
                    set={"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "GIT_CONFIG_GLOBAL": str(without_safe)}
                ),
            )
            status = runtime.git_status({"max_entries": 5})
            self.assertFalse(status.get("is_repo"))
            self.assertTrue(
                any("dubious ownership" in warning for warning in status.get("warnings", [])),
                status.get("warnings"),
            )

            with_safe = root / "gitconfig-safe"
            with_safe.write_text(f"[safe]\n\tdirectory = {workspace.as_posix()}\n", encoding="utf-8")
            runtime = Runtime(
                workspace,
                shell_env_policy=server_module.ShellEnvPolicy(
                    set={"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "GIT_CONFIG_GLOBAL": str(with_safe)}
                ),
            )
            status = runtime.git_status({"max_entries": 5})
            self.assertTrue(status.get("is_repo"))
            log = runtime.git_log({"max_count": 1})
            self.assertTrue(log.get("is_repo"))
            self.assertEqual(log.get("commits", [])[0].get("subject"), "initial commit")


def file_path(name: str):
    return Path(name)
