"""Handle-relative structured file access for opt-in strict isolation.

Every content access walks from a pinned workspace descriptor with O_NOFOLLOW.
A validated pathname is never handed back to the host for a later open. Directory
renames cannot redirect an already opened descriptor to a symlink's destination.
The workspace descriptor is the authorization anchor for its lifetime; host-side
renames do not turn that descriptor into a new authority.

This is confinement, not a filesystem transaction against an adversary that can
concurrently edit the same files. Patch revision checks remain optimistic; each
replacement is atomic and rollback is best effort. Strict Windows is deliberately
unavailable until a native handle-relative/reparse-point broker is implemented.
"""
from __future__ import annotations

import errno
import hashlib
import io
import os
import secrets
import stat
import sys
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, TextIO

from .errors import ToolFailure
from .patching import FileBaseline, StagedFile


@dataclass(frozen=True)
class BrokerEntry:
    display: str
    stat: os.stat_result

    @property
    def name(self) -> str:
        return PurePosixPath(self.display).name

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.stat.st_mode)

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.stat.st_mode)


@dataclass
class _PreparedChange:
    change: StagedFile
    display: str
    parent_fd: int
    name: str
    temporary: str | None = None
    backup: str | None = None
    installed: bool = False


def broker_supported() -> bool:
    """Report implemented mechanisms, without implying native platform testing."""
    return (
        sys.platform in {"linux", "darwin"}
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and all(fn in os.supports_dir_fd for fn in (os.open, os.stat, os.mkdir, os.unlink, os.rmdir, os.rename))
        and os.listdir in os.supports_fd
    )


