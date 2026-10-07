"""Fail-closed native sandbox launchers.

Only Linux/bubblewrap currently satisfies the whole strict contract. Seatbelt
profile generation is retained as an explicitly incomplete adapter: filesystem
and network rules alone do not account for daemonized descendants on macOS.
Unavailable backends never fall back to subprocess. Proxy mode uses a native
namespace-local relay and a host-side CONNECT policy proxy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ToolFailure
from .processes import spawn_process

HELPER_VERSION = "0.1.0"
PROTOCOL_VERSION = 1
MIN_BWRAP_VERSION = (0, 12, 0)
HANDSHAKE_TIMEOUT = 10.0
_HELPER_DEST = "/.coding-tools-sandbox-helper"
_RESERVED_ROOTS = (Path("/proc"), Path("/sys"), Path("/dev"))


@dataclass(frozen=True)
class SandboxSpec:
    workspace: Path
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    deny_roots: tuple[Path, ...]
    network: str = "offline"
    helper_path: Path | None = None
    helper_sha256: str | None = None
    allowed_destinations: tuple[str, ...] = ()


def _failure(code: str, message: str, **details: Any) -> ToolFailure:
    return ToolFailure(code, message, category="security", details=details)


def _beneath(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _canonical_roots(roots: tuple[Path, ...], *, must_exist: bool) -> tuple[Path, ...]:
    result = []
    for root in roots:
        if not root.is_absolute():
            raise _failure("SANDBOX_POLICY_INVALID", "Sandbox roots must be absolute.")
        try:
            target = root.resolve(strict=must_exist)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _failure("SANDBOX_POLICY_INVALID", f"Sandbox root is unavailable: {root}") from exc
        if target != root:
            raise _failure("SANDBOX_POLICY_INVALID", "Sandbox roots must be canonical and must not change to symbolic links.")
        if target == Path("/"):
            raise _failure("SANDBOX_POLICY_INVALID", "The filesystem root cannot be a sandbox grant.")
        result.append(target)
    return tuple(dict.fromkeys(result))


def _open_pinned(path: Path, *, executable: bool = False) -> int:
    """Open an absolute canonical path without following any replacement link.

    A descriptor pins each parent during traversal. bubblewrap's bind-fd checks
    the resulting mount's device/inode before any untrusted code can start.
    """
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            new = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = new
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path.name, flags, dir_fd=directory)
        if executable and not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise _failure("SANDBOX_HELPER_UNTRUSTED", "Native helper must be a regular executable file.")
        return fd
    finally:
        os.close(directory)


def _trusted_install(path: Path, writes: tuple[Path, ...], *, root_owned: bool = False) -> None:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise _failure("SANDBOX_HELPER_UNTRUSTED", "Sandbox executables must use canonical absolute paths.")
    if _beneath(path, writes):
        raise _failure("SANDBOX_HELPER_UNTRUSTED", "Sandbox executables must be outside command-writable roots.")
    for current in (path, *path.parents):
        info = current.stat()
        # A sticky shared ancestor such as /tmp is safe only for an owned,
        # non-shared install directory below it, never for the binary itself.
        sticky_parent = current != path and stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_ISVTX)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not sticky_parent:
            raise _failure("SANDBOX_HELPER_UNTRUSTED", f"Untrusted writable executable ancestor: {current}")
        expected = (0,) if root_owned else (0, os.geteuid())
        if info.st_uid not in expected:
            raise _failure("SANDBOX_HELPER_UNTRUSTED", f"Untrusted executable owner: {current}")
    if not os.access(path, os.X_OK):
        raise _failure("SANDBOX_HELPER_UNTRUSTED", "Sandbox executable is not executable.")


def seatbelt_profile(spec: SandboxSpec) -> str:
    """Compile the bounded offline filesystem/network portion of macOS policy.

    Not a complete execution backend: strict launch is refused until there is
    a native, enforceable descendant-lifetime boundary. Kept testable without
    advertising that a generated profile is installed or effective.
    """
    if spec.network != "offline":
        raise _failure("SANDBOX_NETWORK_UNSUPPORTED", "Enforced proxy mode is not implemented.")
    reads = _canonical_roots(spec.read_roots, must_exist=False)
    writes = _canonical_roots(spec.write_roots, must_exist=False)
    denies = _canonical_roots(spec.deny_roots, must_exist=False)
    lines = [
        "(version 1)", "(deny default)", "(deny network*)",
        "(allow process-exec)", "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        # Runtime startup/getcwd can open the root directory for reading.
        # A literal root grant does not authorize any descendant file.
        '(allow file-read* (literal "/"))',
        '(allow file-read* file-write-data (literal "/dev/null"))',
        '(allow file-read* (literal "/dev/urandom"))',
        # Loader/CPython platform metadata only; this grants no Mach service,
        # host network, or additional file-content access.
        '(allow sysctl-read (sysctl-name "hw.machine") (sysctl-name "hw.ncpu") '
        '(sysctl-name "hw.pagesize") (sysctl-name "kern.ostype") '
        '(sysctl-name "kern.osrelease") (sysctl-name "kern.osversion") '
        '(sysctl-name "kern.osproductversion") (sysctl-name "kern.version") '
        '(sysctl-name "kern.argmax"))',
    ]
    for root in dict.fromkeys((*reads, *writes)):
        encoded = json.dumps(str(root))
        lines.append(f"(allow file-read* (subpath {encoded}))")
        # Executable mappings are a separate Seatbelt operation from reads.
        # Required for the dynamic loader and Python extension modules; scope
        # remains exactly the already-authorized executable/read tree.
        lines.append(f"(allow file-map-executable (subpath {encoded}))")
        lines.append(f"(allow file-read-metadata (path-ancestors {encoded}))")
    for root in writes:
        lines.append(f"(allow file-write* (subpath {json.dumps(str(root))}))")
    for root in denies:
        lines.append(f"(deny file-read* file-write* file-map-executable (subpath {json.dumps(str(root))}))")
    return "\n".join(lines) + "\n"


class SandboxBackend:
    def __init__(self, spec: SandboxSpec) -> None:
        self.spec = spec
        self.platform = sys.platform
        self._last_confirmed = False

    def capability_report(self) -> dict[str, Any]:
        """Describe guarantees separately from unprobed host availability."""
        linux = self.platform.startswith("linux")
        backend = "linux-bwrap" if linux else "macos-seatbelt" if self.platform == "darwin" else "windows-native" if self.platform == "win32" else "unsupported"
        reason = None
        error_code = "SANDBOX_UNAVAILABLE"
        if self.spec.network not in {"offline", "proxy"}:
            reason = "Unknown sandbox network mode."
            error_code = "SANDBOX_NETWORK_UNSUPPORTED"
        elif self.spec.network == "proxy" and not self.spec.allowed_destinations:
            reason = "Proxy mode requires an explicit destination host:port allowlist."
            error_code = "SANDBOX_NETWORK_UNSUPPORTED"
        elif self.platform == "darwin":
            reason = "Seatbelt descendant cleanup is not guaranteed for setsid/double-fork processes."
        elif not linux:
            reason = "No native backend satisfies strict filesystem, network, and descendant isolation."
        elif platform.machine().lower() not in {"x86_64", "amd64", "aarch64", "arm64"}:
            reason = "The native seccomp helper supports only x86_64 and aarch64 Linux."
        elif self.spec.helper_path is None or self.spec.helper_sha256 is None:
            reason = "A trusted, SHA-256-pinned native helper must be configured."
        elif not Path("/usr/bin/bwrap").is_file():
            reason = "bubblewrap >= 0.12.0 is required at /usr/bin/bwrap."
        return {
            "backend": backend,
            "protocol_version": PROTOCOL_VERSION,
            "helper_version": HELPER_VERSION,
            "strict_supported": reason is None,
            "availability": "unprobed" if reason is None else "unavailable",
            "reason": reason,
            "error_code": error_code if reason is not None else None,
            "filesystem_isolation": linux,
            "offline_network_isolation": linux,
            "descendant_cleanup": linux,
            "proxy_egress": linux and bool(self.spec.allowed_destinations),
            "last_launch_confirmed": self._last_confirmed,
        }

    def _validate(self, argv: list[str], cwd: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        report = self.capability_report()
        if report["reason"]:
            raise _failure(report["error_code"], report["reason"], capabilities=report)
        if not argv or not Path(argv[0]).is_absolute():
            raise _failure("SANDBOX_POLICY_INVALID", "Strict execution requires an absolute executable path.")
        workspace = _canonical_roots((self.spec.workspace,), must_exist=True)[0]
        reads = _canonical_roots((workspace, *self.spec.read_roots), must_exist=True)
        writes = _canonical_roots(self.spec.write_roots, must_exist=True)
        denies = _canonical_roots(self.spec.deny_roots, must_exist=False)
        try:
            resolved_cwd = cwd.resolve(strict=True)
            if not resolved_cwd.is_dir():
                raise _failure("SANDBOX_POLICY_INVALID", "Command cwd is not a directory.", cwd=str(cwd))
        except (OSError, RuntimeError, ValueError) as exc:
            raise _failure("SANDBOX_POLICY_INVALID", "Command cwd cannot be resolved.", cwd=str(cwd)) from exc
        if not _beneath(resolved_cwd, reads + writes) or _beneath(resolved_cwd, denies):
            raise _failure("SANDBOX_POLICY_INVALID", "Command cwd is outside the permitted roots.")
        for root in reads + writes:
            if _beneath(root, _RESERVED_ROOTS):
                raise _failure("SANDBOX_POLICY_INVALID", "Host proc, sys and device roots cannot be granted.")
        for denied in denies:
            if any(root == denied or denied in root.parents or root in denied.parents for root in reads + writes):
                raise _failure(
                    "SANDBOX_POLICY_INVALID",
                    "Denied roots must be disjoint from every readable or writable root. Nested path masks cannot protect hardlink aliases or renamed trees; move private data outside the granted trees.",
                    denied_root=str(denied),
                )
        return reads, writes

    def _helper_fd(self, writes: tuple[Path, ...]) -> int:
        helper = self.spec.helper_path
        digest = self.spec.helper_sha256
        if helper is None or digest is None or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise _failure("SANDBOX_HELPER_UNTRUSTED", "A native helper and exact SHA-256 pin are required.")
        try:
            _trusted_install(helper, (self.spec.workspace.resolve(), *writes))
            fd = _open_pinned(helper, executable=True)
            info = os.fstat(fd)
            if info.st_nlink != 1 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or info.st_uid not in (0, os.geteuid()):
                os.close(fd)
                raise _failure("SANDBOX_HELPER_UNTRUSTED", "Native helper must be an unshared, trusted inode.")
        except (OSError, RuntimeError, ValueError) as exc:
            raise _failure("SANDBOX_HELPER_UNTRUSTED", "Native helper is unavailable or its path changed.") from exc
        try:
            with os.fdopen(os.dup(fd), "rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            os.lseek(fd, 0, os.SEEK_SET)
            if not hmac.compare_digest(actual, digest.lower()):
                raise _failure("SANDBOX_HELPER_UNTRUSTED", "Native helper SHA-256 does not match the configured pin.")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _bwrap(self, writes: tuple[Path, ...]) -> Path:
        try:
            executable = Path("/usr/bin/bwrap").resolve(strict=True)
            _trusted_install(executable, writes, root_owned=True)
            check = subprocess.run([str(executable), "--version"], cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=True)
            version = re.fullmatch(rb"bubblewrap (\d+)\.(\d+)\.(\d+)\s*", check.stdout)
            if version is None or tuple(map(int, version.groups())) < MIN_BWRAP_VERSION:
                raise _failure("SANDBOX_UNAVAILABLE", "bubblewrap >= 0.12.0 with fd-pinned mounts is required.")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise _failure("SANDBOX_UNAVAILABLE", "Trusted bubblewrap is missing or its version probe failed.") from exc
        return executable

    def _linux_argv(self, argv: list[str], cwd: Path, control_fd: int, nonce: str, fds: list[int], reads: tuple[Path, ...], writes: tuple[Path, ...], proxy_socket: Path | None = None) -> list[str]:
        bwrap = self._bwrap(writes)
        helper_fd = self._helper_fd(writes)
        fds.append(helper_fd)
        command = [str(bwrap), "--unshare-all", "--unshare-user", "--die-with-parent", "--new-session", "--disable-userns", "--cap-drop", "ALL", "--tmpfs", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
        # Parent roots before children; writable overlays follow readonly views.
        # FD binding (>= 0.12) verifies inode identity even if renamed mid-launch.
        for mode, roots in (("--ro-bind-fd", reads), ("--bind-fd", writes)):
            for root in sorted(set(roots), key=lambda p: (len(p.parts), str(p))):
                if mode == "--ro-bind-fd" and any(other != root and other in root.parents for other in roots):
                    continue
                fd = _open_pinned(root)
                fds.append(fd)
                command += [mode, str(fd), str(root)]
        if proxy_socket is not None:
            # Only the dedicated socket directory is made visible. The native
            # child filter denies ALL AF_UNIX sockets, so only the trusted
            # helper relay (outside that filter) can use this endpoint.
            proxy_fd = _open_pinned(proxy_socket.parent)
            fds.append(proxy_fd)
            command += ["--ro-bind-fd", str(proxy_fd), str(proxy_socket.parent)]
        # system_read_roots canonicalizes merged-/usr aliases. Restore only
        # trusted system aliases whose targets are already explicitly readable.
        for name in ("/bin", "/sbin", "/lib", "/lib64"):
            link = Path(name)
            if link.is_symlink() and _beneath(link.resolve(), reads):
                command += ["--symlink", str(link.resolve()), name]
        command += ["--perms", "0555", "--ro-bind-data", str(helper_fd), _HELPER_DEST,
                    "--remount-ro", "/", "--chdir", str(cwd), "--", _HELPER_DEST, "--control-fd", str(control_fd), "--nonce", nonce]
        if proxy_socket is not None:
            command += ["--proxy-socket", str(proxy_socket)]
        command += ["--", *argv]
        return command

    def spawn(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], tty: bool = False,
        stdio: dict[str, Any] | None = None, text: bool = False,
        encoding: str | None = None, errors: str | None = None,
    ) -> tuple[subprocess.Popen[Any], int | None]:
        self._last_confirmed = False
        reads, writes = self._validate(argv, Path(cwd))
        fds: list[int] = []
        try:
            parent, child = socket.socketpair()
        except OSError as exc:
            raise _failure("SANDBOX_INITIALIZATION_FAILED", "The trusted native control channel could not be created.", cause=str(exc)) from exc
        process: subprocess.Popen[Any] | None = None
        pty_fd: int | None = None
        proxy = None
        try:
            nonce = secrets.token_hex(32)
            if self.spec.network == "proxy":
                from .network_proxy import ControlledProxy
                proxy = ControlledProxy(self.spec.allowed_destinations).start()
                command = self._linux_argv(argv, Path(cwd).resolve(), child.fileno(), nonce, fds, reads, writes, proxy.socket_path)
            else:
                command = self._linux_argv(argv, Path(cwd).resolve(), child.fileno(), nonce, fds, reads, writes)
            safe_env = dict(env)
            safe_env.update({"HOME": "/tmp/home", "TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp"})
            # Loader/plugin injection must not run before the trusted helper.
            for key in list(safe_env):
                if key.startswith(("LD_", "DYLD_")) or key in {"BASH_ENV", "ENV", "GCONV_PATH", "GLIBC_TUNABLES", "PYTHONHOME", "PYTHONPATH", "PERL5OPT", "RUBYOPT", "NODE_OPTIONS"}:
                    del safe_env[key]
            process, pty_fd = spawn_process(command, cwd="/", shell=False, env=safe_env, tty=tty,
                                           popen_kwargs={"start_new_session": True, "close_fds": True, "pass_fds": tuple([child.fileno(), *fds]),
                                                         "text": text, "encoding": encoding, "errors": errors}, stdio=stdio)
            child.close()
            for fd in fds:
                os.close(fd)
            fds.clear()
            parent.settimeout(HANDSHAKE_TIMEOUT)
            handshake_backend = "linux-bwrap-proxy" if proxy is not None else "linux-bwrap"
            expected = f"CTMCP_SANDBOX {PROTOCOL_VERSION} {HELPER_VERSION} {handshake_backend} {nonce}\n".encode("ascii")
            received = bytearray()
            deadline = time.monotonic() + HANDSHAKE_TIMEOUT
            while not received.endswith(b"\n") and len(received) <= 256:
                parent.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = parent.recv(257 - len(received))
                if not chunk:
                    break
                received += chunk
            if not hmac.compare_digest(bytes(received), expected):
                raise _failure("SANDBOX_INITIALIZATION_FAILED", "Native isolation was not confirmed; command was not authorized to start.", backend="linux-bwrap")
            if proxy is not None and not proxy.healthy:
                raise _failure("SANDBOX_INITIALIZATION_FAILED", "The controlled proxy failed before command startup.")
            parent.sendall(f"GO {nonce}\n".encode("ascii"))
            self._last_confirmed = True
            if proxy is not None:
                def release_proxy() -> None:
                    assert process is not None
                    process.wait()
                    assert proxy is not None
                    proxy.close()
                threading.Thread(target=release_proxy, name="sandbox-proxy-lifetime", daemon=True).start()
            return process, pty_fd
        except BaseException as exc:
            backend_stderr = ""
            parent.close()  # Close the launch gate before process cleanup.
            if proxy is not None:
                proxy.close()
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # A short-lived process can already be exiting on
                        # Darwin. Popen.kill rechecks child state before the
                        # direct-PID fallback; never signal another group.
                        process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                if process.stderr is not None:
                    try:
                        diagnostic_output = process.stderr.read(4096)
                        backend_stderr = (diagnostic_output.decode("utf-8", errors="replace")
                                          if isinstance(diagnostic_output, bytes) else diagnostic_output).strip()
                    except (OSError, ValueError):
                        pass
                    process.stderr.close()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stdin is not None:
                    process.stdin.close()
            if pty_fd is not None:
                os.close(pty_fd)
            if isinstance(exc, ToolFailure):
                if backend_stderr:
                    exc.details["backend_stderr"] = backend_stderr
                raise
            if isinstance(exc, (OSError, TimeoutError)):
                raise _failure("SANDBOX_INITIALIZATION_FAILED", "Sandbox initialization failed; no unsandboxed fallback was attempted.", cause=str(exc), backend_stderr=backend_stderr) from exc
            raise
        finally:
            parent.close()
            child.close()
            for fd in fds:
                os.close(fd)
