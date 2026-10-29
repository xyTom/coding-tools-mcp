from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import shells
from coding_tools_mcp.errors import ToolFailure

PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"
CMD = r"C:\Windows\System32\cmd.exe"
WORKSPACE = r"C:\projects\untrusted"
INSTALLATIONS = ((r"C:\Program Files",), r"C:\Windows\System32")


class ShellSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        shells._selected_shells.clear()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_trusted_ps7_is_preferred_without_searching_path(self) -> None:
        with (patch.object(shells, "_native_installation_paths", return_value=INSTALLATIONS),
              patch.object(shells, "validate_windows_executable", side_effect=lambda path, **kw: path) as trust,
              patch.object(shells, "pwsh_major_version", return_value=7) as probe):
            selected = shells.resolve_windows_command_shell(WORKSPACE)
        self.assertEqual(selected.kind, "pwsh")
        self.assertEqual(selected.executable, PWSH)
        self.assertFalse(selected.fallback)
        trust.assert_called_once()
        probe.assert_called_once_with(PWSH, system_directory=INSTALLATIONS[1])

    def test_explicit_bad_pin_does_not_fall_back(self) -> None:
        os.environ[shells.PWSH_PATH_ENV] = r"C:\workspace\pwsh.exe"
        with (patch.object(shells, "_native_installation_paths", return_value=INSTALLATIONS),
              patch.object(shells, "pwsh_major_version") as probe):
            with self.assertRaises(ToolFailure) as failure:
                shells.resolve_windows_command_shell(WORKSPACE)
        self.assertEqual(failure.exception.code, "SHELL_UNTRUSTED")
        probe.assert_not_called()

    def test_empty_pin_is_configuration_error(self) -> None:
        os.environ[shells.PWSH_PATH_ENV] = " "
        with self.assertRaises(ToolFailure) as failure:
            shells.resolve_windows_command_shell(WORKSPACE)
        self.assertEqual(failure.exception.code, "SHELL_CONFIGURATION_ERROR")

    def test_cmd_has_actual_shell_and_reason(self) -> None:
        def trust(path: str, **kwargs: object) -> str:
            if path == PWSH:
                raise ToolFailure("SHELL_NOT_FOUND", "not installed")
            return path
        with (patch.object(shells, "_native_installation_paths", return_value=INSTALLATIONS),
              patch.object(shells, "validate_windows_executable", side_effect=trust),
              patch.object(shells, "pwsh_major_version") as probe):
            selected = shells.resolve_windows_command_shell(WORKSPACE)
        payload = shells.windows_command_shell_payload(selected)
        self.assertEqual(payload["kind"], "cmd")
        self.assertEqual(payload["executable"], CMD)
        self.assertEqual(payload["fallback_reason"], "SHELL_NOT_FOUND")
        self.assertTrue(payload["fallback"])
        self.assertIn("cmd.exe", payload["warning"])
        probe.assert_not_called()

    def test_cmd_fallback_can_be_disabled(self) -> None:
        os.environ[shells.CMD_FALLBACK_ENV] = "false"
        with (patch.object(shells, "_native_installation_paths", return_value=INSTALLATIONS),
              patch.object(shells, "validate_windows_executable", side_effect=ToolFailure("SHELL_NOT_FOUND", "missing")) as trust):
            with self.assertRaises(ToolFailure):
                shells.resolve_windows_command_shell(WORKSPACE)
        trust.assert_called_once()

    def test_explicit_cmd_never_probes_powershell(self) -> None:
        os.environ[shells.WINDOWS_SHELL_ENV] = "cmd"
        with (patch.object(shells, "_native_installation_paths", return_value=INSTALLATIONS),
              patch.object(shells, "validate_windows_executable", side_effect=lambda path, **kw: path) as trust,
              patch.object(shells, "pwsh_major_version") as probe):
            selected = shells.resolve_windows_command_shell(WORKSPACE)
        self.assertFalse(selected.fallback)
        self.assertEqual(selected.fallback_reason, "configured_cmd")
        trust.assert_called_once_with(CMD, workspace=WORKSPACE, kind="cmd", installation_paths=INSTALLATIONS)
        probe.assert_not_called()

    def test_no_windows_powershell_51_fallback(self) -> None:
        os.environ[shells.PWSH_PATH_ENV] = PWSH
        with (patch.object(shells, "_native_installation_paths", return_value=INSTALLATIONS),
              patch.object(shells, "validate_windows_executable", side_effect=lambda path, **kw: path),
              patch.object(shells, "pwsh_major_version", return_value=5)):
            with self.assertRaises(ToolFailure) as failure:
                shells.resolve_windows_command_shell(WORKSPACE)
        self.assertEqual(failure.exception.code, "SHELL_VERSION_UNSUPPORTED")

    def test_cached_selection_revalidates_path_and_scope(self) -> None:
        selected = shells.WindowsCommandShell("pwsh", PWSH)
        with (patch.object(shells, "resolve_windows_command_shell", return_value=selected) as resolve,
              patch.object(shells, "validate_windows_executable", return_value=PWSH) as validate):
            shells.selected_windows_command_shell(WORKSPACE)
            shells.selected_windows_command_shell(WORKSPACE)
            shells.selected_windows_command_shell(r"C:\another-workspace")
        self.assertEqual(resolve.call_count, 2)
        validate.assert_called_once_with(PWSH, workspace=WORKSPACE, kind="pwsh")

    def test_refresh_reprobes_version(self) -> None:
        with patch.object(shells, "resolve_windows_command_shell", return_value=shells.WindowsCommandShell("pwsh", PWSH)) as resolve:
            shells.selected_windows_command_shell(WORKSPACE)
            shells.selected_windows_command_shell(WORKSPACE, refresh=True)
        self.assertEqual(resolve.call_count, 2)

    def test_probe_environment_drops_secrets_and_loader_variables(self) -> None:
        os.environ.update({"SECRET_TOKEN": "secret", "PSModulePath": WORKSPACE, "PATH": WORKSPACE})
        completed = subprocess.CompletedProcess([], 0, b"7\r\n", b"")
        with patch.object(shells.subprocess, "run", return_value=completed) as probe:
            self.assertEqual(shells.pwsh_major_version(PWSH, system_directory=INSTALLATIONS[1]), 7)
        kwargs = probe.call_args.kwargs
        self.assertEqual(kwargs["env"]["PATH"], INSTALLATIONS[1])
        self.assertNotIn("SECRET_TOKEN", kwargs["env"])
        self.assertNotIn("PSModulePath", kwargs["env"])
        self.assertEqual(kwargs["cwd"], r"C:\Program Files\PowerShell\7")
        self.assertFalse(kwargs["text"])


