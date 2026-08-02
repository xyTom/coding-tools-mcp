from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import processes
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime, ShellEnvPolicy


class WindowsPowerShellSpawnTests(unittest.TestCase):
    def tearDown(self) -> None:
        processes.pwsh_major_version.cache_clear()

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

    def test_windows_string_command_requires_pwsh(self) -> None:
        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(processes.os, "getcwd", return_value=r"C:\server"),
            patch.dict(processes.os.environ, {"Path": r"C:\Windows\System32"}, clear=True),
            patch.object(processes.os.path, "isfile", return_value=False),
        ):
            with self.assertRaises(ToolFailure) as raised:
                processes.resolve_pwsh()

        self.assertEqual(raised.exception.code, "SHELL_NOT_FOUND")

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
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                for command in commands:
                    with self.subTest(command=command):
                        with self.assertRaises(ToolFailure) as raised:
                            runtime._check_command_policy(command, {})
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
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                for command in commands:
                    with self.subTest(command=command):
                        with self.assertRaises(ToolFailure) as raised:
                            runtime._check_command_policy(command, {})
                        self.assertEqual(
                            raised.exception.details.get("permission"),
                            "destructive_command",
                        )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
