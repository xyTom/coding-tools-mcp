from __future__ import annotations

import functools
import ntpath
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Literal

from .errors import ToolFailure
from .textutils import DEFAULT_MAX_LINES, TextTruncation, truncate_text_tail


COMMAND_BUFFER_BYTES = 524_288
# Fraction of the per-stream budget frozen as the head segment. The head keeps
# the earliest output (command echo, first error) that a tail-only rolling
# buffer would lose first, mirroring the head+tail retention used by other
# agent runtimes.
COMMAND_HEAD_BUFFER_DIVISOR = 8
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
PWSH_PATH_ENV = "CODING_TOOLS_MCP_PWSH_PATH"
CMD_FALLBACK_WARNING = (
    "PowerShell 7 (pwsh) is unavailable; exec_command is using the cmd.exe "
    "compatibility fallback. Commands use cmd.exe syntax, not PowerShell syntax. "
    "Install PowerShell 7 or set CODING_TOOLS_MCP_PWSH_PATH to use PowerShell."
)


@dataclass(frozen=True)
class WindowsCommandShell:
    """Trusted Windows command interpreter selected by the server."""

    kind: Literal["pwsh", "cmd"]
    executable: str
    fallback: bool = False
    fallback_reason: str | None = None
    warning: str | None = None


def _environment_value(env: Mapping[str, str], target: str) -> str | None:
    target_upper = target.upper()
    for name, value in env.items():
        if name.upper() == target_upper:
            return value
    return None


def _pwsh_probe_environment() -> dict[str, str]:
    """Build a minimal host-derived environment without server secrets."""

    allowed = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC")
    result: dict[str, str] = {}
    for name in allowed:
        value = _environment_value(os.environ, name)
        if value is not None:
            result[name] = value
    return result


def _find_windows_executable_on_path(filename: str, path: str | None) -> str | None:
    """Search absolute PATH entries without Windows' implicit current-directory lookup."""

    if not path:
        return None
    current_dir = ntpath.normcase(ntpath.abspath(os.getcwd()))
    for raw_entry in path.split(";"):
        entry = ntpath.expandvars(raw_entry.strip().strip('"'))
        if not entry or not ntpath.isabs(entry):
            continue
        normalized_entry = ntpath.normcase(ntpath.abspath(entry))
        try:
            if ntpath.commonpath((current_dir, normalized_entry)) == current_dir:
                continue
        except ValueError:
            pass
        candidate = ntpath.normpath(ntpath.join(entry, filename))
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_pwsh() -> str:
    """Resolve PowerShell 7 only from the trusted server process environment."""

    executable: str | None
    configured = (_environment_value(os.environ, PWSH_PATH_ENV) or "").strip().strip('"')
    if configured:
        executable = ntpath.normpath(ntpath.expandvars(configured))
        if (
            not ntpath.isabs(executable)
            or ntpath.basename(executable).lower() != "pwsh.exe"
            or not os.path.isfile(executable)
        ):
            raise ToolFailure(
                "SHELL_NOT_FOUND",
                f"{PWSH_PATH_ENV} must point to an existing absolute pwsh.exe path.",
                category="runtime",
                details={"executable": executable, "environment_variable": PWSH_PATH_ENV},
            )
    else:
        executable = _find_windows_executable_on_path(
            "pwsh.exe",
            _environment_value(os.environ, "PATH"),
        )
    if executable is None:
        raise ToolFailure(
            "SHELL_NOT_FOUND",
            "PowerShell 7 was not found on the server process PATH.",
            category="runtime",
            details={
                "executable": "pwsh",
                "retry_hint": (
                    "Install PowerShell 7 and add pwsh to PATH, or set "
                    f"{PWSH_PATH_ENV} to its absolute path."
                ),
            },
        )
    major = pwsh_major_version(executable)
    if major < 7:
        raise ToolFailure(
            "SHELL_VERSION_UNSUPPORTED",
            f"PowerShell 7 or newer is required; resolved major version {major}.",
            category="runtime",
            details={"executable": executable, "major_version": major, "required_major_version": 7},
        )
    return executable


