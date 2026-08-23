from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import processes
from coding_tools_mcp import server
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime, ShellEnvPolicy

PWSH_SHELL = processes.WindowsCommandShell(
    kind="pwsh",
    executable=r"C:\Program Files\PowerShell\7\pwsh.exe",
)
CMD_SHELL = processes.WindowsCommandShell(
    kind="cmd",
    executable=r"C:\Windows\System32\cmd.exe",
    fallback=True,
)


class WindowsPowerShellSpawnTests(unittest.TestCase):
    def tearDown(self) -> None:
        processes.pwsh_major_version.cache_clear()
        processes._reset_selected_windows_command_shell()

    def test_windows_string_command_uses_server_resolved_noninteractive_pwsh(self) -> None:
        captured: dict[str, object] = {}

        class FakeProcess:
            pass

        def fake_popen(command: object, **kwargs: object) -> FakeProcess:
            captured["command"] = command
            captured.update(kwargs)
            return FakeProcess()

        trusted = r"C:\Program Files\PowerShell\7\pwsh.exe"
        trusted_path = r"C:\Program Files\PowerShell\7;C:\Windows\System32"
        attacker_path = r"C:\workspace\bin"

        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(processes.os, "getcwd", return_value=r"C:\server"),
            patch.dict(processes.os.environ, {"Path": trusted_path}, clear=True),
            patch.object(processes.os.path, "isfile", side_effect=lambda path: path == trusted),
            patch.object(processes, "pwsh_major_version", return_value=7),
            patch.object(processes.subprocess, "Popen", side_effect=fake_popen),
        ):
            process, pty_fd = processes.spawn_process(
                "Write-Output 'ok'",
                cwd=r"C:\workspace",
                shell=True,
                env={"Path": attacker_path},
                tty=False,
                popen_kwargs={},
            )

        self.assertIsInstance(process, FakeProcess)
        self.assertIsNone(pty_fd)
        self.assertEqual(
            captured["command"],
            [
                trusted,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Write-Output 'ok'",
            ],
        )
        self.assertIs(captured["shell"], False)
        self.assertEqual(captured["env"], {"Path": attacker_path})

    def test_windows_string_command_falls_back_to_trusted_cmd(self) -> None:
        captured: dict[str, object] = {}

        class FakeProcess:
            pass

        def fake_popen(command: object, **kwargs: object) -> FakeProcess:
            captured["command"] = command
            captured.update(kwargs)
            return FakeProcess()

        trusted = r"C:\Windows\System32\cmd.exe"
        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(processes.os, "getcwd", return_value=r"C:\server"),
            patch.dict(
                processes.os.environ,
                {
                    "COMSPEC": trusted,
                    "Path": r"C:\Windows\System32",
                    "SystemRoot": r"C:\Windows",
                },
                clear=True,
            ),
            patch.object(processes.os.path, "isfile", side_effect=lambda path: path == trusted),
            patch.object(processes.subprocess, "Popen", side_effect=fake_popen),
        ):
            process, pty_fd = processes.spawn_process(
                "echo ok",
                cwd=r"C:\workspace",
                shell=True,
                env={"Path": r"C:\workspace\bin"},
                tty=False,
                popen_kwargs={},
            )

        self.assertIsInstance(process, FakeProcess)
        self.assertIsNone(pty_fd)
        self.assertEqual(
            captured["command"],
            f'"{trusted}" /D /V:OFF /S /C "echo ok"',
        )
        self.assertIs(captured["shell"], False)
        self.assertEqual(captured["env"], {"Path": r"C:\workspace\bin"})

    def test_cmd_command_line_preserves_embedded_quotes_verbatim(self) -> None:
        # An argv list would be re-serialized with MS CRT rules and reach
        # cmd.exe as \" sequences, corrupting any quoted argument. The raw
        # /S /C command line must carry the payload byte-for-byte.
        command = 'type "C:\\Program Files\\notes.txt" > "out dir\\copy.txt"'
        line = processes.build_cmd_command_line(r"C:\Windows\System32\cmd.exe", command)
        self.assertEqual(
            line,
            '"C:\\Windows\\System32\\cmd.exe" /D /V:OFF /S /C '
            '"type "C:\\Program Files\\notes.txt" > "out dir\\copy.txt""',
        )

    def test_windows_pwsh_resolution_skips_current_directory_entries(self) -> None:
        trusted = r"C:\Program Files\PowerShell\7\pwsh.exe"
        malicious = r"C:\workspace\pwsh.exe"

        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(processes.os, "getcwd", return_value=r"C:\workspace"),
            patch.dict(
                processes.os.environ,
                {"Path": r".;C:\workspace;C:\Program Files\PowerShell\7"},
                clear=True,
            ),
            patch.object(
                processes.os.path,
                "isfile",
                side_effect=lambda path: path in {trusted, malicious},
            ),
            patch.object(processes, "pwsh_major_version", return_value=7),
        ):
            self.assertEqual(processes.resolve_pwsh(), trusted)

    def test_windows_pwsh_rejects_invalid_explicit_path(self) -> None:
        with (
            patch.object(processes.os, "name", "nt"),
            patch.dict(
                processes.os.environ,
                {processes.PWSH_PATH_ENV: r".\pwsh.exe"},
                clear=True,
            ),
            patch.object(processes.os.path, "isfile", return_value=True),
        ):
            with self.assertRaises(ToolFailure) as raised:
                processes.resolve_pwsh()

        self.assertEqual(raised.exception.code, "SHELL_NOT_FOUND")

    def test_invalid_explicit_pwsh_pin_does_not_fall_back(self) -> None:
        with (
            patch.dict(
                processes.os.environ,
                {processes.PWSH_PATH_ENV: r".\pwsh.exe"},
                clear=True,
            ),
            patch.object(processes.os.path, "isfile", return_value=False),
            patch.object(processes, "resolve_cmd") as resolve_cmd,
        ):
            with self.assertRaises(ToolFailure) as raised:
                processes.resolve_windows_command_shell()

        self.assertEqual(raised.exception.code, "SHELL_NOT_FOUND")
        resolve_cmd.assert_not_called()

    def test_windows_shell_falls_back_when_pwsh_is_missing(self) -> None:
        cmd = r"C:\Windows\System32\cmd.exe"
        with (
            patch.object(processes.os, "getcwd", return_value=r"C:\server"),
            patch.dict(
                processes.os.environ,
                {
                    "COMSPEC": cmd,
                    "Path": r"C:\Windows\System32",
                    "SystemRoot": r"C:\Windows",
                },
                clear=True,
            ),
            patch.object(processes.os.path, "isfile", side_effect=lambda path: path == cmd),
        ):
            selected = processes.resolve_windows_command_shell()

        self.assertEqual(selected.kind, "cmd")
        self.assertEqual(selected.executable, cmd)
        self.assertTrue(selected.fallback)
        self.assertEqual(selected.fallback_reason, "SHELL_NOT_FOUND")
        self.assertIn("cmd.exe syntax", selected.warning or "")

    def test_windows_string_command_rejects_powershell_older_than_7(self) -> None:
        executable = r"C:\PowerShell\6\pwsh.exe"
        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(processes.os, "getcwd", return_value=r"C:\server"),
            patch.dict(processes.os.environ, {"Path": r"C:\PowerShell\6"}, clear=True),
            patch.object(processes.os.path, "isfile", side_effect=lambda path: path == executable),
            patch.object(processes, "pwsh_major_version", return_value=6),
        ):
            with self.assertRaises(ToolFailure) as raised:
                processes.resolve_pwsh()

        self.assertEqual(raised.exception.code, "SHELL_VERSION_UNSUPPORTED")

    def test_unpinned_unsupported_pwsh_falls_back_to_cmd(self) -> None:
        pwsh = r"C:\PowerShell\6\pwsh.exe"
        cmd = r"C:\Windows\System32\cmd.exe"
        with (
            patch.object(processes.os, "getcwd", return_value=r"C:\server"),
            patch.dict(
                processes.os.environ,
                {
                    "COMSPEC": cmd,
                    "Path": r"C:\PowerShell\6;C:\Windows\System32",
                },
                clear=True,
            ),
            patch.object(
                processes.os.path,
                "isfile",
                side_effect=lambda path: path in {pwsh, cmd},
            ),
            patch.object(processes, "pwsh_major_version", return_value=6),
        ):
            selected = processes.resolve_windows_command_shell()

        self.assertEqual(selected.kind, "cmd")
        self.assertEqual(selected.fallback_reason, "SHELL_VERSION_UNSUPPORTED")

    def test_windows_shell_reports_error_when_neither_interpreter_exists(self) -> None:
        with (
            patch.object(processes.os, "getcwd", return_value=r"C:\server"),
            patch.dict(processes.os.environ, {"Path": r"C:\empty"}, clear=True),
            patch.object(processes.os.path, "isfile", return_value=False),
        ):
            with self.assertRaises(ToolFailure) as raised:
                processes.resolve_windows_command_shell()

        self.assertEqual(raised.exception.code, "SHELL_NOT_FOUND")

    @unittest.skipUnless(os.name == "nt", "requires Windows PowerShell 7")
    def test_runtime_executes_bare_powershell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                permission_mode="trusted",
                shell_env_policy=ShellEnvPolicy(inherit="all"),
            )
            try:
                result = runtime.exec_command(
                    {
                        "cmd": "Write-Output ('PS_MAJOR=' + $PSVersionTable.PSVersion.Major)",
                        "timeout_ms": 30_000,
                        "yield_time_ms": 30_000,
                        "verbosity": "full",
                    }
                )
            finally:
                runtime.close()

        self.assertEqual(result.get("status"), "exited", result)
        self.assertEqual(result.get("exit_code"), 0, result)
        self.assertIn("PS_MAJOR=7", str(result.get("stdout", "")))

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
    def test_runtime_cmd_fallback_is_visible_and_executes(self) -> None:
        cmd = os.environ["COMSPEC"]
        selected = processes.WindowsCommandShell(
            kind="cmd",
            executable=cmd,
            fallback=True,
            fallback_reason="SHELL_NOT_FOUND",
            warning=processes.CMD_FALLBACK_WARNING,
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                permission_mode="trusted",
                shell_env_policy=ShellEnvPolicy(inherit="all"),
            )
            try:
                with patch.object(
                    server, "selected_windows_command_shell", return_value=selected
                ):
                    result = runtime.exec_command(
                        {
                            "cmd": "echo CMD_FALLBACK_OK",
                            "timeout_ms": 30_000,
                            "yield_time_ms": 30_000,
                            "verbosity": "full",
                        }
                    )
            finally:
                runtime.close()

        self.assertEqual(result.get("exit_code"), 0, result)
        self.assertIn("CMD_FALLBACK_OK", str(result.get("stdout", "")))
        self.assertEqual(result.get("command_shell", {}).get("kind"), "cmd")
        self.assertTrue(result.get("command_shell", {}).get("fallback"))
        self.assertTrue(
            any("cmd.exe compatibility fallback" in warning for warning in result.get("warnings", [])),
            result,
        )

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
    def test_runtime_real_cmd_fallback_without_pwsh_on_path(self) -> None:
        # No mocked resolver or Popen: hide every PATH entry that offers
        # pwsh.exe, let selection fall back to the real cmd.exe, and run a
        # command whose quoted argument would be corrupted by argv re-quoting.
        def offers_pwsh(entry: str) -> bool:
            candidate = entry.strip().strip('"')
            if not candidate:
                return False
            try:
                return (Path(candidate) / "pwsh.exe").is_file()
            except OSError:
                return False

        stripped_path = ";".join(
            entry for entry in os.environ.get("PATH", "").split(";") if not offers_pwsh(entry)
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "has space.txt").write_text("FALLBACK_QUOTES_OK", encoding="utf-8")
            runtime = Runtime(
                Path(tmp),
                permission_mode="trusted",
                shell_env_policy=ShellEnvPolicy(inherit="all"),
            )
            try:
                with patch.dict(os.environ, {"PATH": stripped_path}, clear=False):
                    os.environ.pop(processes.PWSH_PATH_ENV, None)
                    processes._reset_selected_windows_command_shell()
                    result = runtime.exec_command(
                        {
                            "cmd": 'type "has space.txt"',
                            "timeout_ms": 30_000,
                            "yield_time_ms": 30_000,
                            "verbosity": "full",
                        }
                    )
            finally:
                processes._reset_selected_windows_command_shell()
                runtime.close()

        self.assertEqual(result.get("exit_code"), 0, result)
        self.assertIn("FALLBACK_QUOTES_OK", str(result.get("stdout", "")))
        self.assertEqual(result.get("command_shell", {}).get("kind"), "cmd")
        self.assertTrue(result.get("command_shell", {}).get("fallback"), result)

    def test_server_info_and_exec_check_disclose_cmd_fallback(self) -> None:
        selected = processes.WindowsCommandShell(
            kind="cmd",
            executable=r"C:\Windows\System32\cmd.exe",
            fallback=True,
            fallback_reason="SHELL_NOT_FOUND",
            warning=processes.CMD_FALLBACK_WARNING,
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp))
            try:
                summary = {"command_shell": server.windows_command_shell_payload(selected)}
                with patch.object(runtime, "_exec_environment_summary", return_value=summary):
                    info = runtime.server_info_payload()
                    check = runtime.check_exec_environment({})
            finally:
                runtime.close()

        self.assertEqual(info.get("command_shell", {}).get("kind"), "cmd")
        self.assertTrue(info.get("command_shell", {}).get("fallback"))
        self.assertTrue(
            any("cmd.exe compatibility fallback" in warning for warning in check.get("warnings", [])),
            check,
        )


class WindowsShellSelectionCacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        processes._reset_selected_windows_command_shell()

    def test_selection_is_pinned_for_the_process(self) -> None:
        with patch.object(
            processes, "resolve_windows_command_shell", return_value=PWSH_SHELL
        ) as resolver:
            first = processes.selected_windows_command_shell()
            second = processes.selected_windows_command_shell()

        self.assertIs(first, PWSH_SHELL)
        self.assertIs(second, PWSH_SHELL)
        resolver.assert_called_once()

    def test_selection_failure_is_pinned_until_refresh(self) -> None:
        failure = ToolFailure("SHELL_NOT_FOUND", "no shell", category="runtime")
        with patch.object(
            processes,
            "resolve_windows_command_shell",
            side_effect=[failure, CMD_SHELL],
        ) as resolver:
            with self.assertRaises(ToolFailure):
                processes.selected_windows_command_shell()
            # A pinned failure must not re-probe on the exec path.
            with self.assertRaises(ToolFailure):
                processes.selected_windows_command_shell()
            self.assertEqual(resolver.call_count, 1)
            refreshed = processes.selected_windows_command_shell(refresh=True)

        self.assertIs(refreshed, CMD_SHELL)
        self.assertEqual(resolver.call_count, 2)


class WindowsPowerShellPolicyTests(unittest.TestCase):
    def test_safe_mode_blocks_powershell_network_commands_without_url_literals(self) -> None:
        commands = (
            "Invoke-WebRequest -Uri example.com",
            "Invoke-RestMethod -Uri api.example.com",
            "iwr example.com",
            "irm api.example.com",
            "Start-BitsTransfer -Source example.com -Destination out.bin",
            "New-Object System.Net.WebClient",
            "Test-NetConnection example.com",
            "Test-Connection example.com",
            "ping example.com",
            "tnc example.com",
            "Resolve-DnsName example.com",
            "[System.Net.Dns]::GetHostAddresses('example.com')",
            "Write-Output ok\nInvoke-WebRequest -Uri example.com",
            # Module-qualified invocation runs the same cmdlet.
            "Microsoft.PowerShell.Utility\\Invoke-WebRequest -Uri example.com",
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                for command in commands:
                    with self.subTest(command=command):
                        with self.assertRaises(ToolFailure) as raised:
                            runtime._check_command_policy(command, {}, windows_shell=PWSH_SHELL)
                        self.assertEqual(raised.exception.details.get("permission"), "network")
            finally:
                runtime.close()

    def test_safe_mode_blocks_recursive_powershell_deletion_aliases_and_abbreviations(self) -> None:
        commands = (
            "Remove-Item -Recurse .",
            "Remove-Item -Recurse -Force .",
            "Remove-Item -Force -LiteralPath . -Recurse",
            "Remove-Item -Recu -For .",
            "rm -r .",
            "rm -Recurse -Force .",
            "ri -Recu .",
            "ri -Force -Recurse .",
            "del -Recurse .",
            "rmdir -r .",
            "Write-Output ok\r\nRemove-Item -Recurse .",
            # Module-qualified invocation runs the same cmdlet.
            "Microsoft.PowerShell.Management\\Remove-Item -Recurse -Force build",
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                for command in commands:
                    with self.subTest(command=command):
                        with self.assertRaises(ToolFailure) as raised:
                            runtime._check_command_policy(command, {}, windows_shell=PWSH_SHELL)
                        self.assertEqual(
                            raised.exception.details.get("permission"),
                            "destructive_command",
                        )
            finally:
                runtime.close()


class WindowsPowerShellDynamicSyntaxTests(unittest.TestCase):
    """PowerShell resolves commands at runtime, so cmdlet-name scanning alone
    cannot decide whether a command is destructive or reaches the network."""

    def assert_policy(self, command: str, *, mode: str = "safe") -> ToolFailure | None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode=mode)
            try:
                try:
                    runtime._check_command_policy(command, {}, windows_shell=PWSH_SHELL)
                except ToolFailure as failure:
                    return failure
            finally:
                runtime.close()
        return None

    def test_safe_mode_gates_command_names_built_from_variables_and_splatting(self) -> None:
        # Both of these pass every cmdlet-name scan while still invoking a
        # network download and a recursive delete.
        cases = {
            "$c='Invoke-WebRequest'; & $c example.com": "expansion",
            "$p=@{Recurse=$true}; Remove-Item . @p": "expansion",
            "Set-Alias grab Invoke-WebRequest; grab example.com": "dynamic_eval",
            "[IO.File]::Delete('C:\\data\\report.csv')": "static_member",
            "Remove-Item . @args": "splatting",
            ". .\\payload.ps1": "call_operator",
            "iex (Get-Content payload.txt -Raw)": "dynamic_eval",
        }
        for command, construct in cases.items():
            with self.subTest(command=command):
                failure = self.assert_policy(command)
                self.assertIsNotNone(failure, f"{command!r} was allowed in safe mode")
                assert failure is not None
                self.assertEqual(failure.details.get("permission"), "shell_expansion")
                self.assertEqual(failure.details.get("construct"), construct)

    def test_safe_mode_gates_nested_shells_that_smuggle_encoded_scripts(self) -> None:
        for command in (
            "pwsh -EncodedCommand SQBuAHYAbwBrAGUA",
            "pwsh -enc SQBuAHYAbwBrAGUA",
            "pwsh -Command Invoke-WebRequest example.com",
            "powershell.exe -c whoami",
            "cmd /c del /s /q C:\\data",
            # Unquoted absolute interpreter paths must not slip past the gate
            # just because POSIX tokenization eats backslashes.
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -EncodedCommand SQBuAHYAbwBrAGUA",
            "C:\\Windows\\System32\\cmd.exe /c del /s /q data",
        ):
            with self.subTest(command=command):
                failure = self.assert_policy(command)
                self.assertIsNotNone(failure, f"{command!r} was allowed in safe mode")
                assert failure is not None
                self.assertEqual(failure.details.get("permission"), "inline_script")

    def test_safe_mode_still_allows_literal_powershell_commands(self) -> None:
        for command in (
            "Get-ChildItem -Path src -Recurse -Name",
            "Write-Output ok",
            "git commit -m 'fix parser' && git log --oneline -1",
            "git config user.email dev@example.com",
            ".\\build.exe --release",
            "Get-Content README.md | Select-String planet.txt",
            # Single-quoted PowerShell strings are inert: no expansion happens.
            "Write-Output '$5'",
            "git commit -m 'refs #12 :: costs $5'",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.assert_policy(command), f"{command!r} was blocked in safe mode")

    def test_trusted_mode_allows_dynamic_syntax(self) -> None:
        for command in ("$c='Get-Date'; & $c", "Remove-Item . @p"):
            with self.subTest(command=command):
                self.assertIsNone(self.assert_policy(command, mode="trusted"))

    def test_network_scan_does_not_flag_words_ending_in_net(self) -> None:
        self.assertIsNone(server.POWERSHELL_NETWORK_RE.search("Get-Content planet.txt"))
        self.assertIsNotNone(server.POWERSHELL_NETWORK_RE.search("New-Object System.Net.WebClient"))
        self.assertIsNotNone(server.POWERSHELL_NETWORK_RE.search("[Net.Dns]::GetHostAddresses('a')"))


class PosixPolicyIsolationTests(unittest.TestCase):
    """Without a Windows shell the PowerShell and cmd.exe scans must stay off:
    they would otherwise rewrite the POSIX policy contract (rm -r without -f
    and ping were never gated there)."""

    def assert_policy(self, command: str) -> ToolFailure | None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                try:
                    runtime._check_command_policy(command, {})
                except ToolFailure as failure:
                    return failure
            finally:
                runtime.close()
        return None

    def test_posix_hosts_keep_their_existing_expansion_policy(self) -> None:
        self.assertIsNone(self.assert_policy("echo $HOME"))

    def test_posix_hosts_do_not_inherit_powershell_scans(self) -> None:
        for command in ("rm -r build", "ping example.com", "Remove-Item -Recurse ."):
            with self.subTest(command=command):
                self.assertIsNone(
                    self.assert_policy(command),
                    f"{command!r} was blocked by a Windows-only scan on POSIX",
                )


class WindowsCmdPolicyTests(unittest.TestCase):
    def assert_policy(self, command: str, *, mode: str = "safe") -> ToolFailure | None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode=mode)
            try:
                try:
                    runtime._check_command_policy(command, {}, windows_shell=CMD_SHELL)
                except ToolFailure as failure:
                    return failure
            finally:
                runtime.close()
        return None

    def test_safe_mode_blocks_recursive_cmd_deletion(self) -> None:
        for command in (
            "del /s /q data",
            "rmdir /s /q build",
            "format.com D:",
            "format D:",
            "diskpart",
            # Path spellings invoke the same executables.
            "C:\\Windows\\System32\\format.com D:",
        ):
            with self.subTest(command=command):
                failure = self.assert_policy(command)
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual(failure.details.get("permission"), "destructive_command")

    def test_safe_mode_gates_cmd_dynamic_syntax(self) -> None:
        cases = {
            "%COMSPEC% /c echo ok": "expansion",
            "echo %PATH%": "expansion",
            "c^u^r^l example.com": "escape",
            "call tool.cmd": "dynamic_eval",
            "for %f in (*.txt) do type %f": "dynamic_eval",
        }
        for command, construct in cases.items():
            with self.subTest(command=command):
                failure = self.assert_policy(command)
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual(failure.details.get("permission"), "shell_expansion")
                self.assertEqual(failure.details.get("construct"), construct)

    def test_safe_mode_allows_literal_cmd_commands(self) -> None:
        for command in (
            "echo ok",
            "git status",
            "hello.exe",
            "cl.exe /nologo hello.c",
            # A lone % cannot expand on a cmd command line.
            "git log --format=%h",
            "echo 100%",
            # CALL and FOR only evaluate at command position.
            "echo call for help",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.assert_policy(command), f"{command!r} was blocked in safe mode")

    def test_trusted_mode_allows_cmd_dynamic_syntax(self) -> None:
        self.assertIsNone(self.assert_policy("%COMSPEC% /c echo ok", mode="trusted"))


if __name__ == "__main__":
    unittest.main()
