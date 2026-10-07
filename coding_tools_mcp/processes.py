from __future__ import annotations

import ntpath
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from .errors import ToolFailure
from .textutils import DEFAULT_MAX_LINES, TextTruncation, truncate_text_tail
from .windows_job import close_windows_job, get_windows_job, spawn_windows_process


COMMAND_BUFFER_BYTES = 524_288
# The operation-level result of running a command, kept separate from the
# transport-level "the tool call executed". A build that exits 1 is a
# successful tool call and a failed operation; counting it as a success is how
# telemetry came to report failed builds as wins.
# `killed` is a command a client deliberately stopped with kill_command: the
# stop was the requested operation, so it is not a failure.
COMMAND_OUTCOMES = ("exited_0", "exited_nonzero", "timeout", "signal", "spawn_error", "running", "killed")
# Fraction of the per-stream budget frozen as the head segment. The head keeps
# the earliest output (command echo, first error) that a tail-only rolling
# buffer would lose first, mirroring the head+tail retention used by other
# agent runtimes.
COMMAND_HEAD_BUFFER_DIVISOR = 8
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
WINDOWS_TREE_KILL_TIMEOUT_SECONDS = 5


def _kill_windows_process_tree(pid: int) -> None:
    """Kill a Windows process and every descendant.

    Windows has no process groups to signal: terminating the shell leaves its
    children running and holding the output pipes open. ``taskkill /T`` walks
    the parent/child tree. Failures are ignored; the caller falls back to
    terminating the direct child.
    """

    try:
        # SystemRoot belongs to the server environment, not exec_command's
        # caller-supplied env. Never search the workspace or PATH for cleanup.
        system_root = os.environ.get("SystemRoot", "")
        if not ntpath.isabs(system_root) or not ntpath.splitdrive(system_root)[0]:
            return
        system_dir = ntpath.join(system_root, "System32")
        subprocess.run(
            [ntpath.join(system_dir, "taskkill.exe"), "/T", "/F", "/PID", str(pid)],
            cwd=system_dir,
            env={"SystemRoot": system_root, "WINDIR": system_root},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=WINDOWS_TREE_KILL_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        pass


def terminate_process_group(
    process: subprocess.Popen[bytes],
    signum: signal.Signals,
    *,
    force: bool = False,
) -> None:
    """Stop a process tree or group, escalating or falling back to direct-child cleanup."""
    if os.name == "nt":
        job = get_windows_job(process)
        if job is not None:
            # The root may already have exited while descendants still own
            # its pipes. Job ownership, unlike taskkill /T, survives that exit.
            job.terminate()
            process.wait(timeout=5)
            return
        # CTRL_BREAK needs a console shared with the child, which a stdio
        # server usually lacks, and process.terminate() only ends cmd.exe.
        # Kill the whole tree first, then make sure the direct child is gone.
        if process.poll() is None:
            _kill_windows_process_tree(process.pid)
        try:
            process.wait(timeout=1)
            return
        except Exception:
            pass
    if not hasattr(os, "killpg"):
        try:
            if force:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=1)
        except Exception:
            process.kill()
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, HARD_KILL_SIGNAL)
        except Exception:
            process.kill()


def spawn_process(
    command: Any,
    *,
    cwd: str,
    shell: bool,
    env: dict[str, str],
    tty: bool,
    popen_kwargs: dict[str, Any],
    stdio: dict[str, Any] | None = None,
) -> tuple[subprocess.Popen[Any], int | None]:
    """Spawn a pipe-backed or true POSIX PTY-backed process."""

    if not tty:
        streams = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, **(stdio or {})}
        launch = spawn_windows_process if os.name == "nt" else subprocess.Popen
        process = launch(
            command,
            cwd=cwd,
            shell=shell,
            **streams,
            env=env,
            **popen_kwargs,
        )
        return process, None
    if os.name == "nt":
        raise ToolFailure(
            "TTY_UNSUPPORTED",
            "tty=true requires ConPTY support, which is not available in this build.",
            category="runtime",
            details={"platform": os.name, "retry_hint": "Run the command without tty=true."},
        )
    try:
        import pty

        master_fd, slave_fd = pty.openpty()
    except (ImportError, OSError) as exc:
        raise ToolFailure(
            "TTY_UNSUPPORTED",
            "A POSIX pseudo-terminal could not be created.",
            category="runtime",
        ) from exc
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            **popen_kwargs,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return process, master_fd


