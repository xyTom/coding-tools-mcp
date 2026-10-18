"""Unit tests only. Mocked Win32 calls are not native acceptance evidence."""
from __future__ import annotations

import ctypes
import io
import signal
import unittest
from contextlib import ExitStack
from typing import Any
from unittest.mock import Mock, patch

from coding_tools_mcp import processes, windows_job
from coding_tools_mcp.errors import ToolFailure


class _FakeAPI(windows_job._WindowsAPI):
    def __init__(self, events: list[str], fail: str | None = None) -> None:
        self.events, self.fail = events, fail
        self.closed: list[int] = []

    def event(self, name: str) -> None:
        self.events.append(name)
        if name == self.fail:
            raise OSError(f"simulated {name} failure")

    def create_job(self) -> int:
        self.event("create_job")
        return 100

    def configure_job(self, handle: int) -> None:
        assert handle == 100
        self.event("configure_job")

    def assign_process(self, job: int, process: int) -> None:
        assert (job, process) == (100, 200)
        self.event("assign_process")

    def open_primary_thread(self, pid: int) -> int:
        assert pid == 42
        self.event("open_primary_thread")
        return 300

    def resume_thread(self, handle: int) -> None:
        assert handle == 300
        self.event("resume_thread")

    def close_handle(self, handle: int) -> None:
        self.event(f"close_{handle}")
        self.closed.append(handle)

    def terminate_job(self, handle: int) -> None:
        assert handle == 100
        self.event("terminate_job")


class _FakeProcess:
    pid, _handle = 42, 200

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.returncode: int | None = None
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.events.append("kill")
        self.returncode = 1

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait")
        return self.returncode or 0


class WindowsJobUnitTests(unittest.TestCase):
    def launch(self, *, fail: str | None = None, **kwargs: object) -> tuple[Any, _FakeAPI, list[str], Mock]:
        events: list[str] = []
        api = _FakeAPI(events, fail)
        process = _FakeProcess(events)

        def popen(*args: object, **options: object) -> _FakeProcess:
            events.append("popen")
            if fail == "popen":
                raise FileNotFoundError("missing executable")
            return process

        with ExitStack() as stack:
            stack.enter_context(patch.object(windows_job, "_WindowsAPI", return_value=api))
            launch = stack.enter_context(patch.object(windows_job.subprocess, "Popen", side_effect=popen))
            stack.enter_context(patch.object(windows_job.WindowsJob, "watch", side_effect=lambda _: api.event("watch")))
            result = windows_job.spawn_windows_process(["test.exe"], **kwargs)
        return result, api, events, launch

    def test_setup_and_assignment_precede_resume(self) -> None:
        process, api, events, launch = self.launch()
        self.assertEqual(events, ["create_job", "configure_job", "popen", "assign_process",
                                  "open_primary_thread", "watch", "resume_thread", "close_300"])
        self.assertEqual(launch.call_args.kwargs["creationflags"], windows_job.CREATE_SUSPENDED)
        self.assertTrue(launch.call_args.kwargs["close_fds"])
        windows_job.close_windows_job(process)
        self.assertEqual(api.closed, [300, 100])

    def test_byte_text_encoding_and_stdio_options_are_preserved(self) -> None:
        process, _, _, launch = self.launch(text=True, encoding="utf-8", errors="replace", stdout=-1,
                                           creationflags=0x200, close_fds=False)
        for key, value in {"text": True, "encoding": "utf-8", "errors": "replace", "stdout": -1,
                           "creationflags": 0x204, "close_fds": True}.items():
            self.assertEqual(launch.call_args.kwargs[key], value)
        windows_job.close_windows_job(process)

    def test_breakaway_request_is_rejected_before_native_calls(self) -> None:
        with patch.object(windows_job, "_WindowsAPI") as api, self.assertRaises(ToolFailure) as error:
            windows_job.spawn_windows_process(["never.exe"], creationflags=windows_job.CREATE_BREAKAWAY_FROM_JOB)
        api.assert_not_called()
        self.assertEqual(error.exception.details["stage"], "creation_flags")

    def test_api_failures_never_resume_a_child_or_fallback(self) -> None:
        for failure in ("create_job", "configure_job", "assign_process", "open_primary_thread", "watch", "resume_thread"):
            with self.subTest(failure=failure):
                events: list[str] = []
                api = _FakeAPI(events, failure)
                process = _FakeProcess(events)
                with patch.object(windows_job, "_WindowsAPI", return_value=api), \
                     patch.object(windows_job.subprocess, "Popen", return_value=process) as popen, \
                     patch.object(windows_job.WindowsJob, "watch", side_effect=lambda _: api.event("watch")), \
                     self.assertRaises(ToolFailure) as error:
                    windows_job.spawn_windows_process(["side-effect.exe"])
                self.assertEqual(error.exception.code, "WINDOWS_JOB_UNAVAILABLE")
                self.assertIn("No unowned fallback", error.exception.details["retry_hint"])
                self.assertLessEqual(popen.call_count, 1)
                if failure != "resume_thread":
                    self.assertNotIn("resume_thread", events)
                if popen.called:
                    self.assertIn("kill", events)
                    self.assertTrue(process.stdin.closed and process.stdout.closed and process.stderr.closed)
                if failure != "create_job":
                    self.assertEqual(api.closed.count(100), 1)
                if failure in {"watch", "resume_thread"}:
                    self.assertEqual(api.closed.count(300), 1)

    def test_popen_failure_preserves_existing_spawn_error_contract(self) -> None:
        events: list[str] = []
        api = _FakeAPI(events)
        with patch.object(windows_job, "_WindowsAPI", return_value=api), \
             patch.object(windows_job.subprocess, "Popen", side_effect=FileNotFoundError("missing")), \
             self.assertRaises(FileNotFoundError):
            windows_job.spawn_windows_process(["missing"])
        self.assertEqual(api.closed, [100])

    def test_close_and_terminate_are_idempotent(self) -> None:
        api = _FakeAPI([])
        job = windows_job.WindowsJob(api, 100)
        job.terminate()
        job.close()
        job.terminate()
        self.assertTrue(job.closed)
        self.assertEqual(api.events, ["terminate_job", "close_100"])

    def test_close_still_kills_members_when_terminate_api_fails(self) -> None:
        api = _FakeAPI([], "terminate_job")
        job = windows_job.WindowsJob(api, 100)
        job.terminate()
        self.assertTrue(job.closed)
        self.assertEqual(api.closed, [100])

    def test_failed_close_keeps_ownership_for_retry(self) -> None:
        api = _FakeAPI([], "close_100")
        job = windows_job.WindowsJob(api, 100)
        with self.assertRaises(OSError):
            job.close()
        self.assertFalse(job.closed)
        api.fail = None
        job.close()
        self.assertTrue(job.closed)

    def test_cancel_uses_job_even_when_root_already_exited(self) -> None:
        process, api, _, _ = self.launch()
        process.returncode = 0
        with patch.object(processes.os, "name", "nt"), patch.object(processes, "_kill_windows_process_tree") as taskkill:
            processes.terminate_process_group(process, signal.SIGTERM)
        taskkill.assert_not_called()
        self.assertEqual(api.events[-3:], ["terminate_job", "close_100", "wait"])

    def test_refresh_releases_job_and_preserves_execution_metadata(self) -> None:
        process, api, _, _ = self.launch()
        process.returncode = 0
        run = processes.CommandRun("owned", process, execution_isolation={"backend": "test"})
        result = run.snapshot_since_cursor(1024)
        self.assertTrue(run.closed)
        self.assertEqual(api.closed, [300, 100])
        self.assertEqual(result["execution_isolation"], {"backend": "test"})
        self.assertIsNot(result["execution_isolation"], run.execution_isolation)

    def test_spawn_process_routes_windows_pipe_launch_through_job(self) -> None:
        with patch.object(processes.os, "name", "nt"), patch.object(processes, "spawn_windows_process") as spawn:
            result, fd = processes.spawn_process("cmd", cwd="cwd", shell=False, env={}, tty=False,
                                                popen_kwargs={"creationflags": 0x200}, stdio={"stdin": -3})
        self.assertIs(result, spawn.return_value)
        self.assertIsNone(fd)
        self.assertEqual(spawn.call_args.kwargs["stdin"], -3)


