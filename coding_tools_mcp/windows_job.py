"""Native Windows command lifetime, not a filesystem or network sandbox.

Create the process suspended, attach a non-breakaway, kill-on-close Job, and
only then resume its primary thread. Python closes CreateProcess's thread
handle, so we reopen the single suspended thread using documented Toolhelp
APIs. Failure to establish ownership never falls back to an ordinary launch.

The root process bounds the command lifetime: its exit closes the Job and
ends remaining descendants, including ones holding inherited output pipes.
This covers descendants created normally with CreateProcess, not work sent
to independent services (WMI, scheduled tasks, etc.). A Job is not a security
boundary against another process running with the same user privileges.

Win32 contracts:
https://learn.microsoft.com/windows/win32/procthread/job-objects
https://learn.microsoft.com/windows/win32/api/tlhelp32/nf-tlhelp32-thread32first
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
from typing import Any

from .errors import ToolFailure

CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_THREAD_QUERY_LIMITED_INFORMATION = 0x0800
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_JOB_ATTRIBUTE = "_coding_tools_windows_job"
_LOG = logging.getLogger(__name__)


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


class _WindowsAPI:
    """Small, typed Win32 surface; loaded only on native Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects require native Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
            "SetInformationJobObject": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            "AssignProcessToJobObject": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
            "TerminateJobObject": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            "CloseHandle": ([ctypes.c_void_p], ctypes.c_int),
            "CreateToolhelp32Snapshot": ([ctypes.c_uint32, ctypes.c_uint32], ctypes.c_void_p),
            "Thread32First": ([ctypes.c_void_p, ctypes.POINTER(_ThreadEntry)], ctypes.c_int),
            "Thread32Next": ([ctypes.c_void_p, ctypes.POINTER(_ThreadEntry)], ctypes.c_int),
            "OpenThread": ([ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
            "GetProcessIdOfThread": ([ctypes.c_void_p], ctypes.c_uint32),
            "ResumeThread": ([ctypes.c_void_p], ctypes.c_uint32),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel32, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def _error(operation: str) -> OSError:
        error = ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        error.strerror = f"{operation}: {error.strerror}"
        return error

    def create_job(self) -> int:
        # NULL security attributes: unnamed, non-inheritable handle.
        handle = self.kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise self._error("CreateJobObjectW")
        return int(handle)

    def configure_job(self, handle: int) -> None:
        limits = _ExtendedLimits()
        # Neither BREAKAWAY_OK nor SILENT_BREAKAWAY_OK is enabled.
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise self._error("SetInformationJobObject")

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        if not self.kernel32.AssignProcessToJobObject(job_handle, process_handle):
            raise self._error("AssignProcessToJobObject")

    def open_primary_thread(self, pid: int) -> int:
        snapshot = self.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot == _INVALID_HANDLE_VALUE or not snapshot:
            raise self._error("CreateToolhelp32Snapshot")
        candidates: list[int] = []
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = self.kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.dwSize < _ThreadEntry.th32OwnerProcessID.offset + ctypes.sizeof(ctypes.c_uint32):
                    raise OSError("Toolhelp did not return the thread owner")
                if entry.th32OwnerProcessID == pid:
                    candidates.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                found = self.kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != _ERROR_NO_MORE_FILES:  # type: ignore[attr-defined]
                raise self._error("Thread32First/Thread32Next")
        finally:
            self.close_handle(int(snapshot))
        # Do not guess which thread to resume or use undocumented NtResumeProcess.
        if len(candidates) != 1:
            raise OSError(f"Expected one suspended primary thread; found {len(candidates)}")
        handle = self.kernel32.OpenThread(
            _THREAD_SUSPEND_RESUME | _THREAD_QUERY_LIMITED_INFORMATION, False, candidates[0]
        )
        if not handle:
            raise self._error("OpenThread")
        if self.kernel32.GetProcessIdOfThread(handle) != pid:
            self.close_handle(int(handle))
            raise OSError("Suspended primary thread ownership changed")
        return int(handle)

    def resume_thread(self, handle: int) -> None:
        count = self.kernel32.ResumeThread(handle)
        if count == 0xFFFFFFFF:
            raise self._error("ResumeThread")
        if count != 1:
            raise OSError(f"Unexpected primary-thread suspend count: {count}")

    def terminate_job(self, handle: int) -> None:
        if not self.kernel32.TerminateJobObject(handle, 1):
            raise self._error("TerminateJobObject")

    def close_handle(self, handle: int) -> None:
        if not self.kernel32.CloseHandle(handle):
            raise self._error("CloseHandle")


class WindowsJob:
    """Own exactly one Job handle, shared safely by cancellation and reaping."""

    def __init__(self, api: _WindowsAPI, handle: int) -> None:
        self.api = api
        self._handle: int | None = handle
        self._lock = threading.Lock()
        self.reaper: threading.Thread | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._handle is None

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self.api.close_handle(self._handle)
                self._handle = None

    def terminate(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            try:
                self.api.terminate_job(self._handle)
            except OSError:
                # KILL_ON_JOB_CLOSE is also a guaranteed cleanup path if the
                # explicit termination call fails.
                pass
            # Keep the handle for retry if CloseHandle itself fails.
            self.api.close_handle(self._handle)
            self._handle = None

    def watch(self, process: subprocess.Popen[Any]) -> None:
        def reap() -> None:
            try:
                process.wait()
            finally:
                try:
                    self.close()
                except OSError:
                    _LOG.exception("Could not close the Windows command Job")

        self.reaper = threading.Thread(target=reap, name=f"coding-tools-job-{process.pid}", daemon=True)
        # Start before ResumeThread. If thread creation fails, the command
        # is still suspended and the caller tears it down without executing.
        self.reaper.start()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def get_windows_job(process: subprocess.Popen[Any]) -> WindowsJob | None:
    job = getattr(process, _JOB_ATTRIBUTE, None)
    return job if isinstance(job, WindowsJob) else None


def close_windows_job(process: subprocess.Popen[Any]) -> None:
    job = get_windows_job(process)
    if job is not None:
        job.close()


def spawn_windows_process(command: Any, **kwargs: Any) -> subprocess.Popen[Any]:
    """Popen-compatible launch with fail-closed, pre-execution Job ownership.

    Text/encoding and stdio options are passed unchanged to Popen. All handles
    except Popen's explicit stdio handle list are closed across the launch.
    """
    flags = kwargs.get("creationflags", 0)
    if flags & CREATE_BREAKAWAY_FROM_JOB:
        raise ToolFailure("WINDOWS_JOB_UNAVAILABLE", "Windows commands cannot request Job breakaway.",
                          category="security", details={"stage": "creation_flags", "platform": "win32"})
    kwargs["creationflags"] = flags | CREATE_SUSPENDED
    kwargs["close_fds"] = True
    job: WindowsJob | None = None
    process: subprocess.Popen[Any] | None = None
    thread_handle: int | None = None
    stage = "create_job"
    try:
        api = _WindowsAPI()
        job = WindowsJob(api, api.create_job())
        stage = "configure_job"
        api.configure_job(job._handle)  # type: ignore[arg-type]
        stage = "create_suspended_process"
        process = subprocess.Popen(command, **kwargs)
        stage = "assign_process"
        api.assign_process(job._handle, int(process._handle))  # type: ignore[arg-type, attr-defined]
        setattr(process, _JOB_ATTRIBUTE, job)
        stage = "open_primary_thread"
        thread_handle = api.open_primary_thread(process.pid)
        stage = "start_lifetime_watcher"
        job.watch(process)
        stage = "resume_thread"
        api.resume_thread(thread_handle)
    except BaseException as exc:
        if process is not None:
            # Assignment may have failed, leaving the suspended root outside
            # our Job. Always stop that root as well as any owned members.
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                _LOG.exception("Could not reap failed suspended Windows launch")
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        _LOG.exception("Could not close failed Windows launch pipe")
        if job is not None:
            try:
                job.close()
            except OSError:
                _LOG.exception("Could not close failed Windows launch Job")
        if not isinstance(exc, Exception) or stage == "create_suspended_process":
            raise
        raise ToolFailure(
            "WINDOWS_JOB_UNAVAILABLE",
            "Windows process-tree setup failed; no unowned fallback was started.",
            category="security",
            details={
                "platform": "win32", "stage": stage, "reason": str(exc),
                "winerror": getattr(exc, "winerror", None),
                "retry_hint": "Use a Windows host that permits nested Job Objects, or adjust the service host's Job restrictions. No unowned fallback is allowed.",
            },
        ) from exc
    finally:
        if thread_handle is not None:
            try:
                api.close_handle(thread_handle)
            except OSError:
                # Ownership has already been installed before resumption.
                # Do not misreport a successful launch as one with no effects.
                _LOG.exception("Could not close Windows primary-thread handle")
    return process