@dataclass
class CommandRun:
    command_id: str
    process: subprocess.Popen[bytes]
    timeout_at: float | None = None
    warnings: list[str] = field(default_factory=list)
    # Facts about this command's launch, retained with its output so any
    # terminal observer can decide whether the process could have changed the
    # workspace. These describe the command that actually ran, not the
    # server's advertised Landlock capability.
    landlock_confined: bool = False
    execution_isolation: dict[str, Any] | None = None
    workspace_may_write: bool = True
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_head: bytearray = field(default_factory=bytearray)
    stderr_head: bytearray = field(default_factory=bytearray)
    stdout_start_offset: int = 0
    stderr_start_offset: int = 0
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    buffer_limit: int = COMMAND_BUFFER_BYTES
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader_threads: list[threading.Thread] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    closed: bool = False
    exit_code: int | None = None
    signal_name: str | None = None
    timed_out: bool = False
    terminating: bool = False
    pty_master_fd: int | None = None
    on_evict: Any = None
    # Set by kill_command before it signals a live process, so the terminal
    # outcome is attributed to the deliberate stop rather than a failure.
    killed_by_client: bool = False
    # When a client response first reported this command's terminal state.
    # Retention TTL counts from here, not from process exit, so a command that
    # finished unobserved is never expired before anyone saw its result.
    observed_at: float | None = None
    _stdin_closed: bool = False

    @property
    def stdin_closed(self) -> bool:
        """Return whether piped stdin is unavailable; an attached PTY remains writable."""
        return self.pty_master_fd is None and (
            self._stdin_closed or self.process.stdin is None or self.process.stdin.closed
        )

    @property
    def head_buffer_limit(self) -> int:
        return self.buffer_limit // COMMAND_HEAD_BUFFER_DIVISOR

    @property
    def retained_bytes(self) -> int:
        with self.lock:
            stdout_head_unique = min(len(self.stdout_head), self.stdout_start_offset)
            stderr_head_unique = min(len(self.stderr_head), self.stderr_start_offset)
            return len(self.stdout) + len(self.stderr) + stdout_head_unique + stderr_head_unique

    def append_stdout(self, chunk: bytes) -> None:
        with self.lock:
            head_capacity = self.head_buffer_limit - len(self.stdout_head)
            if head_capacity > 0:
                self.stdout_head.extend(chunk[:head_capacity])
            self.stdout.extend(chunk)
            self.stdout_total_bytes += len(chunk)
            previous_start = self.stdout_start_offset
            dropped = _trim_buffer(
                self.stdout,
                total_bytes=self.stdout_total_bytes,
                start_offset_attr="stdout_start_offset",
                command=self,
            )
            self.stdout_dropped_bytes += dropped
            if dropped:
                self._report_eviction("stdout", previous_start, self.stdout_start_offset, len(self.stdout_head))

    def append_stderr(self, chunk: bytes) -> None:
        with self.lock:
            head_capacity = self.head_buffer_limit - len(self.stderr_head)
            if head_capacity > 0:
                self.stderr_head.extend(chunk[:head_capacity])
            self.stderr.extend(chunk)
            self.stderr_total_bytes += len(chunk)
            previous_start = self.stderr_start_offset
            dropped = _trim_buffer(
                self.stderr,
                total_bytes=self.stderr_total_bytes,
                start_offset_attr="stderr_start_offset",
                command=self,
            )
            self.stderr_dropped_bytes += dropped
            if dropped:
                self._report_eviction("stderr", previous_start, self.stderr_start_offset, len(self.stderr_head))

    def _report_eviction(self, stream: str, previous_start: int, new_start: int, head_len: int) -> None:
        if self.on_evict is None:
            return
        lost_bytes = max(0, new_start - max(previous_start, head_len))
        if lost_bytes:
            self.on_evict(stream, lost_bytes)

    def write_input(self, data: bytes) -> None:
        if self._stdin_closed:
            raise ToolFailure("COMMAND_CLOSED", "Command stdin is closed.", category="runtime")
        try:
            if self.pty_master_fd is not None:
                os.write(self.pty_master_fd, data)
                return
            if self.process.stdin is None or self.process.stdin.closed:
                raise ToolFailure("COMMAND_CLOSED", "Command stdin is closed.", category="runtime")
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ToolFailure("COMMAND_CLOSED", "Command stdin is closed.", category="runtime") from exc

    def close_stdin(self) -> None:
        if self.pty_master_fd is not None or self._stdin_closed:
            return
        self._stdin_closed = True
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def snapshot_since_cursor(self, max_output_bytes: int) -> dict[str, Any]:
        """Advance output cursors and return bounded stream tails with status and loss metadata."""
        self.refresh_status()
        with self.lock:
            stdout_omitted = max(0, self.stdout_start_offset - self.stdout_cursor)
            stderr_omitted = max(0, self.stderr_start_offset - self.stderr_cursor)
            stdout_start = max(0, self.stdout_cursor - self.stdout_start_offset)
            stderr_start = max(0, self.stderr_cursor - self.stderr_start_offset)
            stdout_bytes = bytes(self.stdout[stdout_start:])
            stderr_bytes = bytes(self.stderr[stderr_start:])
            self.stdout_cursor = self.stdout_total_bytes
            self.stderr_cursor = self.stderr_total_bytes
        stdout_truncation = truncate_output_bytes_tail(stdout_bytes, max_output_bytes)
        stderr_truncation = truncate_output_bytes_tail(stderr_bytes, max_output_bytes)
        status = self.status()
        payload: dict[str, Any] = {
            "command_id": self.command_id,
            "status": status,
            "exit_code": self.exit_code,
            "signal": self.signal_name,
            "timed_out": self.timed_out,
            "stdout": stdout_truncation.content,
            "stderr": stderr_truncation.content,
            "stdout_truncated": stdout_truncation.truncated,
            "stderr_truncated": stderr_truncation.truncated,
            "stdout_truncated_by": stdout_truncation.truncated_by,
            "stderr_truncated_by": stderr_truncation.truncated_by,
            "stdout_output_lines": stdout_truncation.output_lines,
            "stderr_output_lines": stderr_truncation.output_lines,
            "stdout_output_bytes": stdout_truncation.output_bytes,
            "stderr_output_bytes": stderr_truncation.output_bytes,
            "stdout_dropped_bytes": self.stdout_dropped_bytes,
            "stderr_dropped_bytes": self.stderr_dropped_bytes,
            "stdout_omitted_bytes": stdout_omitted,
            "stderr_omitted_bytes": stderr_omitted,
            "truncated": (
                stdout_truncation.truncated
                or stderr_truncation.truncated
                or stdout_omitted > 0
                or stderr_omitted > 0
            ),
            # The tool call itself executed; whether the command succeeded is
            # `operation_outcome`, and only that is what telemetry counts.
            "ok": True,
            "operation_outcome": self.operation_outcome(status),
        }
        warnings: list[str] = list(self.warnings)
        if self.execution_isolation is not None:
            payload["execution_isolation"] = dict(self.execution_isolation)
        if stdout_truncation.truncated:
            warnings.append(f"stdout truncated from tail by {stdout_truncation.truncated_by}")
        if stderr_truncation.truncated:
            warnings.append(f"stderr truncated from tail by {stderr_truncation.truncated_by}")
        if stdout_omitted > 0:
            warnings.append("stdout cursor skipped dropped bytes")
        if stderr_omitted > 0:
            warnings.append("stderr cursor skipped dropped bytes")
        if warnings:
            payload["warnings"] = warnings
        return payload

    def status(self) -> str:
        """Map process state to running/exited/terminated/timeout."""

        if self.timed_out:
            return "timeout"
        if self.terminating and self.process.poll() is None:
            return "running"
        if self.signal_name is not None:
            return "terminated"
        return "running" if self.process.poll() is None else "exited"

    def operation_outcome(self, status: str | None = None) -> str:
        """Classify the command outcome, including timeouts and intentional client kills."""
        status = self.status() if status is None else status
        return command_outcome(
            status,
            self.exit_code,
            self.signal_name,
            self.timed_out,
            killed=self.killed_by_client,
        )

    def refresh_status(self) -> None:
        """Enforce the deadline and collect exit state, draining readers and closing stdin."""
        if self.timeout_at is not None and not self.timed_out and self.process.poll() is None and time.time() >= self.timeout_at:
            self.timed_out = True
            terminate_process_group(self.process, signal.SIGTERM)
            self.drain_readers()
        code = self.process.poll()
        if code is None:
            return
        # Also release surviving Windows descendants before draining inherited
        # pipes. The Job's lifetime watcher does this for synchronous helpers.
        close_windows_job(self.process)
        self.drain_readers()
        # A keep_stdin_open pipe has no reader left; release it.
        self.close_stdin()
        self.exit_code = code
        self.terminating = False
        if code < 0:
            values = {item.value for item in signal.Signals}
            self.signal_name = signal.Signals(-code).name if -code in values else str(-code)
        self.closed = True
        if self.completed_at is None:
            self.completed_at = time.time()

    def drain_readers(self, timeout: float = 0.2) -> None:
        deadline = time.time() + timeout
        for thread in list(self.reader_threads):
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def retained_output_bytes(self) -> bytes:
        with self.lock:
            stdout = bytes(self.stdout)
            stderr = bytes(self.stderr)
        sections: list[bytes] = []
        if stdout:
            sections.extend([b"--- stdout ---\n", stdout])
        if stderr:
            if sections:
                sections.append(b"\n")
            sections.extend([b"--- stderr ---\n", stderr])
        return b"".join(sections)

    def retained_stream_segments(self, stream: str) -> tuple[bytes, bytes, int, int, int]:
        """Return (head, tail, tail_start_offset, total_bytes, tail_dropped_bytes).

        The retained set for a stream is the frozen head segment covering
        absolute offsets [0, len(head)) plus the rolling tail window covering
        [tail_start_offset, total_bytes). While the tail has not dropped
        anything the head is a duplicate prefix of the tail; once the tail
        rolls past the head, offsets between the two segments are evicted.
        """
        with self.lock:
            if stream == "stdout":
                return (
                    bytes(self.stdout_head),
                    bytes(self.stdout),
                    self.stdout_start_offset,
                    self.stdout_total_bytes,
                    self.stdout_dropped_bytes,
                )
            if stream == "stderr":
                return (
                    bytes(self.stderr_head),
                    bytes(self.stderr),
                    self.stderr_start_offset,
                    self.stderr_total_bytes,
                    self.stderr_dropped_bytes,
                )
        raise ValueError(f"Unknown output stream: {stream}")


