"""Command execution contract: stdin, output refs, retention, kill, Windows paths."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from coding_tools_mcp import processes as processes_module
from coding_tools_mcp import server as server_module
from coding_tools_mcp import telemetry
from coding_tools_mcp.server import Runtime, input_schemas, output_schemas
from coding_tools_mcp.telemetry import FAILED_OPERATION_OUTCOMES
from tests.test_telemetry import LEGACY_PROTOCOL_VERSION, _CapturingSender, _properties, scrubbed_env

PY = shlex.quote(sys.executable) if os.name != "nt" else f'"{sys.executable}"'
ECHO_LINE = f"{PY} -u -c \"import sys; print('got ' + sys.stdin.readline().strip())\""
SLEEPER = f'{PY} -c "import time; time.sleep(10)"'


def _text(result: dict[str, Any]) -> str:
    """Join all text blocks in an MCP result for assertions on rendered output."""
    return "\n".join(item["text"] for item in result["content"] if item.get("type") == "text")


class _RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        """Create an isolated trusted runtime for command execution tests."""
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = Runtime(self.root, permission_mode="trusted")

    def tearDown(self) -> None:
        """Close managed commands and remove the temporary workspace."""
        self.runtime.close()
        self._tmp.cleanup()

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a command tool through the fixture runtime."""
        return self.runtime.call_tool(tool, args)

    def start(self, cmd: str, **extra: Any) -> dict[str, Any]:
        """Start a command with a bounded lifetime and return its successful structured result."""
        result = self.call("exec_command", {"cmd": cmd, "timeout_ms": 20_000, **extra})
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def wait_exit(self, command_id: str) -> None:
        """Wait for a tracked process to exit and refresh its command state."""
        with self.runtime.commands_lock:
            command = self.runtime.commands.get(command_id) or self.runtime.output_commands.get(command_id)
        assert command is not None
        command.process.wait(timeout=10)
        command.refresh_status()


class KeepStdinOpenTests(_RuntimeCase):
    def test_non_tty_command_with_keep_stdin_open_accepts_write_stdin(self) -> None:
        """Verify a non-TTY command can receive later input when keep_stdin_open is enabled."""
        started = self.start(ECHO_LINE, keep_stdin_open=True, yield_time_ms=200)
        self.assertEqual(started["status"], "running", started)
        result = self.call(
            "write_stdin", {"command_id": started["command_id"], "chars": "hello\n", "yield_time_ms": 5000}
        )
        self.assertFalse(result.get("isError"), result)
        payload = result["structuredContent"]
        for _ in range(20):
            if payload["status"] != "running":
                break
            payload = self.call(
                "write_stdin", {"command_id": started["command_id"], "chars": "", "yield_time_ms": 500}
            )["structuredContent"]
        self.assertEqual(payload["status"], "exited")
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn("got hello", read_all_stdout(self, started["command_id"]))

    def test_default_closes_stdin_so_readers_get_eof(self) -> None:
        """Verify default stdin closure gives readers EOF and lets them finish."""
        payload = self.start(f"{PY} -c \"import sys; print(repr(sys.stdin.read()))\"", yield_time_ms=10_000)
        self.assertEqual(payload["status"], "exited")
        self.assertIn("''", payload["stdout"])

    def test_writing_to_a_running_command_with_closed_stdin_explains_how_to_interact(self) -> None:
        """Verify closed-input errors explain interaction options while empty polling still works."""
        started = self.start(SLEEPER, yield_time_ms=0)
        result = self.call("write_stdin", {"command_id": started["command_id"], "chars": "y\n"})
        self.assertTrue(result["isError"])
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "COMMAND_CLOSED")
        self.assertEqual(error["details"]["reason"], "stdin_closed")
        self.assertIn("still running", error["message"])
        hint = error["details"]["retry_hint"]
        for needle in ("keep_stdin_open=true", "tty=true", "kill_command", "stdin"):
            self.assertIn(needle, hint)
        self.assertIn("keep_stdin_open=true", _text(result))
        # Polling still works.
        polled = self.call("write_stdin", {"command_id": started["command_id"], "chars": "", "yield_time_ms": 0})
        self.assertFalse(polled.get("isError"), polled)

    def test_writing_to_an_exited_command_reports_its_exit(self) -> None:
        """Verify writing to an exited command returns its actual exit status and code."""
        started = self.start(f'{PY} -c "import sys; sys.exit(4)"', keep_stdin_open=True, yield_time_ms=0)
        self.wait_exit(started["command_id"])
        result = self.call("write_stdin", {"command_id": started["command_id"], "chars": "y\n"})
        self.assertTrue(result["isError"])
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "COMMAND_CLOSED")
        self.assertEqual(error["message"], "Command is closed; stdin write blocked.")
        self.assertEqual(error["details"]["reason"], "exited")
        self.assertEqual(error["details"]["exit_code"], 4)
        self.assertEqual(error["details"]["status"], "exited")

    def test_schema_documents_stdin_tty_and_keep_stdin_open(self) -> None:
        """Verify input schemas explain stdin lifetime and POSIX-only TTY support."""
        props = input_schemas()["exec_command"]["properties"]
        self.assertIs(props["keep_stdin_open"]["default"], False)
        self.assertIn("write_stdin", props["keep_stdin_open"]["description"])
        self.assertIn("POSIX only", props["tty"]["description"])
        self.assertIn("keep_stdin_open", props["stdin"]["description"])
        description = server_module.TOOL_REGISTRY["write_stdin"].description
        self.assertIn("keep_stdin_open=true or tty=true", description)


