"""Actual Windows Job acceptance, run separately with cmd and PowerShell 7.

Non-Windows hosts skip this module and provide no native evidence. Windows
hosts must support Jobs: missing native capability fails rather than skips.
The failure-injection test changes setup calls only; it launches real Windows
processes suspended and checks that those processes produce no side effects.
"""
from __future__ import annotations

import ctypes
import gc
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import processes, shells, windows_job
from coding_tools_mcp.errors import ToolFailure

_TREE_SCRIPT = """import os, pathlib, subprocess, sys, time
base = pathlib.Path(sys.argv[2])
if sys.argv[1] == 'leaf':
    (base / 'leaf.pid').write_text(str(os.getpid()))
    print('leaf-ready', flush=True)
    time.sleep(90)
else:
    leaf = subprocess.Popen([sys.executable, __file__, 'leaf', str(base)])
    while not (base / 'leaf.pid').exists():
        time.sleep(0.01)
    (base / 'child.pid').write_text(str(os.getpid()))
    print('tree-ready', flush=True)
    if sys.argv[1] != 'exit':
        time.sleep(90)
"""


@unittest.skipUnless(os.name == "nt", "Requires native Windows; mocks are not acceptance")
class NativeWindowsJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="coding-tools-job-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "中文 job workspace"
        self.workspace.mkdir()
        self.script = self.workspace / "tree probe.py"
        self.script.write_text(_TREE_SCRIPT, encoding="utf-8")
        self.selected = shells.selected_windows_command_shell(str(self.workspace), refresh=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.kernel.WaitForSingleObject.restype = ctypes.c_uint32
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int
        self.kernel.GetCurrentProcess.restype = ctypes.c_void_p
        self.kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.kernel.GetProcessHandleCount.restype = ctypes.c_int

    def quote(self, value: object) -> str:
        if self.selected.kind == "pwsh":
            return "'" + str(value).replace("'", "''") + "'"
        return '"' + str(value) + '"'

    def shell_argv(self, *args: object) -> list[str] | str:
        command = " ".join(self.quote(value) for value in args)
        if self.selected.kind == "pwsh":
            command = "& " + command
        return shells.shell_command(command, self.selected)

    def cleanup_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            processes.terminate_process_group(process, signal.SIGTERM)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            job = windows_job.get_windows_job(process)
            if job is not None and job.reaper is not None:
                job.reaper.join(timeout=5)

    def launch_tree(self, mode: str = "parent") -> subprocess.Popen[bytes]:
        process, fd = processes.spawn_process(
            self.shell_argv(sys.executable, self.script, mode, self.workspace),
            cwd=str(self.workspace), shell=False, env=dict(os.environ), tty=False, popen_kwargs={},
        )
        self.addCleanup(self.cleanup_process, process)
        self.assertIsNone(fd)
        self.assertIsNotNone(windows_job.get_windows_job(process))
        return process

    def wait_for_file(self, path: Path, *, timeout: float = 15) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = path.read_text()
            except FileNotFoundError:
                pass
            else:
                if text:
                    return text
            time.sleep(0.01)
        self.fail(f"Native subprocess did not create {path.name}")

    def open_descendants(self) -> list[int]:
        handles = []
        for name in ("child.pid", "leaf.pid"):
            pid = int(self.wait_for_file(self.workspace / name))
            # Synchronize-only handles retain exact process identity across
            # exit and PID reuse; no psutil or taskkill polling is involved.
            handle = self.kernel.OpenProcess(0x00100000, False, pid)
            self.assertTrue(handle, f"Native descendant {pid} disappeared before cancellation")
            self.addCleanup(self.kernel.CloseHandle, handle)
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 258)
            handles.append(handle)
        return handles

    def assert_processes_exit(self, handles: list[int]) -> None:
        for handle in handles:
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 5000), 0,
                             "A native descendant escaped Job cleanup")

    def test_cancel_ends_shell_child_and_grandchild(self) -> None:
        process = self.launch_tree()
        descendants = self.open_descendants()
        processes.terminate_process_group(process, signal.SIGTERM)
        self.assert_processes_exit(descendants)
        self.assertIsNotNone(process.poll())
        job = windows_job.get_windows_job(process)
        assert job is not None
        self.assertTrue(job.closed)

    def test_timeout_ends_all_descendants_and_drains_pipes(self) -> None:
        process = self.launch_tree()
        descendants = self.open_descendants()
        run = processes.CommandRun("native-timeout", process, timeout_at=time.time() + 0.2)
        processes.start_reader_threads(run)
        processes.start_command_watchdog(run)
        self.assert_processes_exit(descendants)
        process.wait(timeout=5)
        run.refresh_status()
        run.drain_readers(timeout=5)
        self.assertTrue(run.timed_out)
        self.assertTrue(all(not reader.is_alive() for reader in run.reader_threads))

    def test_closing_job_handle_kills_live_tree(self) -> None:
        process = self.launch_tree()
        descendants = self.open_descendants()
        windows_job.close_windows_job(process)
        self.assert_processes_exit(descendants)
        process.wait(timeout=5)

    def test_root_exit_reaps_orphan_before_later_cancellation(self) -> None:
        # Delay root exit until exact descendant handles have been opened.
        script = _TREE_SCRIPT.replace("if sys.argv[1] != 'exit':", "while not (base / 'exit-now').exists():\n        time.sleep(0.01)\n    if False:")
        # PowerShell waits for EOF from a native command's output pipeline.
        # Detach the leaf's streams so that shell can actually exit first;
        # inherited-pipe cleanup is exercised directly in the next test.
        script = script.replace("'leaf', str(base)])", "'leaf', str(base)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)")
        self.script.write_text(script, encoding="utf-8")
        process = self.launch_tree()
        descendants = self.open_descendants()
        (self.workspace / "exit-now").write_text("exit")
        self.assertEqual(process.wait(timeout=15), 0)
        self.assert_processes_exit(descendants)
        # Root is gone; cleanup still addresses its Job, never the old PID.
        with patch.object(processes, "_kill_windows_process_tree", side_effect=AssertionError("PID-based cleanup")):
            processes.terminate_process_group(process, signal.SIGTERM)
        stdout, _ = process.communicate(timeout=5)
        self.assertIn(b"tree-ready", stdout)

    def test_direct_parent_exit_does_not_leave_inherited_pipes_open(self) -> None:
        process = windows_job.spawn_windows_process(
            [sys.executable, str(self.script), "exit", str(self.workspace)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(self.cleanup_process, process)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn(b"tree-ready", stdout)
        job = windows_job.get_windows_job(process)
        assert job is not None and job.reaper is not None
        job.reaper.join(timeout=5)
        self.assertTrue(job.closed)

    def test_operating_system_rejects_descendant_breakaway(self) -> None:
        marker = self.workspace / "escaped.txt"
        escaped_code = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('escaped')"
        code = (
            "import pathlib, subprocess, sys\n"
            f"code = {escaped_code!r}\n"
            "try:\n"
            "    subprocess.Popen([sys.executable, '-c', code], creationflags=0x01000000).wait(timeout=10)\n"
            "except OSError as exc:\n"
            "    assert exc.winerror == 5, exc\n"
            "    print('breakaway-blocked')\n"
            "else:\n"
            "    raise AssertionError('CREATE_BREAKAWAY_FROM_JOB succeeded')\n"
        )
        probe = self.workspace / "breakaway.py"
        probe.write_text(code, encoding="utf-8")
        process = windows_job.spawn_windows_process(self.shell_argv(sys.executable, probe), cwd=str(self.workspace),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup_process, process)
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn(b"breakaway-blocked", stdout)
        self.assertFalse(marker.exists())

    def test_setup_failures_do_not_run_the_real_suspended_command(self) -> None:
        marker = self.workspace / "must-not-exist.txt"
        command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"]
        for method in ("create_job", "configure_job", "assign_process", "open_primary_thread", "resume_thread"):
            with self.subTest(method=method), patch.object(windows_job._WindowsAPI, method, side_effect=OSError("injected native setup failure")):
                with self.assertRaises(ToolFailure) as error:
                    windows_job.spawn_windows_process(command, stdin=subprocess.DEVNULL,
                                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(error.exception.code, "WINDOWS_JOB_UNAVAILABLE")
            self.assertFalse(marker.exists(), f"{method} failure allowed untrusted code to run")

    def test_native_text_encoding_contract_is_preserved(self) -> None:
        process = windows_job.spawn_windows_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write('中文'.encode('utf-8'))"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict",
        )
        self.addCleanup(self.cleanup_process, process)
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "中文")

    def test_repeated_launches_release_job_thread_and_pipe_handles(self) -> None:
        def run_once() -> None:
            process = windows_job.spawn_windows_process([sys.executable, "-c", "pass"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0)
            job = windows_job.get_windows_job(process)
            assert job is not None and job.reaper is not None
            job.reaper.join(timeout=5)
            self.assertFalse(job.reaper.is_alive())
            self.assertTrue(job.closed)

        def count() -> int:
            value = ctypes.c_uint32()
            self.assertTrue(self.kernel.GetProcessHandleCount(self.kernel.GetCurrentProcess(), ctypes.byref(value)))
            return value.value

        run_once()
        gc.collect()
        before = count()
        for _ in range(20):
            run_once()
        gc.collect()
        self.assertLessEqual(count(), before + 2, "Native launch leaked one or more handles per command")

    def test_service_exit_closes_job_without_python_finalizers(self) -> None:
        # A separate server-like process owns the Job and exits with os._exit.
        # Windows must close its handle and kill the full live command tree.
        owner_code = (
            "import os, pathlib, subprocess, sys, time\n"
            "from coding_tools_mcp.windows_job import spawn_windows_process\n"
            f"base = pathlib.Path({str(self.workspace)!r})\n"
            f"p = spawn_windows_process({[sys.executable, str(self.script), 'parent', str(self.workspace)]!r}, "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "while not (base / 'owner-exit').exists(): time.sleep(0.01)\n"
            "os._exit(0)\n"
        )
        owner = subprocess.Popen([sys.executable, "-c", owner_code],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup_owner() -> None:
            if owner.poll() is None:
                owner.kill()
            owner.communicate(timeout=5)
        self.addCleanup(cleanup_owner)
        descendants = self.open_descendants()
        (self.workspace / "owner-exit").write_text("exit")
        _, stderr = owner.communicate(timeout=10)
        self.assertEqual(owner.returncode, 0, stderr)
        self.assert_processes_exit(descendants)


if __name__ == "__main__":
    unittest.main()
