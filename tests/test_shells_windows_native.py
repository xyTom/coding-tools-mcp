"""Native Windows shell acceptance. Linux mocks are not acceptance evidence.

Run twice on native Windows:
  CODING_TOOLS_MCP_WINDOWS_SHELL=cmd python -m unittest tests.test_shells_windows_native
  CODING_TOOLS_MCP_WINDOWS_SHELL=pwsh python -m unittest tests.test_shells_windows_native
A configured missing shell is an error, never a successful skipped test. The
cmd-selected run proves independence from PS7 but is not an absent-PS7 image.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import shells
from coding_tools_mcp.errors import ToolFailure


@unittest.skipUnless(os.name == "nt", "Requires native Windows; mocks are not acceptance")
class NativeWindowsShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="coding-tools-shell-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "中文 workspace & quoted"
        self.workspace.mkdir()
        self.selected = shells.selected_windows_command_shell(str(self.workspace), refresh=True)

    def run_command(self, command: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(shells.shell_command(command, self.selected), shell=False,
                              cwd=str(self.workspace), env=env or dict(os.environ),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=30, check=False)

    def test_actual_shell_matches_configuration(self) -> None:
        requested = os.environ.get(shells.WINDOWS_SHELL_ENV, "auto")
        if requested in {"cmd", "pwsh"}:
            self.assertEqual(self.selected.kind, requested)
            self.assertFalse(self.selected.fallback)
        command = "echo cmd-native-ok" if self.selected.kind == "cmd" else "Write-Output 'pwsh-native-ok'"
        result = self.run_command(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn((self.selected.kind + "-native-ok").encode(), result.stdout)

    def test_runtime_reports_selected_shell_exit_status(self) -> None:
        from coding_tools_mcp.server import Runtime

        script = self.workspace / "native status.py"
        script.write_text("raise SystemExit(7)\n", encoding="utf-8")
        if self.selected.kind == "pwsh":
            native = "& " + " ".join("'" + str(value).replace("'", "''") + "'"
                                      for value in (sys.executable, script))
            cases = ((native, 1), (native + "; exit $LASTEXITCODE", 7),
                     (native + "; Write-Output 'continued'", 0), ("throw 'fixture failure'", 1), ("exit 7", 7))
        else:
            native = " ".join('"' + str(value) + '"' for value in (sys.executable, script))
            cases = ((native, 7), ("exit /b 7", 7))
        runtime = Runtime(self.workspace, permission_mode="dangerous")
        self.addCleanup(runtime.close)
        for command, expected in cases:
            with self.subTest(command=command):
                direct = self.run_command(command)
                self.assertEqual(direct.returncode, expected, direct.stderr)
                result = runtime.exec_command({"cmd": command, "timeout_ms": 30_000, "yield_time_ms": 10_000})
                self.assertEqual(result["status"], "exited", result)
                self.assertEqual(result["exit_code"], expected, result)

    def test_quoted_executable_arguments_and_chinese_working_directory(self) -> None:
        script = self.workspace / "参数 program.py"
        script.write_text("import os, sys\nassert sys.argv[1:] == ['two words', '中文', 'literal & character']\nprint('quoted-native-ok')\n", encoding="utf-8")
        if self.selected.kind == "pwsh":
            def quote(value: object) -> str:
                return "'" + str(value).replace("'", "''") + "'"
            command = "& " + " ".join(quote(value) for value in (sys.executable, script, "two words", "中文", "literal & character"))
        else:
            command = " ".join('"' + str(value) + '"' for value in (sys.executable, script, "two words", "中文", "literal & character"))
        result = self.run_command(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"quoted-native-ok", result.stdout)

    def test_pathext_cmd_script_in_path(self) -> None:
        script = self.workspace / "native_pathext_probe.cmd"
        script.write_bytes(b"@echo off\r\necho pathext-native-ok\r\n")
        env = shells.merge_environment(dict(os.environ),
                                       {"Path": str(self.workspace) + ";" + (shells.environment_value(os.environ, "PATH") or ""),
                                        "PathExt": ".COM;.EXE;.BAT;.CMD"}, windows=True)
        result = self.run_command("native_pathext_probe", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"pathext-native-ok", result.stdout)

    def test_single_quoted_get_date_is_literal_in_powershell(self) -> None:
        if self.selected.kind == "pwsh":
            command, expected = "Write-Output '$(Get-Date)'", b"$(Get-Date)"
        else:
            command, expected = "echo $(Get-Date)", b"$(Get-Date)"
        result = self.run_command(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(expected, result.stdout)

    def test_workspace_path_and_comspec_do_not_select_untrusted_executable(self) -> None:
        # A real copy is executable on Windows. If discovery executes it as
        # PowerShell, it is Python rather than PS7 and cannot pass the test.
        import shutil
        fake_pwsh = self.workspace / "pwsh.exe"
        shutil.copyfile(sys.executable, fake_pwsh)
        env = {"PATH": str(self.workspace), "COMSPEC": str(fake_pwsh)}
        with patch.dict(os.environ, env):
            selected = shells.resolve_windows_command_shell(str(self.workspace))
        self.assertNotEqual(Path(selected.executable), fake_pwsh)
        self.assertEqual(selected.kind, self.selected.kind)

    def test_explicit_workspace_pin_is_rejected_without_execution(self) -> None:
        fake = self.workspace / "pwsh.exe"
        fake.write_bytes(b"not executable")
        with patch.dict(os.environ, {shells.PWSH_PATH_ENV: str(fake), shells.WINDOWS_SHELL_ENV: "pwsh"}):
            with self.assertRaises(ToolFailure) as failure:
                shells.resolve_windows_command_shell(str(self.workspace))
        self.assertEqual(failure.exception.code, "SHELL_UNTRUSTED")

    def test_native_dacl_rejects_replaceable_installation(self) -> None:
        import shutil
        installation = Path(self.temporary.name) / "replaceable-installation"
        installation.mkdir()
        fake = installation / "pwsh.exe"
        shutil.copyfile(sys.executable, fake)
        # Windows runner TEMP may use an 8.3 ancestor spelling. Resolve the
        # fixture before validation so canonical-path rejection cannot mask
        # the DACL rejection this acceptance test is intended to exercise.
        installation = installation.resolve(strict=True)
        fake = fake.resolve(strict=True)
        _, system_directory = shells._native_installation_paths()
        # Exercise real Windows DACL inspection using a deliberately unsafe
        # test root, rather than claiming mocked ACL decisions are acceptance.
        with patch.object(shells, "_protected_windows_acl", wraps=shells._protected_windows_acl) as inspect_acl:
            with self.assertRaises(ToolFailure) as failure:
                shells.validate_windows_executable(str(fake), workspace=str(self.workspace), kind="pwsh",
                    installation_paths=((str(installation),), system_directory))
        inspect_acl.assert_any_call(str(fake), executable_directory=False)
        self.assertEqual(failure.exception.code, "SHELL_UNTRUSTED")
        self.assertIn("replaced", failure.exception.message)

    def test_native_junction_is_rejected(self) -> None:
        installation = Path(self.temporary.name) / "junction-installation"
        installation.mkdir()
        junction = installation / "linked-shell"
        _, system_directory = shells._native_installation_paths()
        target = str(Path(self.selected.executable).parent)
        cmd = str(Path(system_directory) / "cmd.exe")
        result = subprocess.run(shells.build_cmd_command_line(cmd, f'mklink /J "{junction}" "{target}"'),
                                shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            if self.selected.kind == "pwsh":
                candidate = junction / "pwsh.exe"
            else:
                # cmd trust requires an exact system path. Supply the test
                # system directory so the native reparse inspection is reached.
                candidate = junction / "cmd.exe"
            with self.assertRaises(ToolFailure) as failure:
                shells.validate_windows_executable(str(candidate), workspace=str(self.workspace),
                    kind=self.selected.kind,
                    installation_paths=((str(installation),), str(junction)))
            self.assertEqual(failure.exception.code, "SHELL_UNTRUSTED")
            self.assertIn("junction", failure.exception.message)
        finally:
            os.rmdir(junction)

    def test_cmd_selection_independent_of_powershell_probe(self) -> None:
        # Use a native cmd invocation after enforcing that the PS probe cannot
        # be called. This validates the selection contract, not PS7 absence.
        config = {shells.WINDOWS_SHELL_ENV: "cmd"}
        with patch.dict(os.environ, config):
            os.environ.pop(shells.PWSH_PATH_ENV, None)
            with patch.object(shells, "pwsh_major_version", side_effect=AssertionError("cmd selection probed PS7")):
                selected = shells.resolve_windows_command_shell(str(self.workspace))
        result = subprocess.run(shells.shell_command("echo cmd-only-native-ok", selected),
                                shell=False, cwd=str(self.workspace), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"cmd-only-native-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