def read_all_stdout(case: _RuntimeCase, command_id: str) -> str:
    """Read a single stdout page of up to 65536 bytes for the small test commands."""
    result = case.call("read_output", {"output_ref": command_id, "limit": 65536})
    case.assertFalse(result.get("isError"), result)
    return str(result["structuredContent"]["content"])


class OutputRefTests(_RuntimeCase):
    def test_exec_and_write_stdin_always_return_output_refs(self) -> None:
        """Verify both completed execution and running polls expose stable stream references."""
        exited = self.start("echo hi", yield_time_ms=10_000)
        command_id = exited["command_id"]
        self.assertEqual(exited["status"], "exited")
        self.assertNotIn("output_ref", exited)
        self.assertEqual(
            exited["output_refs"],
            {"stdout": f"command:{command_id}:stdout", "stderr": f"command:{command_id}:stderr"},
        )
        running = self.start(SLEEPER, yield_time_ms=0)
        polled = self.call("write_stdin", {"command_id": running["command_id"], "chars": "", "yield_time_ms": 0})
        self.assertEqual(
            polled["structuredContent"]["output_refs"]["stdout"], f"command:{running['command_id']}:stdout"
        )
        self.assertIn(f"command:{running['command_id']}:stdout", _text(polled))

    def test_read_output_accepts_a_bare_command_id_or_command_prefix_with_stream(self) -> None:
        """Verify short output references normalize correctly with explicit or default streams."""
        payload = self.start("echo out; echo err >&2", yield_time_ms=10_000)
        command_id = payload["command_id"]
        cases = [
            ({"output_ref": command_id}, "stdout", "out"),
            ({"output_ref": command_id, "stream": "stderr"}, "stderr", "err"),
            ({"output_ref": f"command:{command_id}", "stream": "stderr"}, "stderr", "err"),
            ({"output_ref": f"command:{command_id}:stderr", "stream": "stderr"}, "stderr", "err"),
            ({"output_ref": f"command:{command_id}:stdout"}, "stdout", "out"),
        ]
        for args, stream, content in cases:
            with self.subTest(args=args):
                result = self.call("read_output", args)
                self.assertFalse(result.get("isError"), result)
                page = result["structuredContent"]
                self.assertEqual(page["stream"], stream)
                self.assertEqual(page["output_ref"], f"command:{command_id}:{stream}")
                self.assertEqual(page["content"].strip(), content)

    def test_a_full_ref_with_a_contradicting_stream_is_invalid(self) -> None:
        """Verify a stream argument cannot contradict a fully qualified output reference."""
        payload = self.start("echo out", yield_time_ms=10_000)
        result = self.call(
            "read_output", {"output_ref": f"command:{payload['command_id']}:stdout", "stream": "stderr"}
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "INVALID_ARGUMENT")