def resolve_cmd() -> str:
    """Resolve cmd.exe from trusted server-process Windows locations."""

    raw_candidates: list[str] = []
    comspec = (_environment_value(os.environ, "COMSPEC") or "").strip().strip('"')
    if comspec:
        raw_candidates.append(comspec)
    for variable in ("SYSTEMROOT", "WINDIR"):
        root = (_environment_value(os.environ, variable) or "").strip().strip('"')
        if root:
            raw_candidates.append(ntpath.join(root, "System32", "cmd.exe"))

    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        candidate = ntpath.normpath(ntpath.expandvars(raw_candidate))
        normalized = ntpath.normcase(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if (
            ntpath.isabs(candidate)
            and ntpath.basename(candidate).lower() == "cmd.exe"
            and os.path.isfile(candidate)
        ):
            return candidate

    executable = _find_windows_executable_on_path(
        "cmd.exe",
        _environment_value(os.environ, "PATH"),
    )
    if executable is not None:
        return executable
    raise ToolFailure(
        "SHELL_NOT_FOUND",
        "Neither PowerShell 7 nor cmd.exe could be resolved for Windows string commands.",
        category="runtime",
        details={
            "executable": "cmd.exe",
            "retry_hint": (
                "Restore the Windows command processor or install PowerShell 7 and add "
                f"pwsh to PATH (or set {PWSH_PATH_ENV})."
            ),
        },
    )


def resolve_windows_command_shell() -> WindowsCommandShell:
    """Prefer PowerShell 7 and automatically retain cmd.exe compatibility."""

    configured_pwsh = bool((_environment_value(os.environ, PWSH_PATH_ENV) or "").strip())
    try:
        return WindowsCommandShell(kind="pwsh", executable=resolve_pwsh())
    except ToolFailure as exc:
        # An explicit pin is operator intent. Falling back would hide a typo or
        # silently ignore a version constraint the operator expected to hold.
        if configured_pwsh:
            raise
        return WindowsCommandShell(
            kind="cmd",
            executable=resolve_cmd(),
            fallback=True,
            fallback_reason=exc.code,
            warning=CMD_FALLBACK_WARNING,
        )


_selected_shell_lock = threading.Lock()
_selected_shell: WindowsCommandShell | ToolFailure | None = None


def selected_windows_command_shell(*, refresh: bool = False) -> WindowsCommandShell:
    """Resolve the Windows shell once per process and pin the answer.

    Re-resolving per call would let a transient probe failure flip one exec to
    cmd.exe while the next uses pwsh, and would let concurrent requests each
    start their own multi-second version probe before command accounting sees
    them. The lock serializes the first resolution; every later call reuses the
    pinned selection (including a pinned failure) until ``refresh`` — used by
    check_exec_environment so operators can pick up a newly installed shell
    without restarting the server.
    """

    global _selected_shell
    with _selected_shell_lock:
        if refresh or _selected_shell is None:
            try:
                _selected_shell = resolve_windows_command_shell()
            except ToolFailure as failure:
                _selected_shell = failure
        pinned = _selected_shell
    if isinstance(pinned, ToolFailure):
        raise pinned
    return pinned


def _reset_selected_windows_command_shell() -> None:
    """Drop the pinned shell selection (test seam)."""

    global _selected_shell
    with _selected_shell_lock:
        _selected_shell = None


@functools.lru_cache(maxsize=16)
def pwsh_major_version(executable: str) -> int:
    """Return and cache the major version reported by a pwsh executable."""

    try:
        completed = subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$PSVersionTable.PSVersion.Major",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
            env=_pwsh_probe_environment(),
            cwd=ntpath.dirname(executable) or None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolFailure(
            "SHELL_VERSION_UNSUPPORTED",
            "PowerShell version could not be verified.",
            category="runtime",
            details={"executable": executable, "reason": str(exc)},
        ) from exc
    stdout = completed.stdout.strip()
    if completed.returncode != 0 or not stdout.isdigit():
        raise ToolFailure(
            "SHELL_VERSION_UNSUPPORTED",
            "PowerShell version could not be verified.",
            category="runtime",
            details={
                "executable": executable,
                "exit_code": completed.returncode,
                "stderr": completed.stderr.strip()[:500],
            },
        )
    return int(stdout)


def build_pwsh_argv(executable: str, command: str) -> list[str]:
    """Build a deterministic non-interactive PowerShell invocation."""

    return [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        command,
    ]


def build_cmd_command_line(executable: str, command: str) -> str:
    """Build the exact cmd.exe command line, bypassing MS CRT argv quoting.

    Passing an argument list would make Popen serialize it with list2cmdline,
    whose ``\\"`` escapes reach cmd.exe verbatim and corrupt any command that
    contains quotes. ``/S /C`` is specified against a raw command line instead:
    cmd strips the first and last quote and executes everything between them
    unchanged, so the payload needs exactly one enclosing quote pair and no
    further escaping.
    """

    return f'"{executable}" /D /V:OFF /S /C "{command}"'


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
    windows_shell: WindowsCommandShell | None = None,
) -> tuple[subprocess.Popen[bytes], int | None]:
    """Spawn a pipe-backed or true POSIX PTY-backed process."""

    if not tty:
        if os.name == "nt" and shell and isinstance(command, str):
            selected_shell = windows_shell or selected_windows_command_shell()
            if selected_shell.kind == "pwsh":
                command = build_pwsh_argv(selected_shell.executable, command)
            else:
                command = build_cmd_command_line(selected_shell.executable, command)
            shell = False
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
    _stdin_closed: bool = False

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