class ShellTrustTests(unittest.TestCase):
    def test_relative_unc_drive_relative_device_and_quote_paths_are_rejected(self) -> None:
        cases = ("pwsh.exe", r"C:pwsh.exe", r"\pwsh.exe", r"\\server\share\pwsh.exe",
                 r"\\?\C:\Program Files\PowerShell\7\pwsh.exe", '"' + PWSH + '"',
                 r"%ProgramFiles%\PowerShell\7\pwsh.exe")
        for path in cases:
            with self.subTest(path=path), self.assertRaises(ToolFailure):
                shells.validate_windows_executable(path, workspace=WORKSPACE, kind="pwsh", installation_paths=INSTALLATIONS)

    def test_workspace_is_not_process_cwd(self) -> None:
        with self.assertRaises(ToolFailure) as failure:
            shells.validate_windows_executable(PWSH, workspace=r"C:\Program Files\PowerShell", kind="pwsh", installation_paths=INSTALLATIONS)
        self.assertEqual(failure.exception.code, "SHELL_UNTRUSTED")

    def test_path_candidate_outside_installation_is_never_probed(self) -> None:
        with self.assertRaises(ToolFailure):
            shells.validate_windows_executable(r"C:\user-writable\pwsh.exe", workspace=WORKSPACE, kind="pwsh", installation_paths=INSTALLATIONS)

    def test_cmd_comspec_cannot_replace_system_processor(self) -> None:
        with self.assertRaises(ToolFailure):
            shells.validate_windows_executable(r"C:\Program Files\cmd.exe", workspace=WORKSPACE, kind="cmd", installation_paths=INSTALLATIONS)

    def _stat(self, path: str) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFREG if path == PWSH else stat.S_IFDIR, st_file_attributes=0)

    def test_junction_in_installation_path_is_rejected(self) -> None:
        def lstat(path: str) -> SimpleNamespace:
            result = self._stat(path)
            if path == r"C:\Program Files\PowerShell":
                result.st_file_attributes = 0x400
            return result
        with (patch.object(shells.os, "lstat", side_effect=lstat),
              patch.object(shells.os.path, "realpath", side_effect=lambda path: path),
              patch.object(shells, "_protected_windows_acl", return_value=True)):
            with self.assertRaises(ToolFailure) as failure:
                shells.validate_windows_executable(PWSH, workspace=WORKSPACE, kind="pwsh", installation_paths=INSTALLATIONS)
        self.assertIn("junction", failure.exception.message)

    def test_replaceable_executable_is_rejected_before_probe(self) -> None:
        with (patch.object(shells.os, "lstat", side_effect=self._stat),
              patch.object(shells.os.path, "realpath", side_effect=lambda path: path),
              patch.object(shells, "_protected_windows_acl", return_value=False),
              patch.object(shells, "pwsh_major_version") as probe):
            with self.assertRaises(ToolFailure) as failure:
                shells.validate_windows_executable(PWSH, workspace=WORKSPACE, kind="pwsh", installation_paths=INSTALLATIONS)
        self.assertIn("replaced", failure.exception.message)
        probe.assert_not_called()

    def test_protected_executable_and_all_ancestors_are_checked(self) -> None:
        with (patch.object(shells.os, "lstat", side_effect=self._stat),
              patch.object(shells.os.path, "realpath", side_effect=lambda path: path),
              patch.object(shells, "_protected_windows_acl", return_value=True) as acl):
            self.assertEqual(shells.validate_windows_executable(PWSH, workspace=WORKSPACE, kind="pwsh", installation_paths=INSTALLATIONS), PWSH)
        self.assertEqual(acl.call_count, 5)
        self.assertEqual(acl.call_args_list[1].kwargs, {"executable_directory": True})