def command_outcome(
    status: str,
    exit_code: int | None,
    signal_name: str | None,
    timed_out: bool,
    *,
    killed: bool = False,
) -> str:
    """Classify one command snapshot into a :data:`COMMAND_OUTCOMES` value."""

    if timed_out or status == "timeout":
        return "timeout"
    if status == "running":
        return "running"
    if killed:
        return "killed"
    if signal_name is not None or status == "terminated":
        return "signal"
    return "exited_0" if exit_code == 0 else "exited_nonzero"


def start_reader_threads(command: CommandRun) -> None:
    def reader(stream: BinaryIO, append: Any) -> None:
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                append(chunk)
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def pty_reader(fd: int) -> None:
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                command.append_stdout(chunk)
        except OSError:
            return
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            if command.pty_master_fd == fd:
                command.pty_master_fd = None

    if command.pty_master_fd is not None:
        thread = threading.Thread(target=pty_reader, args=(command.pty_master_fd,), daemon=True)
        command.reader_threads.append(thread)
        thread.start()
        return
    if command.process.stdout is not None:
        thread = threading.Thread(target=reader, args=(command.process.stdout, command.append_stdout), daemon=True)
        command.reader_threads.append(thread)
        thread.start()
    if command.process.stderr is not None:
        thread = threading.Thread(target=reader, args=(command.process.stderr, command.append_stderr), daemon=True)
        command.reader_threads.append(thread)
        thread.start()