class WindowsJobWin32BindingUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = windows_job._WindowsAPI.__new__(windows_job._WindowsAPI)
        self.api.kernel32 = Mock()

    def test_only_kill_on_close_limit_is_set(self) -> None:
        self.api.configure_job(100)
        args = self.api.kernel32.SetInformationJobObject.call_args.args
        limits = ctypes.cast(args[2], ctypes.POINTER(windows_job._ExtendedLimits)).contents
        self.assertEqual(args[1], 9)
        self.assertEqual(limits.BasicLimitInformation.LimitFlags, 0x2000)
        self.assertEqual(limits.BasicLimitInformation.ActiveProcessLimit, 0)

    def enumerate_threads(self, entries: list[tuple[int, int]], *, owner: int = 42, error: int = 18) -> int:
        rows = iter(entries)
        def next_entry(handle: int, pointer: Any) -> bool:
            try:
                pid, tid = next(rows)
            except StopIteration:
                return False
            entry = ctypes.cast(pointer, ctypes.POINTER(windows_job._ThreadEntry)).contents
            entry.th32OwnerProcessID, entry.th32ThreadID = pid, tid
            return True
        self.api.kernel32.CreateToolhelp32Snapshot.return_value = 400
        self.api.kernel32.Thread32First.side_effect = next_entry
        self.api.kernel32.Thread32Next.side_effect = next_entry
        self.api.kernel32.OpenThread.return_value = 300
        self.api.kernel32.GetProcessIdOfThread.return_value = owner
        with patch.object(windows_job.ctypes, "get_last_error", return_value=error, create=True):
            return self.api.open_primary_thread(42)

    def test_reopens_verified_single_primary_thread_with_minimal_rights(self) -> None:
        self.assertEqual(self.enumerate_threads([(1, 10), (42, 20), (99, 30)]), 300)
        self.api.kernel32.CloseHandle.assert_called_once_with(400)
        self.api.kernel32.OpenThread.assert_called_once_with(0x802, False, 20)

    def test_zero_or_multiple_threads_fail_without_guessing(self) -> None:
        for rows in ([], [(42, 20), (42, 21)]):
            with self.subTest(rows=rows), self.assertRaises(OSError):
                self.enumerate_threads(rows)
        self.api.kernel32.OpenThread.assert_not_called()
        self.assertEqual(self.api.kernel32.CloseHandle.call_count, 2)

    def test_thread_owner_mismatch_closes_snapshot_and_thread(self) -> None:
        with self.assertRaises(OSError):
            self.enumerate_threads([(42, 20)], owner=99)
        self.assertEqual([call.args[0] for call in self.api.kernel32.CloseHandle.call_args_list], [400, 300])

    def test_resume_requires_exactly_one_suspension(self) -> None:
        self.api.kernel32.ResumeThread.return_value = 1
        self.api.resume_thread(300)
        for count in (0, 2):
            self.api.kernel32.ResumeThread.return_value = count
            with self.subTest(count=count), self.assertRaises(OSError):
                self.api.resume_thread(300)


if __name__ == "__main__":
    unittest.main()
