"""The only entry point for workspace-derived subprocesses.

The process module remains responsible for byte buffers and lifecycle. Every
launch here takes an immutable policy; read helpers receive narrower authority
and have configuration-driven program execution disabled.
"""
from __future__ import annotations

import os
import locale
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ToolFailure
from .policy import ExecutionPolicy
from .processes import HARD_KILL_SIGNAL, spawn_process, terminate_process_group

if TYPE_CHECKING:
    from .file_broker import FileBroker
    from .sandbox import SandboxBackend

_GIT_CONFIG = (
    "core.fsmonitor=false", "core.hooksPath=/dev/null", "core.pager=cat",
    "core.attributesFile=/dev/null", "diff.external=", "diff.trustExitCode=false",
    "protocol.allow=never", "protocol.file.allow=never", "credential.helper=",
    "submodule.recurse=false",
    "status.submoduleSummary=false", "diff.submodule=short",
)
_ENV_REJECT = re.compile(r"(TOKEN|SECRET|CREDENTIAL|PASSWORD|PASSWD|API.?KEY|PRIVATE)", re.I)
_HELPER_ENV = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL", "LC_CTYPE",
    "HOME", "TMPDIR", "TEMP", "TMP", "USERPROFILE",
})
_DANGEROUS_ENV = frozenset({
    "BASH_ENV", "ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS",
    "PERL5LIB", "PERL5OPT", "RUBYLIB", "RUBYOPT", "RIPGREP_CONFIG_PATH",
    "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_GLOBAL", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_EXEC_PATH",
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_EXTERNAL_DIFF",
})
_COMPATIBILITY_GIT_ENV = frozenset({
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "GIT_TEST_ASSUME_DIFFERENT_OWNER",
})


