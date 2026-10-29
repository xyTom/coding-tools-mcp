"""Immutable, per-launch Git configuration for read helpers.

Git has no wildcard switch to disable clean/process filters. Command-line
overrides for previously observed driver names race with a new driver in the
repository configuration. A private common-directory view instead keeps Git's
original HEAD/index and object database, while replacing every configuration
scope with an expanded, sanitized snapshot. All subprocesses are delegated to
the caller's executor, including the two configuration/discovery probes.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from .errors import ToolFailure

GitProbe = Callable[[list[str], dict[str, str]], subprocess.CompletedProcess[bytes]]
_CONFIG_NAME = re.compile(rb"^[A-Za-z][A-Za-z0-9-]*$")
_NO_ARGUMENT_GLOBALS = frozenset({
    "--no-pager", "--paginate", "--no-optional-locks", "--no-lazy-fetch",
    "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
})
_MAX_METADATA_ENTRIES = 10000
_MAX_METADATA_BYTES = 64 * 1024 * 1024
_MAX_METADATA_DEPTH = 32


class GitHelperView:
    """Own the private files until the corresponding Git process has exited."""

    def __init__(self, temporary: tempfile.TemporaryDirectory[str], env: dict[str, str]) -> None:
        self._temporary = temporary
        self.root = Path(temporary.name).resolve()
        self.env = env

    def cleanup(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> GitHelperView:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()


class GitProbeFailure(ToolFailure):
    """Preserve Git's raw failure without launching the unsafe original argv."""

    def __init__(self, completed: subprocess.CompletedProcess[bytes]) -> None:
        super().__init__("GIT_ERROR", "Git helper configuration or discovery failed.", category="runtime")
        self.completed = completed


def _prefix_and_directory(command: Sequence[str], cwd: Path) -> tuple[list[str], Path]:
    """Reuse only the global options preceding the original builtin command."""
    if not command:
        raise ToolFailure("INVALID_ARGUMENT", "A Git helper command is required.", category="validation")
    prefix = [command[0]]
    directory = cwd
    index = 1
    while index < len(command):
        arg = command[index]
        if not arg.startswith("-"):
            return prefix, directory.resolve()
        if arg in {"-c", "-C"}:
            if index + 1 == len(command):
                break
            value = command[index + 1]
            prefix.extend((arg, value))
            if arg == "-C" and value:
                directory = directory / value
            index += 2
        elif arg.startswith("-C") and len(arg) > 2:
            prefix.append(arg)
            directory = directory / arg[2:]
            index += 1
        elif (arg.startswith("-c") and len(arg) > 2) or arg in _NO_ARGUMENT_GLOBALS:
            prefix.append(arg)
            index += 1
        else:
            raise ToolFailure("INVALID_ARGUMENT", "Unsupported global Git helper option.", category="validation")
    raise ToolFailure("INVALID_ARGUMENT", "A Git builtin command is required.", category="validation")


def _git_failure(completed: subprocess.CompletedProcess[bytes]) -> GitProbeFailure:
    return GitProbeFailure(completed)


def _quote(value: bytes) -> bytes:
    return b'"' + value.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(
        b"\n", b"\\n").replace(b"\t", b"\\t").replace(b"\b", b"\\b") + b'"'


def _configuration_scopes(data: bytes) -> dict[str, bytes]:
    """Preserve value order and protected scopes without replaying includes."""
    fields = data.split(b"\0")
    if not fields or fields[-1] != b"" or (len(fields) - 1) % 2:
        raise ToolFailure("GIT_ERROR", "Invalid Git configuration snapshot.", category="runtime")
    result: dict[str, bytearray] = {scope: bytearray() for scope in ("system", "global", "local")}
    for index in range(0, len(fields) - 1, 2):
        scope_bytes, entry = fields[index:index + 2]
        if scope_bytes not in {b"system", b"global", b"local", b"worktree", b"command"}:
            raise ToolFailure("GIT_ERROR", "Unsupported Git configuration scope.", category="runtime")
        key, separator, value = entry.partition(b"\n")
        lower_key = key.lower()
        if lower_key.startswith((b"filter.", b"include.", b"includeif.")):
            continue
        # Original per-worktree values are flattened below. Git must never
        # reopen the mutable original config.worktree after this snapshot.
        if lower_key == b"extensions.worktreeconfig" or scope_bytes == b"command":
            continue
        section, dot, rest = key.partition(b".")
        subsection, last_dot, variable = rest.rpartition(b".")
        if not last_dot:
            variable = rest
        if not dot or not _CONFIG_NAME.fullmatch(section) or not _CONFIG_NAME.fullmatch(variable):
            raise ToolFailure("GIT_ERROR", "Invalid Git configuration key.", category="runtime")
        header = b"[" + section
        if last_dot:
            header += b" " + _quote(subsection)
        header += b"]\n\t" + variable
        if separator:
            header += b" = " + _quote(value)
        header += b"\n"
        scope = "local" if scope_bytes == b"worktree" else scope_bytes.decode("ascii")
        result[scope].extend(header)
    result["local"].extend(b"[extensions]\n\tworktreeConfig = false\n")
    return {scope: bytes(content) for scope, content in result.items()}


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