class ReadOutputStatusTests(_RuntimeCase):
    def test_read_output_text_reports_evicted_bytes_on_the_last_page(self) -> None:
        """Verify the final output page still renders dropped-byte and eviction warnings."""
        started = self.start(f'{PY} -c "pass"', yield_time_ms=10_000)
        command_id = started["command_id"]
        command = self.runtime.output_commands[command_id]
        command.buffer_limit = 32
        command.append_stdout(b"x" * 64)
        result = self.call("read_output", {"output_ref": command_id, "offset": 4, "limit": 64})
        payload = result["structuredContent"]
        self.assertIsNone(payload["next_offset"])
        self.assertEqual(payload["omitted_bytes"], 32)
        self.assertIn("offset skipped dropped bytes", _text(result))
        self.assertIn("was evicted", _text(result))

    def test_read_output_reports_status_and_exit_code(self) -> None:
        """Verify output polling exposes running or terminal status and the eventual exit code."""
        running = self.start(f'{PY} -c "import sys, time; time.sleep(0.5); sys.exit(3)"', yield_time_ms=0)
        first = self.call("read_output", {"output_ref": running["command_id"]})
        self.assertEqual(first["structuredContent"]["status"], "running")
        self.assertIsNone(first["structuredContent"]["exit_code"])
        self.assertIn("[command running]", _text(first))
        self.wait_exit(running["command_id"])
        second = self.call("read_output", {"output_ref": running["command_id"]})
        self.assertEqual(second["structuredContent"]["status"], "exited")
        self.assertEqual(second["structuredContent"]["exit_code"], 3)
        self.assertEqual(second["structuredContent"]["operation_outcome"], "exited_nonzero")
        self.assertIn("[command exited | exit code 3]", _text(second))
        declared = output_schemas()["read_output"]
        self.assertIn("status", declared)
        self.assertIn("exit_code", declared)


class RetentionTests(_RuntimeCase):
    def test_ttl_counts_from_first_observation_not_from_exit(self) -> None:
        """Verify retained-output TTL begins with the first reported completion."""
        started = self.start(f'{PY} -c "print(1)"', yield_time_ms=0)
        command_id = started["command_id"]
        self.wait_exit(command_id)
        with self.runtime.commands_lock:
            command = self.runtime.commands.get(command_id) or self.runtime.output_commands[command_id]
        # Finished long ago but never reported to anyone: still retained.
        command.completed_at = time.time() - 10 * server_module.COMPLETED_COMMAND_TTL_SECONDS
        self.runtime._prune_commands()
        self.assertIsNone(command.observed_at)
        observed = self.call("read_output", {"output_ref": command_id})
        self.assertFalse(observed.get("isError"), observed)
        self.assertEqual(observed["structuredContent"]["status"], "exited")
        self.assertIsNotNone(command.observed_at)
        # Within the TTL of the observation it is still there.
        self.assertFalse(self.call("read_output", {"output_ref": command_id}).get("isError"))
        command.observed_at = time.time() - server_module.COMPLETED_COMMAND_TTL_SECONDS - 1
        gone = self.call("read_output", {"output_ref": command_id})
        self.assertTrue(gone["isError"])
        error = gone["structuredContent"]["error"]
        self.assertEqual(error["code"], "COMMAND_NOT_FOUND")
        hint = error["details"]["retry_hint"]
        self.assertIn("evicted", hint)
        self.assertIn("restart", hint)
        self.assertIn("first reported", hint)

    def test_every_terminal_response_marks_observation(self) -> None:
        """Verify execution, polling, and kill responses record the first terminal observation."""
        for tool in ("exec_command", "write_stdin", "kill_command"):
            with self.subTest(tool=tool):
                if tool == "exec_command":
                    payload = self.start("echo done", yield_time_ms=10_000)
                else:
                    payload = self.start(f'{PY} -c "print(1)"', yield_time_ms=0)
                    self.wait_exit(payload["command_id"])
                    self.call(tool, {"command_id": payload["command_id"]})
                with self.runtime.commands_lock:
                    command = self.runtime.output_commands[payload["command_id"]]
                self.assertIsNotNone(command.observed_at)

    def test_a_running_report_does_not_mark_observation(self) -> None:
        """Verify observing a running command does not start the completed-output TTL."""
        payload = self.start(SLEEPER, yield_time_ms=0)
        self.call("read_output", {"output_ref": payload["command_id"]})
        with self.runtime.commands_lock:
            command = self.runtime.commands[payload["command_id"]]
        self.assertIsNone(command.observed_at)