def start_command_watchdog(command: CommandRun) -> None:
    if command.timeout_at is None:
        return

    def watchdog() -> None:
        delay = max(0.0, command.timeout_at - time.time()) if command.timeout_at is not None else 0.0
        try:
            command.process.wait(timeout=delay)
        except subprocess.TimeoutExpired:
            pass
        else:
            command.refresh_status()
            return
        if command.process.poll() is not None or command.timed_out:
            return
        command.timed_out = True
        terminate_process_group(command.process, signal.SIGTERM)
        command.refresh_status()

    threading.Thread(
        target=watchdog,
        name=f"coding-tools-watchdog-{command.command_id}",
        daemon=True,
    ).start()


def _trim_buffer(
    buffer: bytearray,
    *,
    total_bytes: int,
    start_offset_attr: str,
    command: CommandRun,
) -> int:
    tail_limit = command.buffer_limit - command.head_buffer_limit
    overflow = len(buffer) - tail_limit
    if overflow <= 0:
        return 0
    del buffer[:overflow]
    setattr(command, start_offset_attr, total_bytes - len(buffer))
    return overflow


def truncate_output_bytes_tail(data: bytes, max_bytes: int, max_lines: int = DEFAULT_MAX_LINES) -> TextTruncation:
    return truncate_text_tail(
        data.decode("utf-8", errors="replace"),
        max_lines=max_lines,
        max_bytes=max_bytes,
    )