class _MetadataBudget:
    def __init__(self) -> None:
        self.entries = 0
        self.bytes = 0

    def add(self, size: int) -> None:
        self.entries += 1
        self.bytes += size
        if self.entries > _MAX_METADATA_ENTRIES or self.bytes > _MAX_METADATA_BYTES:
            raise ToolFailure("GIT_ERROR", "Git helper reference metadata exceeds the private snapshot limit.", category="runtime")


def _metadata_stat(path: Path) -> os.stat_result:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ToolFailure("GIT_ERROR", "Git helper reference metadata contains a symbolic link or reparse point.", category="runtime")
    if not stat.S_ISDIR(value.st_mode) and not stat.S_ISREG(value.st_mode):
        raise ToolFailure("GIT_ERROR", "Git helper reference metadata is not a regular file or directory.", category="runtime")
    return value


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode) == (right.st_dev, right.st_ino, right.st_mode)


def _copy_metadata(source: Path, destination: Path, budget: _MetadataBudget, *, depth: int = 0) -> None:
    """Bound Windows fallback copies, rejecting links and changed handles."""
    if depth > _MAX_METADATA_DEPTH:
        raise ToolFailure("GIT_ERROR", "Git helper reference metadata is nested too deeply.", category="runtime")
    before = _metadata_stat(source)
    budget.add(before.st_size if stat.S_ISREG(before.st_mode) else 0)
    if stat.S_ISDIR(before.st_mode):
        destination.mkdir(mode=0o700)
        with os.scandir(source) as entries:
            for entry in entries:
                _copy_metadata(source / entry.name, destination / entry.name, budget, depth=depth + 1)
                if not _same_file(before, _metadata_stat(source)):
                    raise ToolFailure("GIT_ERROR", "Git helper reference metadata changed during snapshot.", category="runtime")
        return
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as original:
        opened = os.fstat(original.fileno())
        if not _same_file(before, opened) or not _same_file(before, _metadata_stat(source)):
            raise ToolFailure("GIT_ERROR", "Git helper reference metadata changed during snapshot.", category="runtime")
        output_descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(output_descriptor, "wb") as output:
            # A source may grow concurrently; never read beyond its bounded
            # original length, and reject changed sizes instead of truncating.
            remaining = before.st_size
            while remaining:
                chunk = original.read(min(remaining, 65536))
                if not chunk:
                    raise ToolFailure("GIT_ERROR", "Git helper reference metadata changed during snapshot.", category="runtime")
                output.write(chunk)
                remaining -= len(chunk)
            if original.read(1) or os.fstat(original.fileno()).st_size != before.st_size:
                raise ToolFailure("GIT_ERROR", "Git helper reference metadata changed during snapshot.", category="runtime")


def _metadata_reference(
    source: Path, destination: Path, budget: _MetadataBudget, *, directory: bool = False,
) -> None:
    """Reference only data metadata; Windows need not enable symlink privileges."""
    try:
        source.lstat()
    except FileNotFoundError:
        if directory:
            destination.mkdir()
        return
    if os.name != "nt":
        destination.symlink_to(source, target_is_directory=directory)
    else:
        # The object database is never copied. Windows does not need developer
        # mode, symlink privileges, or any persistent host configuration change.
        _copy_metadata(source, destination, budget)


def _protected_only_view(scopes: dict[str, bytes], env: dict[str, str], directory: Path) -> GitHelperView:
    """A no-index diff outside repositories can still use global filters."""
    temporary = tempfile.TemporaryDirectory(prefix="coding-tools-git-view-")
    root = Path(temporary.name).resolve()
    try:
        os.chmod(root, 0o700)
        if root.is_relative_to(directory):
            raise ToolFailure("SANDBOX_UNAVAILABLE", "Private Git configuration must be outside the workspace.", category="security")
        _write_private(root / "system", scopes["system"])
        _write_private(root / "global", scopes["global"])
        return GitHelperView(temporary, {
            **env, "GIT_CONFIG_SYSTEM": str(root / "system"), "GIT_CONFIG_GLOBAL": str(root / "global"),
            "GIT_CONFIG_NOSYSTEM": "0", "GIT_ATTR_NOSYSTEM": "1",
        })
    except BaseException:
        temporary.cleanup()
        raise