class KillCommandTests(_RuntimeCase):
    def test_kill_on_an_exited_command_sends_nothing_and_reports_the_real_exit(self) -> None:
        """Verify killing an exited command preserves its real outcome and sends no signal."""
        payload = self.start(f'{PY} -c "import sys; sys.exit(2)"', yield_time_ms=10_000)
        result = self.call("kill_command", {"command_id": payload["command_id"]})
        self.assertFalse(result.get("isError"), result)
        killed = result["structuredContent"]
        self.assertIsNone(killed["signal_sent"])
        self.assertFalse(killed["killed"])
        self.assertEqual(killed["status"], "exited")
        self.assertEqual(killed["exit_code"], 2)
        self.assertEqual(killed["operation_outcome"], "exited_nonzero")
        text = _text(result)
        self.assertNotIn("SIGTERM", text)
        self.assertIn("exit code 2", text)
        self.assertIn("no signal sent", text)

    def test_kill_of_a_running_command_reports_killed_outcome(self) -> None:
        """Verify intentional kills report a stable killed outcome in later output reads."""
        payload = self.start(SLEEPER, yield_time_ms=0)
        result = self.call("kill_command", {"command_id": payload["command_id"]})
        killed = result["structuredContent"]
        self.assertEqual(killed["signal_sent"], "SIGTERM")
        self.assertTrue(killed["killed"])
        self.assertEqual(killed["operation_outcome"], "killed")
        self.assertIn("killed", output_schemas()["kill_command"]["operation_outcome"]["enum"])
        # A later poll keeps reporting the same outcome.
        page = self.call("read_output", {"output_ref": payload["command_id"]})["structuredContent"]
        self.assertEqual(page["operation_outcome"], "killed")


class OutcomeTelemetryTests(unittest.TestCase):
    def _summaries(self, run: Any) -> dict[str, dict[str, object]]:
        """Run a command scenario with captured telemetry and return summaries keyed by tool."""
        sender = _CapturingSender()
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="on"), patch.object(
            telemetry, "_get_sender", return_value=sender
        ), tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            try:
                runtime.telemetry.record_request(LEGACY_PROTOCOL_VERSION, "tools/call")
                run(runtime)
            finally:
                runtime.close()
        return {
            str(_properties(event)["tool"]): _properties(event)
            for event in sender.events
            if event["event"] == "tool_summary"
        }

    def test_killed_is_not_a_failed_operation(self) -> None:
        """Verify intentional kills are attributed to execution without counting as failures."""
        self.assertNotIn("killed", FAILED_OPERATION_OUTCOMES)

        def run(runtime: Runtime) -> None:
            """Start a sleeper and assert that its requested termination reports a killed outcome."""
            started = runtime.call_tool("exec_command", {"cmd": SLEEPER, "yield_time_ms": 0})
            killed = runtime.call_tool("kill_command", {"command_id": started["structuredContent"]["command_id"]})
            self.assertEqual(killed["structuredContent"]["operation_outcome"], "killed")

        summaries = self._summaries(run)
        self.assertEqual(summaries["exec_command"]["operation_failures"], 0)
        self.assertEqual(summaries["exec_command"]["outcome_killed"], 1)
        self.assertEqual(summaries["exec_command"]["ok"], 1)
        self.assertNotIn("outcome_signal", summaries["exec_command"])
        self.assertNotIn("outcome_killed", summaries["kill_command"])

    def test_running_is_not_counted_so_outcomes_sum_to_calls(self) -> None:
        """Verify running polls add no terminal outcomes and completed counts match executions."""
        def run(runtime: Runtime) -> None:
            """Poll one command to completion and execute another to produce two terminal outcomes."""
            cmd = f'{PY} -c "import time; time.sleep(0.3)"'
            started = runtime.call_tool("exec_command", {"cmd": cmd, "yield_time_ms": 0})
            command_id = started["structuredContent"]["command_id"]
            for _ in range(40):
                polled = runtime.call_tool(
                    "write_stdin", {"command_id": command_id, "chars": "", "yield_time_ms": 250}
                )
                if polled["structuredContent"]["status"] != "running":
                    break
            runtime.call_tool("exec_command", {"cmd": "echo hi"})

        summaries = self._summaries(run)
        exec_summary = summaries["exec_command"]
        self.assertNotIn("outcome_running", exec_summary)
        self.assertEqual(exec_summary["outcome_exited_0"], 2)
        self.assertEqual(exec_summary["calls"], 2)
        self.assertNotIn("outcome_running", summaries["write_stdin"])