class ShellEnvironmentAndSyntaxTests(unittest.TestCase):
    def test_windows_merges_case_insensitively_without_losing_msvc(self) -> None:
        merged = shells.merge_environment({"Path": "old", "INCLUDE": "headers", "LIB": "libs"},
                                           {"PATH": "new"}, {"path": "latest"}, windows=True)
        self.assertEqual(merged, {"INCLUDE": "headers", "LIB": "libs", "path": "latest"})

    def test_posix_merge_is_case_sensitive(self) -> None:
        self.assertEqual(shells.merge_environment({"Path": "a"}, {"PATH": "b"}), {"Path": "a", "PATH": "b"})

    def test_filter_runs_after_configuration_and_request_overrides(self) -> None:
        from coding_tools_mcp.server import is_filtered_env_var
        env = shells.sanitized_environment(
            {"Path": "tools", "INCLUDE": "headers"},
            {"AWS_SECRET_ACCESS_KEY": "secret", "LD_PRELOAD": "bad.so", "PSModulePath": "bad-modules", "CoReClr_Enable_Profiling": "1"},
            {"path": "new-tools", "DYLD_INSERT_LIBRARIES": "bad.dylib"},
            windows=True, is_filtered=is_filtered_env_var,
        )
        self.assertEqual(env, {"INCLUDE": "headers", "path": "new-tools"})

    def test_pathext_is_case_insensitive_validated_and_deduplicated(self) -> None:
        self.assertEqual(shells.windows_pathext({"PathExt": ".EXE;.cmd;.exe;.wrong/slash; .BAT "}), (".EXE", ".CMD", ".BAT"))
        self.assertEqual(shells.windows_pathext({}), (".COM", ".EXE", ".BAT", ".CMD"))

    def test_cmd_quotes_are_not_crt_escaped(self) -> None:
        command = r'"C:\some directory\tool.exe" "hello world" && echo done'
        result = shells.build_cmd_command_line(CMD, command)
        self.assertEqual(result, f'"{CMD}" /D /V:OFF /S /C "{command}"')
        self.assertNotIn(r'\"', result)

    def test_pwsh_argv_has_no_profile_and_retains_command_exactly(self) -> None:
        command = "Write-Output '中文'"
        argv = shells.build_pwsh_argv(PWSH, command)
        self.assertEqual(argv, [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command])

    def test_powershell_single_quoted_dynamic_text_is_literal(self) -> None:
        for command in ("Write-Output '$(Get-Date)'", "Write-Output 'it''s $(Get-Date)'",
                        "Write-Output '$env:SECRET [IO.File]::Delete(foo)'",
                        "Write-Output @'\n$(Get-Date)\n'@"):
            with self.subTest(command=command):
                self.assertIsNone(shells.powershell_dynamic_construct(command))

    def test_double_quoted_embedded_single_quotes_do_not_mask_expansion(self) -> None:
        self.assertEqual(shells.powershell_dynamic_construct('Write-Output "\'$(Get-Date)\'"'), "expansion")

    def test_dynamic_scanner_only_hints_and_does_not_claim_safety(self) -> None:
        for command in ("Write-Output $(Get-Date)", "(Get-Command Remove-Item) foo", "& $command", "[IO.File]::Delete('foo')"):
            with self.subTest(command=command):
                self.assertIsNotNone(shells.powershell_dynamic_construct(command))
        self.assertIsNone(shells.cmd_dynamic_construct("echo 100%"))
        self.assertEqual(shells.cmd_dynamic_construct("echo %PATH%"), "expansion")
        self.assertEqual(shells.cmd_dynamic_construct("call tool.cmd"), "dynamic_eval")


class WindowsRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from coding_tools_mcp import server
        self.server = server
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = server.Runtime(Path(self.temporary.name), permission_mode="safe")
        self.addCleanup(self.runtime.close)
        # Replacing only the server reference avoids globally changing os.name
        # and making pathlib instantiate WindowsPath on a Linux test runner.
        self.platform = patch.object(server, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.selection = patch.object(server, "selected_windows_command_shell", return_value=shells.WindowsCommandShell("pwsh", PWSH))
        self.selection.start()
        self.addCleanup(self.selection.stop)

    def test_runtime_allows_single_quoted_get_date_literal(self) -> None:
        self.runtime._check_command_policy("Write-Output '$(Get-Date)'", {})

    def test_runtime_still_blocks_double_quoted_get_date_expansion(self) -> None:
        with self.assertRaises(ToolFailure) as failure:
            self.runtime._check_command_policy('Write-Output "$(Get-Date)"', {})
        self.assertEqual(failure.exception.details.get("permission"), "shell_expansion")

    def test_runtime_does_not_hide_single_quoted_absolute_paths(self) -> None:
        with self.assertRaises(ToolFailure) as failure:
            self.runtime._check_command_policy("Get-Content 'C:/outside/secret.txt'", {})
        self.assertEqual(failure.exception.details.get("permission"), "filesystem_escape")

    def test_runtime_mixed_case_environment_merge_preserves_msvc_and_filters_loaders(self) -> None:
        self.runtime.shell_env_policy = self.server.ShellEnvPolicy(inherit="all", set={"pAtH": "configured-tools", "PSModulePath": "malicious-modules"})
        with patch.dict(os.environ, {"Path": "host-tools", "INCLUDE": "headers", "LIB": "libs", "AWS_SECRET_ACCESS_KEY": "secret"}, clear=True):
            result = self.runtime._command_env({"PATH": "request-tools", "CoreCLR_ENABLE_PROFILING": "1"})
        self.assertEqual([key for key in result if key.upper() == "PATH"], ["PATH"])
        self.assertEqual(result["PATH"], "request-tools")
        self.assertEqual(result["INCLUDE"], "headers")
        self.assertEqual(result["LIB"], "libs")
        for key in result:
            self.assertFalse(shells.is_windows_loader_env_name(key), key)
            self.assertFalse(self.server.is_filtered_env_var(key, result[key]), key)


if __name__ == "__main__":
    unittest.main()