class FileBroker:
    """Strict, workspace-scoped file broker; no compatibility fallback exists.

    ``denied_roots`` is shared with the execution policy. Denial overrides read
    and write allowances, including directory listings. Regular hard-linked
    files, symlinks, devices, sockets and FIFOs are denied. Configured roots must
    use their canonical, non-symlink spelling. Call close when the runtime closes.
    """

    def __init__(
        self,
        root: Path,
        *,
        denied_roots: Sequence[Path] = (),
        writable_roots: Sequence[Path] | None = None,
    ) -> None:
        if not broker_supported():
            raise ToolFailure(
                "SANDBOX_UNAVAILABLE",
                "Strict structured file access requires a supported handle-relative broker; "
                "the Windows reparse-point broker is not implemented.",
                category="security",
                details={"capability": "race_resistant_file_broker", "platform": sys.platform},
            )
        self.root = Path(os.path.abspath(root.expanduser()))
        self.denied_roots = tuple(self._absolute_root(path) for path in denied_roots)
        self.writable_roots = tuple(
            self._absolute_root(path) for path in ((self.root,) if writable_roots is None else writable_roots)
        )
        self._denied_ids: set[tuple[int, int]] = set()
        self._root_fd = -1
        self._root_fd = self._open_absolute_directory(self.root)
        try:
            # Pin the identity as well as the spelling of each existing denied
            # root, so renaming a denied directory does not evade the rule.
            for path in self.denied_roots:
                try:
                    info = os.stat(path, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                self._denied_ids.add((info.st_dev, info.st_ino))
            self._check_path(".", write=False)
            self._check_inode(os.fstat(self._root_fd), ".", directory=True)
        except BaseException:
            self.close()
            raise

    def _absolute_root(self, path: Path) -> Path:
        expanded = path.expanduser()
        return Path(os.path.abspath(expanded if expanded.is_absolute() else self.root / expanded))

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    @classmethod
    def _open_absolute_directory(cls, path: Path) -> int:
        fd = os.open(path.anchor, cls._directory_flags())
        try:
            for part in path.parts[1:]:
                child = os.open(part, cls._directory_flags(), dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> FileBroker:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def normalize(self, path: str | Path) -> str:
        """Validate spelling only; all actual operations independently recheck."""
        if isinstance(path, Path):
            if path.is_absolute():
                try:
                    path = path.relative_to(self.root)
                except ValueError as exc:
                    raise self._denied("PATH_OUTSIDE_WORKSPACE", str(path)) from exc
            raw = path.as_posix()
        else:
            raw = path
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ToolFailure("INVALID_ARGUMENT", "Path must be nonempty and contain no NUL.", category="validation")
        # Backslashes and drive-relative paths are ambiguous across supported
        # hosts. Reject rather than treating a Windows escape as a POSIX name.
        windows = PureWindowsPath(raw)
        if raw.startswith("/") or windows.drive or windows.root or "\\" in raw:
            raise self._denied("ABSOLUTE_PATH_DENIED", raw)
        pure = PurePosixPath(raw)
        if ".." in pure.parts:
            raise self._denied("PATH_OUTSIDE_WORKSPACE", raw)
        return pure.as_posix()

    @staticmethod
    def _under(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def _check_path(self, display: str, *, write: bool) -> None:
        absolute = self.root / display
        if any(self._under(absolute, denied) for denied in self.denied_roots):
            raise self._denied("PATH_DENIED", display)
        if write and not any(self._under(absolute, allowed) for allowed in self.writable_roots):
            raise self._denied("WRITE_DENIED", display)

    @staticmethod
    def _denied(code: str, display: str) -> ToolFailure:
        return ToolFailure(code, f"Strict file policy denied path: {display}", category="security", details={"path": display})

    def _check_inode(self, info: os.stat_result, display: str, *, directory: bool = False) -> None:
        if (info.st_dev, info.st_ino) in self._denied_ids:
            raise self._denied("PATH_DENIED", display)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise self._denied("SYMLINK_ESCAPE", display)
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ToolFailure("NOT_A_DIRECTORY", f"Not a directory: {display}", category="validation")
        elif not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise self._denied("UNSUPPORTED_FILE_TYPE", display)
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise self._denied("HARDLINK_DENIED", display)

    def _duplicate_root(self) -> int:
        if self._root_fd < 0:
            raise ToolFailure("SANDBOX_UNAVAILABLE", "Structured file broker is closed.", category="security")
        return os.dup(self._root_fd)

    def _open_directory(self, display: str, *, create: bool = False, created: list[tuple[int, str]] | None = None) -> int:
        fd = self._duplicate_root()
        prefix: list[str] = []
        try:
            for part in PurePosixPath(display).parts:
                prefix.append(part)
                current = "/".join(prefix)
                self._check_path(current, write=False)
                try:
                    child = os.open(part, self._directory_flags(), dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    self._check_path(current, write=True)
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                    else:
                        if created is not None:
                            created.append((os.dup(fd), part))
                    child = os.open(part, self._directory_flags(), dir_fd=fd)
                try:
                    self._check_inode(os.fstat(child), current, directory=True)
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            return fd
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise self._denied("SYMLINK_ESCAPE", display) from exc
            raise
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _parent(self, path: str | Path, *, write: bool = False) -> Iterator[tuple[str, int, str]]:
        display = self.normalize(path)
        self._check_path(display, write=write)
        pure = PurePosixPath(display)
        fd = self._open_directory(pure.parent.as_posix())
        try:
            yield display, fd, pure.name or "."
        finally:
            os.close(fd)

    def _open_file_at(self, parent: int, name: str, display: str) -> int:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise self._denied("SYMLINK_ESCAPE", display) from exc
            raise
        try:
            info = os.fstat(fd)
            self._check_inode(info, display)
            if stat.S_ISDIR(info.st_mode):
                raise ToolFailure("IS_DIRECTORY", f"Path is a directory: {display}", category="validation")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def open_binary(self, path: str | Path) -> BinaryIO:
        with self._parent(path) as (display, parent, name):
            fd = self._open_file_at(parent, name, display)
        return os.fdopen(fd, "rb")

    def open_text(
        self, path: str | Path, *, encoding: str = "utf-8", errors: str = "strict", newline: str | None = ""
    ) -> TextIO:
        return io.TextIOWrapper(self.open_binary(path), encoding=encoding, errors=errors, newline=newline)

    def read_bytes(self, path: str | Path) -> bytes:
        with self.open_binary(path) as handle:
            return handle.read()

    def read_text(self, path: str | Path, *, encoding: str = "utf-8", errors: str = "strict") -> str:
        with self.open_text(path, encoding=encoding, errors=errors) as handle:
            return handle.read()

    def stat(self, path: str | Path) -> os.stat_result:
        with self._parent(path) as (display, parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            self._check_inode(info, display)
            return info

    def exists(self, path: str | Path) -> bool:
        try:
            self.stat(path)
            return True
        except FileNotFoundError:
            return False

    def is_dir(self, path: str | Path) -> bool:
        try:
            return stat.S_ISDIR(self.stat(path).st_mode)
        except FileNotFoundError:
            return False

    def is_file(self, path: str | Path) -> bool:
        try:
            return stat.S_ISREG(self.stat(path).st_mode)
        except FileNotFoundError:
            return False

    def ensure_directory(self, path: str | Path) -> None:
        """Create an authorized directory tree through pinned directory handles."""
        display = self.normalize(path)
        self._check_path(display, write=True)
        fd = self._open_directory(display, create=True)
        os.close(fd)

    def _entries_at(self, fd: int, display: str) -> list[BrokerEntry]:
        entries: list[BrokerEntry] = []
        for name in sorted(os.listdir(fd)):
            child_display = (PurePosixPath(display) / name).as_posix()
            try:
                self._check_path(child_display, write=False)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                self._check_inode(info, child_display)
            except (FileNotFoundError, ToolFailure):
                continue
            entries.append(BrokerEntry(child_display, info))
        return entries

    def list_dir(self, path: str | Path = ".") -> list[BrokerEntry]:
        display = self.normalize(path)
        self._check_path(display, write=False)
        fd = self._open_directory(display)
        try:
            return self._entries_at(fd, display)
        finally:
            os.close(fd)

    def walk_files(
        self, path: str | Path = ".", *, excluded_directories: Collection[str] = (), max_depth: int | None = None,
    ) -> Iterator[str]:
        """Yield safe names, pruning before opening excluded or deep directories.

        Depth is relative to ``path``: zero includes only that directory's
        files. Consumers must still open yielded files through the broker.
        """
        display = self.normalize(path)
        self._check_path(display, write=False)
        excluded = frozenset(excluded_directories)
        # Queue names rather than open descriptors: broad directories must not
        # consume one service descriptor per child before yielding any files.
        pending = [(display, 0)]
        while pending:
            current, depth = pending.pop()
            try:
                fd = self._open_directory(current)
            except (FileNotFoundError, ToolFailure):
                if current == display:
                    raise
                # Descendants may disappear or become unsafe after listing.
                # Reopening from the pinned root rechecks every component.
                continue
            try:
                for entry in self._entries_at(fd, current):
                    if entry.is_file:
                        yield entry.display
                    elif (entry.is_dir and entry.name not in excluded
                          and (max_depth is None or depth < max_depth)):
                        pending.append((entry.display, depth + 1))
            finally:
                os.close(fd)

    def _baseline_at(self, parent: int, name: str, display: str) -> FileBaseline:
        try:
            fd = self._open_file_at(parent, name, display)
        except FileNotFoundError:
            return FileBaseline(data=None, mode=None, digest=None)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            data = handle.read()
            # Reject hard links introduced while the descriptor was read too.
            self._check_inode(os.fstat(handle.fileno()), display)
        return FileBaseline(data=data, mode=stat.S_IMODE(info.st_mode), digest=hashlib.sha256(data).hexdigest())

    def capture_baseline(self, path: str | Path) -> FileBaseline:
        try:
            with self._parent(path) as (display, parent, name):
                return self._baseline_at(parent, name, display)
        except FileNotFoundError:
            return FileBaseline(data=None, mode=None, digest=None)

    def _assert_baseline(self, item: _PreparedChange) -> None:
        current = self._baseline_at(item.parent_fd, item.name, item.display)
        if current.digest == item.change.baseline.digest and current.mode == item.change.baseline.mode:
            return
        raise ToolFailure(
            "PATCH_CONFLICT", f"File changed while the patch was being prepared: {item.display}",
            category="conflict", retryable=True,
            details={"path": item.display, "retry_hint": "Read the current file and regenerate the patch."},
        )

    @staticmethod
    def _sync_directory(fd: int) -> None:
        try:
            os.fsync(fd)
        except OSError as exc:
            # Some filesystems do not implement directory fsync.
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                raise

    @staticmethod
    def _reserve(parent: int, prefix: str) -> tuple[int, str]:
        for _ in range(32):
            name = prefix + secrets.token_hex(16)
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent)
            except FileExistsError:
                continue
            return fd, name
        raise ToolFailure("PATCH_FAILED", "Could not reserve a unique patch temporary file.", category="internal")

    @staticmethod
    def _unlink_at(parent: int, name: str | None) -> None:
        if name is None:
            return
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass

    def commit(self, changes: list[StagedFile]) -> None:
        """Commit with pinned parent handles and never follow replacement links.

        All permissions are checked before creating anything. Existing public
        patch semantics (atomic replacement, optimistic baselines, rollback) are
        retained, but none of the legacy pathname-based committer is used.
        """
        displays = [self.normalize(change.path) for change in changes]
        if len(displays) != len(set(displays)):
            raise ToolFailure("PATCH_FAILED", "Patch staged the same path more than once.", category="validation")
        for change, display in zip(changes, displays):
            self._check_path(display, write=change.action != "verify")
            if display == ".":
                raise ToolFailure("PATCH_FAILED", "Cannot patch the workspace directory.", category="validation")
        items: list[_PreparedChange] = []
        created: list[tuple[int, str]] = []
        preserve_backups = False
        succeeded = False
        try:
            for change, display in zip(changes, displays):
                pure = PurePosixPath(display)
                parent = self._open_directory(pure.parent.as_posix(), create=change.action == "write", created=created)
                item = _PreparedChange(change, display, parent, pure.name)
                items.append(item)
                if change.action == "write":
                    fd, item.temporary = self._reserve(parent, ".coding-tools-patch-")
                    with os.fdopen(fd, "wb") as handle:
                        handle.write((change.content or "").encode("utf-8"))
                        handle.flush()
                        os.fchmod(handle.fileno(), change.mode if change.mode is not None else 0o644)
                        os.fsync(handle.fileno())
            for item in items:
                self._assert_baseline(item)
            for item in items:
                if item.change.action == "verify":
                    continue
                self._assert_baseline(item)
                if item.change.baseline.data is not None:
                    fd, backup = self._reserve(item.parent_fd, ".coding-tools-backup-")
                    os.close(fd)
                    try:
                        os.rename(item.name, backup, src_dir_fd=item.parent_fd, dst_dir_fd=item.parent_fd)
                    except BaseException:
                        self._unlink_at(item.parent_fd, backup)
                        raise
                    item.backup = backup
                    self._sync_directory(item.parent_fd)
            for item in items:
                if item.temporary is not None:
                    if item.backup is None:
                        self._assert_baseline(item)
                    os.rename(item.temporary, item.name, src_dir_fd=item.parent_fd, dst_dir_fd=item.parent_fd)
                    item.temporary = None
                    item.installed = True
                    self._sync_directory(item.parent_fd)
            succeeded = True
        except BaseException as exc:
            errors: list[str] = []
            for item in reversed(items):
                try:
                    if item.installed:
                        self._unlink_at(item.parent_fd, item.name)
                    if item.backup is not None:
                        os.rename(item.backup, item.name, src_dir_fd=item.parent_fd, dst_dir_fd=item.parent_fd)
                        item.backup = None
                        self._sync_directory(item.parent_fd)
                except OSError as rollback_error:
                    errors.append(f"{item.display}: {rollback_error}")
            if errors:
                preserve_backups = True
                raise ToolFailure(
                    "PATCH_ROLLBACK_FAILED", "Patch failed and recovery backups were preserved.", category="internal",
                    details={"rollback_errors": errors, "recovery_backups": {
                        item.display: (PurePosixPath(item.display).parent / item.backup).as_posix()
                        for item in items if item.backup is not None
                    }, "cause": str(exc)},
                ) from exc
            raise
        finally:
            for item in items:
                try:
                    try:
                        self._unlink_at(item.parent_fd, item.temporary)
                        if not preserve_backups:
                            self._unlink_at(item.parent_fd, item.backup)
                    except OSError:
                        pass
                finally:
                    os.close(item.parent_fd)
            for fd, name in reversed(created):
                try:
                    if not succeeded:
                        try:
                            os.rmdir(name, dir_fd=fd)
                        except OSError:
                            pass
                finally:
                    os.close(fd)