def filtered_environment(env: dict[str, str], *, helper: bool, strict: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in env.items():
        upper = key.upper()
        # Preserve the operator's protected-config selection in compatibility
        # mode, including system safe.directory entries and explicit opt-outs.
        # Git's executable mechanisms are still disabled by _prepare below.
        compatibility_git = helper and not strict and upper in _COMPATIBILITY_GIT_ENV
        if (helper or strict) and not compatibility_git and (
            _ENV_REJECT.search(upper) or upper in _DANGEROUS_ENV
            or upper.startswith(("LD_", "DYLD_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        ):
            continue
        if helper and not compatibility_git and upper not in _HELPER_ENV:
            continue
        # Windows has a single case-insensitive namespace, not Path and PATH.
        canonical = upper if os.name == "nt" else key
        result[canonical] = value
    if helper:
        result.update({
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat", "PAGER": "cat",
            "GIT_ATTR_NOSYSTEM": "1",
        })
        if strict:
            result["GIT_CONFIG_NOSYSTEM"] = "1"
            result["GIT_CONFIG_GLOBAL"] = os.devnull
    return result


class WorkspaceExecutor:
    def __init__(
        self,
        policy: Callable[[str], ExecutionPolicy],
        environment: Callable[[], dict[str, str]],
        *,
        file_broker: FileBroker | None = None,
    ) -> None:
        self._policy = policy
        self._environment = environment
        self.file_broker = file_broker
        self.last_launch_confirmed = False

    def _prepare(
        self, argv: Sequence[str], purpose: str, env: dict[str, str] | None,
    ) -> tuple[list[str], dict[str, str], ExecutionPolicy]:
        raw_env = self._environment() if env is None else env
        # Environment preparation may select a writable fallback runtime tree.
        # Compile only after that one-time choice so mounts and HOME agree.
        policy = self._policy(purpose)
        if not argv or not all(isinstance(item, str) and "\0" not in item for item in argv):
            raise ToolFailure("INVALID_ARGUMENT", "An argv list of non-NUL strings is required.", category="validation")
        helper = purpose == "read-helper"
        clean_env = filtered_environment(raw_env, helper=helper, strict=policy.strict)
        command = list(argv)
        executable = Path(command[0])
        if not executable.is_absolute():
            found = shutil.which(command[0], path=clean_env.get("PATH", os.defpath))
            if found:
                executable = Path(found)
        if policy.strict:
            try:
                real = executable.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ToolFailure("COMMAND_SPAWN_FAILED", "Executable is unavailable.", category="runtime") from exc
            # Read helpers are part of the service, never workspace-selected
            # programs. Arbitrary commands are intentionally allowed inside
            # the sandbox and do not receive service/helper authority.
            if helper and (
                not executable.is_absolute() or not policy.permits_read(real)
                or any(real.is_relative_to(root) for root in policy.write_roots)
                or real.is_relative_to(policy.workspace)
            ):
                raise ToolFailure("UNTRUSTED_EXECUTABLE", "Read-helper executable is not in a trusted toolchain root.", category="security")
            command[0] = str(real)
        name = executable.name.lower().removesuffix(".exe")
        if helper and name == "git":
            if policy.strict:
                self._validate_git_storage(policy)
            settings = list(_GIT_CONFIG)
            if os.name == "nt":
                settings = [s.replace("/dev/null", "NUL") for s in settings]
            command = [command[0], *(part for setting in settings for part in ("-c", setting)), *command[1:]]
            # --no-ext-diff and --no-textconv are command-specific, not global
            # options. These cover every read operation that can invoke them.
            for index, arg in enumerate(command):
                if arg in {"diff", "show", "log", "blame"}:
                    command[index + 1:index + 1] = ["--no-textconv"] + ([] if arg == "blame" else ["--no-ext-diff"])
                    break
            for index, arg in enumerate(command):
                if arg in {"status", "diff"}:
                    # A child Git launched for a submodule clears the private
                    # common-directory environment. Keep nested repositories
                    # outside this read operation rather than executing their
                    # repository-controlled conversion programs.
                    command.insert(index + 1, "--ignore-submodules=all")
                    break
        elif helper and name == "rg" and "--no-config" not in command:
            command.insert(1, "--no-config")
        return command, clean_env, policy

    def _validate_git_storage(self, policy: ExecutionPolicy) -> None:
        """Do not let .git indirection silently grant external storage access."""
        broker = self.file_broker
        if broker is None:
            raise ToolFailure("SANDBOX_UNAVAILABLE", "Strict Git helpers require the structured file broker.", category="security")
        if not broker.exists(".git"):
            return
        git_dir = policy.workspace / ".git"
        if broker.is_file(".git"):
            text = broker.read_text(".git").strip()
            if not text.startswith("gitdir:"):
                raise ToolFailure("GIT_ERROR", "Invalid Git directory file.", category="validation")
            git_dir = (policy.workspace / text[7:].strip()).resolve(strict=False)
        if not git_dir.is_relative_to(policy.workspace):
            raise ToolFailure("EXTERNAL_GIT_STORAGE_DENIED", "External Git worktrees require an explicitly authorized storage policy.", category="security")
        common = git_dir / "commondir"
        if broker.exists(common):
            target = (git_dir / broker.read_text(common).strip()).resolve(strict=False)
            if not target.is_relative_to(policy.workspace):
                raise ToolFailure("EXTERNAL_GIT_STORAGE_DENIED", "External Git common directories are denied.", category="security")
            git_dir = target
        alternates = git_dir / "objects" / "info" / "alternates"
        if broker.exists(alternates) and broker.read_text(alternates).strip():
            # Git's quoting and recursive alternate syntax is deliberately not
            # interpreted as authority. A future explicit storage grant can
            # support these; no repository file can grant itself access.
            raise ToolFailure("EXTERNAL_GIT_STORAGE_DENIED", "Git object alternates are unsupported in strict mode.", category="security")

    @staticmethod
    def _backend(policy: ExecutionPolicy) -> SandboxBackend:
        from .sandbox import SandboxBackend, SandboxSpec
        return SandboxBackend(SandboxSpec(
            workspace=policy.workspace, read_roots=policy.read_roots,
            write_roots=policy.write_roots, deny_roots=policy.deny_roots,
            network=policy.network, helper_path=policy.helper_path,
            helper_sha256=policy.helper_sha256, allowed_destinations=policy.allowed_destinations,
        ))

    def capability_report(self) -> dict[str, Any]:
        policy = self._policy("command")
        if not policy.strict:
            return {"mode": "compatibility", "strict": False, "network_enforced": False}
        return {
            "mode": "strict", "network": policy.network,
            **self._backend(policy).capability_report(),
            "last_launch_confirmed": self.last_launch_confirmed,
        }

    def spawn_managed(
        self, command: Any, *, cwd: str, shell: bool, env: dict[str, str], tty: bool,
        popen_kwargs: dict[str, Any],
    ) -> tuple[subprocess.Popen[bytes], int | None]:
        policy = self._policy("command")
        if not policy.strict:
            return spawn_process(command, cwd=cwd, shell=shell, env=env, tty=tty, popen_kwargs=popen_kwargs)
        if shell:
            command = ["/bin/sh", "-c", command]
        self.last_launch_confirmed = False
        argv, clean_env, policy = self._prepare(command, "command", env)
        result = self._backend(policy).spawn(argv, cwd=Path(cwd), env=clean_env, tty=tty)
        self.last_launch_confirmed = True
        return result

    def popen(
        self, argv: Sequence[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
        purpose: str = "read-helper", _helper_deadline: float | None = None,
        _watch_git_view: bool = True, **kwargs: Any,
    ) -> subprocess.Popen[Any]:
        self.last_launch_confirmed = False
        command, clean_env, policy = self._prepare(argv, purpose, env)
        actual_cwd = cwd or str(policy.workspace)
        view, clean_env, policy = self._git_helper_view(command, clean_env, policy, actual_cwd,
                                                      deadline=_helper_deadline)
        try:
            process = self._spawn_prepared(command, clean_env, policy, actual_cwd, **kwargs)
        except BaseException:
            if view is not None:
                view.cleanup()
            raise
        if view is not None:
            setattr(process, "_coding_git_view", view)
            # popen callers stream output independently. Retain the immutable
            # metadata until the process has finished using it.
            def release_view() -> None:
                try:
                    process.wait()
                finally:
                    view.cleanup()
            if _watch_git_view:
                threading.Thread(target=release_view, name="git-helper-view", daemon=True).start()
        return process

    def _git_helper_view(
        self, command: list[str], env: dict[str, str], policy: ExecutionPolicy, cwd: str,
        *, deadline: float | None = None,
    ) -> tuple[Any, dict[str, str], ExecutionPolicy]:
        if policy.purpose != "read-helper" or Path(command[0]).name.lower().removesuffix(".exe") != "git":
            return None, env, policy
        from .git_helpers import create_git_helper_view

        def probe(argv: list[str], probe_env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
            # Probes only inspect builtin repository/config metadata. They use
            # the same read-only, offline backend; no direct spawn exception.
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, 0)
            process = self._spawn_prepared(argv, probe_env, policy, cwd,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                stdout, stderr = process.communicate(timeout=self._remaining(deadline))
            except BaseException:
                terminate_process_group(process, HARD_KILL_SIGNAL)
                process.communicate()
                raise
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)

        def validate_storage(git_dir: Path, common: Path) -> None:
            if policy.strict:
                if any(not root.is_relative_to(policy.workspace) for root in (git_dir, common)):
                    raise ToolFailure("EXTERNAL_GIT_STORAGE_DENIED", "External Git storage is denied.", category="security")
                # Discovery and configuration are separate builtin probes.
                # Check indirection again before referencing their metadata.
                self._validate_git_storage(policy)

        view = create_git_helper_view(command, env, cwd=Path(cwd), run_probe=probe,
                                      validate_storage=validate_storage)
        if view is None:
            return None, env, policy
        if any(view.root.is_relative_to(root) or root.is_relative_to(view.root)
               for root in (policy.workspace, *policy.write_roots)):
            view.cleanup()
            raise ToolFailure("SANDBOX_UNAVAILABLE", "Private Git configuration overlaps a writable workspace or runtime root.", category="security")
        # This service-owned view is private to this read helper. It is never
        # included in arbitrary commands' readable or writable roots.
        return view, view.env, replace(policy, read_roots=(*policy.read_roots, view.root))

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        return None if deadline is None else max(0., deadline - time.monotonic())

    def _spawn_prepared(
        self, command: list[str], clean_env: dict[str, str], policy: ExecutionPolicy,
        cwd: str, **kwargs: Any,
    ) -> subprocess.Popen[Any]:
        if not policy.strict:
            if os.name != "nt":
                kwargs.setdefault("start_new_session", True)
            kwargs.setdefault("close_fds", True)
            if os.name == "nt":
                from .windows_job import spawn_windows_process
                return spawn_windows_process(command, cwd=cwd, env=clean_env, **kwargs)
            return subprocess.Popen(command, cwd=cwd, env=clean_env, **kwargs)
        text = kwargs.pop("text", False) or kwargs.pop("universal_newlines", False)
        encoding = kwargs.pop("encoding", None)
        errors = kwargs.pop("errors", None)
        stdio: dict[str, Any] = {}
        for key in ("stdin", "stdout", "stderr"):
            value = kwargs.pop(key, subprocess.PIPE)
            stdio[key] = subprocess.DEVNULL if value is None else value
            if value not in (None, subprocess.PIPE, subprocess.DEVNULL):
                raise ToolFailure("INVALID_ARGUMENT", "Strict helpers support only private pipes.", category="validation")
        if kwargs:
            raise ToolFailure("INVALID_ARGUMENT", f"Unsupported strict process options: {', '.join(kwargs)}", category="validation")
        process, _ = self._backend(policy).spawn(
            command, cwd=Path(cwd), env=clean_env, stdio=stdio,
            text=bool(text), encoding=encoding, errors=errors,
        )
        self.last_launch_confirmed = True
        return process

    def run(
        self, argv: Sequence[str], *, timeout: float | None = None, input: Any = None,
        check: bool = False, **kwargs: Any,
    ) -> subprocess.CompletedProcess[Any]:
        from .git_helpers import GitProbeFailure
        try:
            return self._run(argv, timeout=timeout, input=input, check=check, **kwargs)
        except subprocess.TimeoutExpired as exc:
            if (kwargs.get("purpose", "read-helper") == "read-helper" and argv
                    and Path(argv[0]).name.lower().removesuffix(".exe") == "git"):
                # Internal probes must report the requested operation's timeout
                # and argv, retaining subprocess's raw captured byte buffers.
                raise subprocess.TimeoutExpired(list(argv), timeout, output=exc.output, stderr=exc.stderr) from None
            raise
        except GitProbeFailure as exc:
            # Repository/configuration failures retain Git's exit status and
            # diagnostics. Never launch the original filter-enabled command.
            binary = exc.completed.stderr
            text = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding") or kwargs.get("errors")
            stderr: Any = binary
            stdout: Any = b""
            if text:
                stderr = binary.decode(kwargs.get("encoding") or locale.getencoding(),
                                       errors=kwargs.get("errors") or "strict")
                stderr = stderr.replace("\r\n", "\n").replace("\r", "\n")
                stdout = ""
            capture = kwargs.get("capture_output", False)
            destination = kwargs.get("stderr")
            merged = destination == subprocess.STDOUT
            if merged:
                destination = kwargs.get("stdout")
                stdout = stderr
                stderr = None
            if not capture and destination != subprocess.PIPE and destination != subprocess.DEVNULL and binary:
                descriptor = (1 if merged else 2) if destination is None else (
                    destination if isinstance(destination, int) else destination.fileno())
                pending = memoryview(binary)
                while pending:
                    pending = pending[os.write(descriptor, pending):]
            completed = subprocess.CompletedProcess(
                list(argv), exc.completed.returncode,
                stdout if capture or kwargs.get("stdout") == subprocess.PIPE else None,
                stderr if not merged and (capture or kwargs.get("stderr") == subprocess.PIPE) else None,
            )
            if check:
                completed.check_returncode()
            return completed

    def _run(
        self, argv: Sequence[str], *, timeout: float | None = None, input: Any = None,
        check: bool = False, **kwargs: Any,
    ) -> subprocess.CompletedProcess[Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        # communicate's timeout cleanup normally kills only the direct child;
        # managed helper process groups and native supervisors own descendants.
        if os.name != "nt" and not self._policy(str(kwargs.get("purpose", "read-helper"))).strict:
            purpose = kwargs.pop("purpose", "read-helper")
            env = kwargs.pop("env", None)
            command, clean_env, policy = self._prepare(argv, purpose, env)
            kwargs.setdefault("cwd", str(policy.workspace))
            kwargs.setdefault("close_fds", True)
            view, clean_env, _ = self._git_helper_view(command, clean_env, policy, str(kwargs["cwd"]), deadline=deadline)
            try:
                if view is not None:
                    if kwargs.pop("capture_output", False):
                        if "stdout" in kwargs or "stderr" in kwargs:
                            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
                        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if input is not None:
                        if kwargs.get("stdin") is not None:
                            raise ValueError("stdin and input arguments may not both be used.")
                        kwargs["stdin"] = subprocess.PIPE
                    process = self._spawn_prepared(command, clean_env, policy, str(kwargs.pop("cwd")), **kwargs)
                    try:
                        stdout, stderr = process.communicate(input=input, timeout=self._remaining(deadline))
                    except BaseException:
                        terminate_process_group(process, HARD_KILL_SIGNAL)
                        process.communicate()
                        raise
                    finally:
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
                    completed = subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)
                    if check:
                        completed.check_returncode()
                    return completed
                return subprocess.run(command, env=clean_env, timeout=timeout, input=input, check=check, **kwargs)
            finally:
                if view is not None:
                    view.cleanup()
        if input is not None:
            kwargs.setdefault("stdin", subprocess.PIPE)
        git_helper = (kwargs.get("purpose", "read-helper") == "read-helper" and bool(argv)
                      and Path(argv[0]).name.lower().removesuffix(".exe") == "git")
        if git_helper:
            process = self.popen(argv, _helper_deadline=deadline, _watch_git_view=False, **kwargs)
        else:
            process = self.popen(argv, **kwargs)
        try:
            stdout, stderr = process.communicate(input=input, timeout=self._remaining(deadline) if git_helper else timeout)
        except BaseException:
            terminate_process_group(process, HARD_KILL_SIGNAL)
            process.communicate()
            raise
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            if git_helper and (view := getattr(process, "_coding_git_view", None)) is not None:
                view.cleanup()
        completed = subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)
        if check:
            completed.check_returncode()
        return completed
