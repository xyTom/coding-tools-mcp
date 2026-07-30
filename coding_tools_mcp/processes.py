from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from .errors import ToolFailure
from .textutils import DEFAULT_MAX_LINES, TextTruncation, truncate_text_tail


COMMAND_BUFFER_BYTES = 524_288
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def terminate_process_group(
    process: subprocess.Popen[bytes],
    signum: signal.Signals,
    *,
    force: bool = False,
) -> None:
    if not hasattr(os, "killpg"):
        if os.name == "nt" and not force:
            event = getattr(signal, "CTRL_BREAK_EVENT", None)
            if event is not None:
                try:
                    process.send_signal(event)
                    process.wait(timeout=1)
                    return
                except Exception:
                    pass
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
) -> tuple[subprocess.Popen[bytes], int | None]:
    """Spawn a pipe-backed or true POSIX PTY-backed process."""

    if not tty:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
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
    _stdin_closed: bool = False

    @property
    def retained_bytes(self) -> int:
        with self.lock:
            return len(self.stdout) + len(self.stderr)

    def append_stdout(self, chunk: bytes) -> None:
        with self.lock:
            self.stdout.extend(chunk)
            self.stdout_total_bytes += len(chunk)
            self.stdout_dropped_bytes += _trim_buffer(
                self.stdout,
                total_bytes=self.stdout_total_bytes,
                start_offset_attr="stdout_start_offset",
                command=self,
            )

    def append_stderr(self, chunk: bytes) -> None:
        with self.lock:
            self.stderr.extend(chunk)
            self.stderr_total_bytes += len(chunk)
            self.stderr_dropped_bytes += _trim_buffer(
                self.stderr,
                total_bytes=self.stderr_total_bytes,
                start_offset_attr="stderr_start_offset",
                command=self,
            )

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
        if self.timed_out:
            status = "timeout"
        elif self.terminating and self.process.poll() is None:
            status = "running"
        elif self.signal_name is not None:
            status = "terminated"
        else:
            status = "running" if self.process.poll() is None else "exited"
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
            "ok": True,
        }
        warnings: list[str] = list(self.warnings)
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

    def refresh_status(self) -> None:
        if self.timeout_at is not None and not self.timed_out and self.process.poll() is None and time.time() >= self.timeout_at:
            self.timed_out = True
            terminate_process_group(self.process, signal.SIGTERM)
            self.drain_readers()
        code = self.process.poll()
        if code is None:
            return
        self.drain_readers()
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

    def retained_stream_bytes(self, stream: str) -> tuple[bytes, int, int, int]:
        with self.lock:
            if stream == "stdout":
                return bytes(self.stdout), self.stdout_start_offset, self.stdout_total_bytes, self.stdout_dropped_bytes
            if stream == "stderr":
                return bytes(self.stderr), self.stderr_start_offset, self.stderr_total_bytes, self.stderr_dropped_bytes
        raise ValueError(f"Unknown output stream: {stream}")


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
    overflow = len(buffer) - command.buffer_limit
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