class _FakeProcess:
    pid = 4321

    def __init__(self, *, exits_after_tree_kill: bool = True) -> None:
        """Configure a fake process and its behavior after attempted tree termination."""
        self.calls: list[object] = []
        self.exits_after_tree_kill = exits_after_tree_kill

    def poll(self) -> int | None:
        """Report the fake process as running so termination logic attempts cleanup."""
        return None

    def send_signal(self, value: object) -> None:
        """Record a signal request without signaling a real process."""
        self.calls.append(("send_signal", value))

    def wait(self, timeout: float) -> int:
        """Record waits and optionally time out the first tree-kill wait to exercise fallback."""
        self.calls.append(("wait", timeout))
        if not self.exits_after_tree_kill and len([c for c in self.calls if isinstance(c, tuple)]) == 1:
            raise subprocess.TimeoutExpired("cmd", timeout)
        return 0

    def terminate(self) -> None:
        """Record direct-child termination without launching or stopping a process."""
        self.calls.append("terminate")

    def kill(self) -> None:
        """Record forced direct-child cleanup for later assertions."""
        self.calls.append("kill")


class WindowsTreeKillTests(unittest.TestCase):
    def _terminate(self, process: _FakeProcess, *, force: bool = False) -> list[list[str]]:
        """Simulate Windows tree cleanup and return the captured taskkill invocations."""
        runs: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            """Capture taskkill arguments, require a timeout, and simulate successful completion."""
            runs.append(argv)
            self.assertIn("timeout", kwargs)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        def no_killpg(value: object, name: str) -> bool:
            """Hide POSIX killpg support while preserving other attribute checks."""
            if value is processes_module.os and name == "killpg":
                return False
            return hasattr(value, name)

        with (
            patch.object(processes_module.os, "name", "nt"),
            patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}),
            patch.object(processes_module, "hasattr", side_effect=no_killpg, create=True),
            patch.object(processes_module.subprocess, "run", side_effect=fake_run),
        ):
            processes_module.terminate_process_group(  # type: ignore[arg-type]
                process, signal.SIGTERM, force=force
            )
        return runs

    def test_windows_kills_the_whole_tree_with_taskkill(self) -> None:
        """Verify both Windows cleanup modes invoke taskkill for the full process tree."""
        for force in (False, True):
            with self.subTest(force=force):
                process = _FakeProcess()
                runs = self._terminate(process, force=force)
                self.assertEqual(runs, [[r"C:\Windows\System32\taskkill.exe", "/T", "/F", "/PID", "4321"]])
                self.assertEqual(process.calls, [("wait", 1)])

    def test_windows_falls_back_to_the_direct_child_when_the_tree_kill_fails(self) -> None:
        """Verify a child still alive after tree cleanup receives direct termination."""
        process = _FakeProcess(exits_after_tree_kill=False)
        runs = self._terminate(process)
        self.assertEqual(len(runs), 1)
        self.assertIn("terminate", process.calls)

    def test_taskkill_failures_are_ignored(self) -> None:
        """Verify a missing taskkill executable does not escape the best-effort cleanup helper."""
        with (
            patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}),
            patch.object(processes_module.subprocess, "run", side_effect=FileNotFoundError("taskkill")) as run,
        ):
            processes_module._kill_windows_process_tree(1)
        run.assert_called_once()

    def test_taskkill_does_not_search_workspace_or_inherit_server_secrets(self) -> None:
        """Pin cleanup to the system utility even with a workspace PATH and secret env."""
        with (
            patch.dict(os.environ, {"SystemRoot": r"D:\Windows", "PATH": r"C:\repo", "TEST_API_TOKEN": "fake"}),
            patch.object(processes_module.subprocess, "run") as run,
        ):
            processes_module._kill_windows_process_tree(4321)
        self.assertEqual(run.call_args.args[0][0], r"D:\Windows\System32\taskkill.exe")
        self.assertEqual(run.call_args.kwargs["cwd"], r"D:\Windows\System32")
        self.assertEqual(run.call_args.kwargs["env"], {"SystemRoot": r"D:\Windows", "WINDIR": r"D:\Windows"})

    def test_taskkill_never_falls_back_to_search_when_system_root_is_unusable(self) -> None:
        """An absent or relative system directory leaves direct-child cleanup to the caller."""
        for root in ("", "Windows", r"C:Windows", r"\Windows"):
            with (
                self.subTest(root=root),
                patch.dict(os.environ, {"SystemRoot": root}),
                patch.object(processes_module.subprocess, "run") as run,
            ):
                processes_module._kill_windows_process_tree(4321)
                run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_posix_still_signals_the_process_group(self) -> None:
        """Verify POSIX cleanup terminates a process group without invoking taskkill."""
        with patch.object(processes_module.subprocess, "run") as run:
            process = subprocess.Popen(["sleep", "10"], start_new_session=True)
            try:
                processes_module.terminate_process_group(process, signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        run.assert_not_called()


class WindowsCommandPathTests(_RuntimeCase):
    def setUp(self) -> None:
        """Create a safe-mode runtime and an allowed relative path for policy checks."""
        super().setUp()
        self.runtime.close()
        self.runtime = Runtime(self.root, permission_mode="safe")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "ok.txt").write_text("ok\n", encoding="utf-8")

    def check(self, cmd: str) -> str | None:
        """Return the path-policy failure code, or None if the command is accepted."""
        try:
            self.runtime._check_command_paths(cmd)
        except server_module.ToolFailure as exc:
            return exc.code
        return None

    def test_windows_tokenizer_preserves_backslashes(self) -> None:
        """Verify Windows tokenization preserves path separators and POSIX escaping still works."""
        self.assertEqual(
            server_module.shlex_split(r"cat ..\..\secret.txt C:\Windows\win.ini", windows=True),
            ["cat", r"..\..\secret.txt", r"C:\Windows\win.ini"],
        )
        self.assertEqual(
            server_module.shlex_split(r'type "C:\Program Files\x.txt" > out.txt', windows=True),
            ["type", r"C:\Program Files\x.txt", ">", "out.txt"],
        )
        # POSIX tokenization is unchanged.
        self.assertEqual(server_module.shlex_split(r"echo a\ b", windows=False), ["echo", "a b"])

    def test_windows_backslash_and_drive_paths_cannot_escape_the_workspace(self) -> None:
        """Verify traversal, drive, and UNC paths are blocked while workspace paths remain valid."""
        with patch.object(server_module, "windows_shell_syntax", return_value=True):
            for cmd in (
                r"cat ..\..\secret.txt",
                r'cat "..\..\secret.txt"',
                r"cat C:\Windows\win.ini",
                r"cat c:\Windows\win.ini",
                r"cat \\server\share\secret.txt",
                r"cat C:secret.txt",
                r"cat sub\..\..\secret.txt",
                r"echo hi > ..\out.txt",
            ):
                with self.subTest(cmd=cmd):
                    self.assertEqual(self.check(cmd), "PERMISSION_REQUIRED")
            self.assertIsNone(self.check(r"cat sub\ok.txt"))
            self.assertIsNone(self.check(r"cat .\sub\ok.txt"))

    def test_posix_forward_slash_escape_is_still_blocked(self) -> None:
        """Verify POSIX traversal remains blocked and ordinary relative paths are accepted."""
        with patch.object(server_module, "windows_shell_syntax", return_value=False):
            self.assertEqual(self.check("cat ../../secret.txt"), "PERMISSION_REQUIRED")
            self.assertIsNone(self.check("cat sub/ok.txt"))


if __name__ == "__main__":
    unittest.main()