def create_git_helper_view(
    command: Sequence[str], env: dict[str, str], *, cwd: Path, run_probe: GitProbe,
    validate_storage: Callable[[Path, Path], None] | None = None,
) -> GitHelperView | None:
    """Create a stable, non-executing config view for one read-only Git launch.

    The caller must pass its hardened Git argv and filtered environment, run
    probes with the same executor/policy, mount ``root`` read-only for this
    helper only, and keep the view alive until the process exits. A genuine
    non-repository receives a protected-config view without a fabricated
    repository. Malformed/unreadable configurations fail closed instead of
    retrying the original filter-enabled command.
    """
    prefix, directory = _prefix_and_directory(command, cwd)
    probe_env = {**env, "LC_ALL": "C"}
    discovered = run_probe([*prefix, "rev-parse", "--absolute-git-dir", "--git-common-dir",
                            "--is-bare-repository", "--is-inside-work-tree"], probe_env)
    if discovered.returncode:
        if b"not a git repository" not in discovered.stderr.lower():
            # Preserve native discovery diagnostics, including protected-file
            # read failures whose wording differs from `git config --list`.
            raise _git_failure(discovered)
    # Config's builtin listing does not run repository filters or hooks. It
    # expands conditional includes in the original repository and checks its
    # protected configuration before any private paths are introduced.
    snapshot = run_probe([*prefix, "config", "--includes", "--null", "--list", "--show-scope"], dict(env))
    if snapshot.returncode:
        raise _git_failure(snapshot)
    scopes = _configuration_scopes(snapshot.stdout)
    if discovered.returncode:
        return _protected_only_view(scopes, env, directory)
    fields = discovered.stdout.rstrip(b"\n").split(b"\n")
    if len(fields) != 4 or fields[2] not in {b"true", b"false"} or fields[3] not in {b"true", b"false"}:
        raise ToolFailure("GIT_ERROR", "Invalid Git repository discovery result.", category="runtime")
    git_dir = (directory / os.fsdecode(fields[0])).resolve()
    common = (directory / os.fsdecode(fields[1])).resolve()
    if validate_storage is not None:
        validate_storage(git_dir, common)
    worktree: str | None = None
    if fields[3] == b"true":
        top = run_probe([*prefix, "rev-parse", "--show-toplevel"], probe_env)
        if top.returncode:
            raise _git_failure(top)
        worktree = os.fsdecode(top.stdout.rstrip(b"\n"))
    temporary = tempfile.TemporaryDirectory(prefix="coding-tools-git-view-")
    root = Path(temporary.name).resolve()
    try:
        os.chmod(root, 0o700)
        if root.is_relative_to(directory) or worktree and root.is_relative_to(Path(worktree)):
            raise ToolFailure("SANDBOX_UNAVAILABLE", "Private Git configuration must be outside the workspace.", category="security")
        common_view = root / "common"
        common_view.mkdir(mode=0o700)
        _write_private(root / "system", scopes["system"])
        _write_private(root / "global", scopes["global"])
        _write_private(common_view / "config", scopes["local"])
        budget = _MetadataBudget()
        for name in ("refs", "logs", "worktrees", "reftable"):
            _metadata_reference(common / name, common_view / name, budget, directory=True)
        for name in ("packed-refs", "shallow"):
            _metadata_reference(common / name, common_view / name, budget)
        info = common_view / "info"
        info.mkdir(mode=0o700)
        # Never reference the whole original info directory: its attributes
        # are mutable, and are always read even with GIT_ATTR_SOURCE set.
        for name in ("exclude", "grafts"):
            _metadata_reference(common / "info" / name, info / name, budget)
        clean_env = {
            **env, "GIT_DIR": str(git_dir), "GIT_COMMON_DIR": str(common_view),
            "GIT_OBJECT_DIRECTORY": str(common / "objects"),
            "GIT_CONFIG_SYSTEM": str(root / "system"), "GIT_CONFIG_GLOBAL": str(root / "global"),
            "GIT_CONFIG_NOSYSTEM": "0", "GIT_ATTR_NOSYSTEM": "1",
            # Explicit GIT_DIR pins discovery without turning a bare Git
            # directory into an implicit worktree at the current directory.
            "GIT_IMPLICIT_WORK_TREE": "0",
        }
        if worktree is not None:
            # GIT_COMMON_DIR intentionally ignores core.worktree in common
            # config. Retain the root Git found in its original trusted view.
            clean_env["GIT_WORK_TREE"] = worktree
        return GitHelperView(temporary, clean_env)
    except BaseException:
        temporary.cleanup()
        raise
