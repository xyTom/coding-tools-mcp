from __future__ import annotations

import argparse
import base64
import difflib
import fnmatch
import http.server
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "codex-tool-runtime-mcp"
DEFAULT_EXCLUDED_NAMES = {
    ".git",
    ".reference",
    "node_modules",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
SENSITIVE_ENV_RE = re.compile(r"(token|secret|credential|api[_-]?key|password|passwd|private)", re.I)
NETWORK_RE = re.compile(
    r"(https?://|urllib\.request|requests\.|socket\.|curl\b|wget\b|nc\b|netcat\b|ssh\b|scp\b|ftp\b)",
    re.I,
)
DESTRUCTIVE_RE = re.compile(
    r"(^|\s)(sudo|su|chmod\s+-R|chown\s+-R|git\s+reset\s+--hard|git\s+clean\s+-fdx|mkfs|mount|umount)\b",
    re.I,
)


class ToolFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "runtime",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.retryable = retryable
        self.details = details or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_response_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def truncate_bytes(data: bytes, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        limit = 1
    truncated = len(data) > limit
    if truncated:
        if limit > 64:
            head = max(1, limit // 2)
            tail = max(1, limit - head)
            data = data[:head] + b"\n... output truncated ...\n" + data[-tail:]
        else:
            data = data[:limit]
    return data.decode("utf-8", errors="replace"), truncated


def normalize_rel_display(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix()
    text = rel.as_posix()
    return "." if text == "" else text


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass
class ResolvedPath:
    display: str
    path: Path
    existed: bool


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ToolFailure("INVALID_ARGUMENT", "Workspace root must be a directory.", category="validation")
        if str(self.root) in {"/", str(Path.home().resolve())}:
            raise ToolFailure("INVALID_ARGUMENT", "Unsafe workspace root rejected.", category="security")

    def _reject_unsafe_text(self, raw_path: str) -> PurePosixPath:
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolFailure("INVALID_ARGUMENT", "Path must be a non-empty string.", category="validation")
        if "\x00" in raw_path:
            raise ToolFailure("INVALID_ARGUMENT", "Path contains a NUL byte.", category="validation")
        if raw_path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", raw_path):
            raise ToolFailure("ABSOLUTE_PATH_DENIED", "Absolute paths are denied.", category="security")
        pure = PurePosixPath(raw_path)
        if any(part == ".." for part in pure.parts):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
        return pure

    def resolve_existing(self, raw_path: str = ".") -> ResolvedPath:
        pure = self._reject_unsafe_text(raw_path or ".")
        candidate = self.root.joinpath(*pure.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("NOT_FOUND", f"Path not found: {raw_path}", category="not_found") from exc
        if not is_relative_to(resolved, self.root):
            code = "SYMLINK_ESCAPE" if candidate.is_symlink() else "PATH_OUTSIDE_WORKSPACE"
            raise ToolFailure(code, "Path escapes the configured workspace.", category="security")
        return ResolvedPath(normalize_rel_display(resolved, self.root), resolved, True)

    def resolve_for_write(self, raw_path: str) -> ResolvedPath:
        pure = self._reject_unsafe_text(raw_path)
        if pure.name in {"", ".", ".."}:
            raise ToolFailure("INVALID_ARGUMENT", "Invalid write target.", category="validation")
        candidate = self.root.joinpath(*pure.parts)
        if candidate.exists() or candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            if not is_relative_to(resolved, self.root):
                raise ToolFailure("SYMLINK_ESCAPE", "Path escapes the configured workspace.", category="security")
            return ResolvedPath(normalize_rel_display(resolved, self.root), resolved, True)

        parent = candidate.parent
        missing: list[Path] = []
        while not parent.exists():
            missing.append(parent)
            if parent == self.root or parent.parent == parent:
                break
            parent = parent.parent
        try:
            resolved_parent = parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("NOT_FOUND", f"Parent directory not found: {raw_path}", category="not_found") from exc
        if not is_relative_to(resolved_parent, self.root):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
        target = resolved_parent.joinpath(*reversed([p.name for p in missing]), candidate.name)
        return ResolvedPath(normalize_rel_display(target, self.root), target, False)

    def reject_write_symlink(self, raw_path: str) -> None:
        pure = self._reject_unsafe_text(raw_path)
        candidate = self.root.joinpath(*pure.parts)
        if candidate.is_symlink():
            raise ToolFailure("SYMLINK_ESCAPE", "Writing through symlinks is denied.", category="security")

    def is_ignored_path(self, path: Path, *, include_hidden: bool = False, include_ignored: bool = False) -> bool:
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return True
        parts = rel.parts
        if not include_hidden and any(part.startswith(".") for part in parts if part not in {".", ""}):
            return True
        if not include_ignored and any(part in DEFAULT_EXCLUDED_NAMES for part in parts):
            return True
        if include_ignored:
            return False
        if self._git_ignored(rel.as_posix()):
            return True
        return False

    def is_safe_existing_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            return False
        return is_relative_to(resolved, self.root)

    def _git_ignored(self, rel_path: str) -> bool:
        git = shutil.which("git")
        if not git:
            return False
        try:
            completed = subprocess.run(
                [git, "-C", str(self.root), "check-ignore", "-q", "--", rel_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            return False
        return completed.returncode == 0


@dataclass
class ExecSession:
    session_id: str
    process: subprocess.Popen[bytes]
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_at: float = field(default_factory=time.time)
    closed: bool = False
    exit_code: int | None = None
    signal_name: str | None = None

    def append_stdout(self, chunk: bytes) -> None:
        with self.lock:
            self.stdout.extend(chunk)

    def append_stderr(self, chunk: bytes) -> None:
        with self.lock:
            self.stderr.extend(chunk)

    def snapshot_since_cursor(self, max_output_bytes: int) -> dict[str, Any]:
        self.refresh_status()
        with self.lock:
            stdout_bytes = bytes(self.stdout[self.stdout_cursor :])
            stderr_bytes = bytes(self.stderr[self.stderr_cursor :])
            self.stdout_cursor = len(self.stdout)
            self.stderr_cursor = len(self.stderr)
        stdout, stdout_truncated = truncate_bytes(stdout_bytes, max_output_bytes)
        stderr, stderr_truncated = truncate_bytes(stderr_bytes, max_output_bytes)
        status = "running" if self.process.poll() is None else "exited"
        return {
            "session_id": self.session_id,
            "status": status,
            "exit_code": self.exit_code,
            "signal": self.signal_name,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "truncated": stdout_truncated or stderr_truncated,
            "ok": True,
        }

    def refresh_status(self) -> None:
        code = self.process.poll()
        if code is None:
            return
        self.exit_code = code
        if code < 0:
            self.signal_name = signal.Signals(-code).name if -code in [s.value for s in signal.Signals] else str(-code)
        self.closed = True


class Runtime:
    def __init__(self, workspace: Path, *, enable_view_image: bool = False) -> None:
        self.workspace = Workspace(workspace)
        self.enable_view_image = enable_view_image
        self.sessions: dict[str, ExecSession] = {}
        self.sessions_lock = threading.Lock()
        self.http_session_id = secrets.token_urlsafe(24)
        self.patch_baselines: dict[str, str | None] = {}

    def initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}, "logging": {}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "Codex Tool Runtime MCP",
                "version": __version__,
            },
            "instructions": "Use these tools only for local coding runtime operations inside the configured workspace.",
        }

    def list_tools(self) -> dict[str, Any]:
        names = [
            "read_file",
            "list_dir",
            "list_files",
            "search_text",
            "apply_patch",
            "exec_command",
            "write_stdin",
            "kill_session",
            "git_status",
            "git_diff",
            "request_permissions",
        ]
        if self.enable_view_image:
            names.append("view_image")
        return {"tools": [tool_definition(name) for name in names]}

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments or {}
        handlers = {
            "read_file": self.read_file,
            "list_dir": self.list_dir,
            "list_files": self.list_files,
            "search_text": self.search_text,
            "apply_patch": self.apply_patch,
            "exec_command": self.exec_command,
            "write_stdin": self.write_stdin,
            "kill_session": self.kill_session,
            "git_status": self.git_status,
            "git_diff": self.git_diff,
            "request_permissions": self.request_permissions,
        }
        if self.enable_view_image:
            handlers["view_image"] = self.view_image
        handler = handlers.get(name)
        if handler is None:
            raise JsonRpcError(-32602, f"Unknown tool: {name}", {"reason": "unknown_tool"})
        try:
            payload = handler(args)
            payload.setdefault("ok", True)
            return tool_result(payload, is_error=False)
        except ToolFailure as exc:
            payload = {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "category": exc.category,
                    "retryable": exc.retryable,
                    "details": exc.details,
                },
            }
            return tool_result(payload, is_error=True)
        except Exception as exc:  # noqa: BLE001 - tool failures must stay structured
            payload = {
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
                    "category": "internal",
                    "retryable": False,
                    "details": {},
                },
            }
            return tool_result(payload, is_error=True)

    def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.workspace.resolve_existing(str(args.get("path", "")))
        if resolved.path.is_dir():
            raise ToolFailure("IS_DIRECTORY", "Path is a directory.", category="validation")
        max_bytes = int(args.get("max_bytes", 131072))
        start_line = int(args.get("start_line", 1))
        end_line = args.get("end_line")
        encoding = args.get("encoding", "utf-8")
        if encoding != "utf-8":
            raise ToolFailure("UNSUPPORTED_ENCODING", "Only utf-8 is supported.", category="validation")
        data = resolved.path.read_bytes()
        if b"\x00" in data[:4096]:
            raise ToolFailure("BINARY_FILE", "Binary file read blocked for text tool.", category="validation")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolFailure("UNSUPPORTED_ENCODING", "File is not valid utf-8.", category="validation") from exc
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        if start_line < 1:
            raise ToolFailure("INVALID_ARGUMENT", "start_line must be >= 1.", category="validation")
        end = int(end_line) if end_line is not None else total_lines
        if end < start_line:
            selected = ""
        else:
            selected = "".join(lines[start_line - 1 : end])
        encoded = selected.encode("utf-8")
        truncated = len(encoded) > max_bytes
        if truncated:
            selected = encoded[:max_bytes].decode("utf-8", errors="replace")
        actual_end = min(end, total_lines)
        return {
            "path": resolved.display,
            "content": selected,
            "encoding": "utf-8",
            "start_line": start_line,
            "end_line": actual_end,
            "total_lines": total_lines,
            "bytes_read": len(selected.encode("utf-8")),
            "truncated": truncated,
            "warnings": ["content truncated"] if truncated else [],
        }

    def list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.workspace.resolve_existing(str(args.get("path", ".")))
        if not resolved.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "Path is not a directory.", category="validation")
        recursive = bool(args.get("recursive", False))
        max_depth = int(args.get("max_depth", 1))
        max_entries = int(args.get("max_entries", 1000))
        include_hidden = bool(args.get("include_hidden", False))
        include_ignored = bool(args.get("include_ignored", False))
        sort_key = args.get("sort", "name")
        entries: list[dict[str, Any]] = []
        truncated = False

        def visit(directory: Path, depth: int) -> None:
            nonlocal truncated
            if truncated:
                return
            try:
                children = list(directory.iterdir())
            except OSError:
                return
            for child in children:
                if self.workspace.is_ignored_path(child, include_hidden=include_hidden, include_ignored=include_ignored):
                    continue
                entries.append(entry_for_path(child, self.workspace.root))
                if len(entries) >= max_entries:
                    truncated = True
                    return
                if recursive and depth < max_depth and child.is_dir() and not child.is_symlink():
                    visit(child, depth + 1)

        visit(resolved.path, 1)
        entries.sort(key=lambda item: sort_value(item, sort_key))
        return {
            "path": resolved.display,
            "entries": entries,
            "truncated": truncated,
            "warnings": ["entry limit reached"] if truncated else [],
        }

    def list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.workspace.resolve_existing(str(args.get("path", ".")))
        if not resolved.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "Path is not a directory.", category="validation")
        patterns_arg = args.get("patterns")
        glob_arg = args.get("glob")
        if isinstance(patterns_arg, list) and patterns_arg:
            patterns = [str(item) for item in patterns_arg]
        elif isinstance(glob_arg, str) and glob_arg:
            patterns = [glob_arg]
        else:
            patterns = ["**/*"]
        exclude_patterns = [str(item) for item in args.get("exclude_patterns", [])]
        include_hidden = bool(args.get("include_hidden", False))
        include_ignored = bool(args.get("include_ignored", False))
        max_results = int(args.get("max_results", 5000))
        files: list[dict[str, Any]] = []
        truncated = False
        for path in walk_files(resolved.path):
            if path.is_symlink() and not self.workspace.is_safe_existing_path(path):
                continue
            if self.workspace.is_ignored_path(path, include_hidden=include_hidden, include_ignored=include_ignored):
                continue
            rel = normalize_rel_display(path, self.workspace.root)
            if not any(fnmatch.fnmatch(rel, pattern) or PurePosixPath(rel).match(pattern) for pattern in patterns):
                continue
            if any(fnmatch.fnmatch(rel, pattern) or PurePosixPath(rel).match(pattern) for pattern in exclude_patterns):
                continue
            stat = path.lstat()
            files.append(
                {
                    "path": rel,
                    "type": "symlink" if path.is_symlink() else "file",
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            )
            if len(files) >= max_results:
                truncated = True
                break
        files.sort(key=lambda item: item["modified"] if args.get("sort") == "modified" else item["path"])
        return {
            "path": resolved.display,
            "files": files,
            "truncated": truncated,
            "warnings": ["result limit reached"] if truncated else [],
        }

    def search_text(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", ""))
        if not query:
            raise ToolFailure("INVALID_ARGUMENT", "query is required.", category="validation")
        resolved = self.workspace.resolve_existing(str(args.get("path", ".")))
        regex = bool(args.get("regex", False))
        case_sensitive = bool(args.get("case_sensitive", False))
        include_globs = [str(item) for item in args.get("include_globs", [])]
        if isinstance(args.get("glob"), str):
            include_globs.append(str(args["glob"]))
        exclude_globs = [str(item) for item in args.get("exclude_globs", [])]
        context_lines = int(args.get("context_lines", 0))
        max_results = int(args.get("max_results", 1000))
        max_preview_bytes = int(args.get("max_preview_bytes", 512))
        matches: list[dict[str, Any]] = []
        total = 0
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(query, flags) if regex else None

        roots = [resolved.path] if resolved.path.is_file() else walk_files(resolved.path)
        for path in roots:
            if path.is_dir() or self.workspace.is_ignored_path(path):
                continue
            if path.is_symlink() and not self.workspace.is_safe_existing_path(path):
                continue
            rel = normalize_rel_display(path, self.workspace.root)
            if include_globs and not any(fnmatch.fnmatch(rel, pat) or PurePosixPath(rel).match(pat) for pat in include_globs):
                continue
            if any(fnmatch.fnmatch(rel, pat) or PurePosixPath(rel).match(pat) for pat in exclude_globs):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            try:
                lines = data.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines):
                found = compiled.search(line) if compiled else find_literal(line, query, case_sensitive)
                if not found:
                    continue
                total += 1
                if len(matches) >= max_results:
                    continue
                column = found.start() + 1 if hasattr(found, "start") else 1
                preview_bytes = line.encode("utf-8")
                preview, _ = truncate_bytes(preview_bytes, max_preview_bytes)
                before = lines[max(0, index - context_lines) : index]
                after = lines[index + 1 : index + 1 + context_lines]
                matches.append(
                    {
                        "path": rel,
                        "line": index + 1,
                        "column": column,
                        "preview": preview,
                        "before": before,
                        "after": after,
                    }
                )
        return {
            "query": query,
            "matches": matches,
            "total_matches": total,
            "truncated": total > len(matches),
            "warnings": ["result limit reached"] if total > len(matches) else [],
        }

    def apply_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        patch = str(args.get("patch", ""))
        dry_run = bool(args.get("dry_run", False))
        operations = parse_patch(patch)
        staged: dict[str, str | None] = {}
        summaries: list[str] = []
        affected: list[dict[str, str]] = []
        for op in operations:
            self._validate_patch_path(op.path, require_existing=op.kind in {"update", "delete"})
            if op.kind in {"update", "delete"}:
                self.workspace.reject_write_symlink(op.path)
            if op.move_to:
                self._validate_patch_path(op.move_to, require_existing=False)
                self.workspace.reject_write_symlink(op.move_to)
            if op.kind == "add":
                target = self.workspace.resolve_for_write(op.path)
                target.path.parent.mkdir(parents=True, exist_ok=True) if dry_run else None
                if target.path.exists() and target.path.is_dir():
                    raise ToolFailure("PATCH_FAILED", "Cannot add file over a directory.", category="validation")
                staged[target.display] = op.add_content or ""
                affected.append({"path": target.display, "operation": "add"})
                summaries.append(f"A {target.display}")
            elif op.kind == "delete":
                target = self.workspace.resolve_existing(op.path)
                if target.path.is_dir():
                    raise ToolFailure("PATCH_FAILED", "Cannot delete a directory.", category="validation")
                staged[target.display] = None
                affected.append({"path": target.display, "operation": "delete"})
                summaries.append(f"D {target.display}")
            elif op.kind == "update":
                source = self.workspace.resolve_existing(op.path)
                if source.path.is_dir():
                    raise ToolFailure("PATCH_FAILED", "Cannot update a directory.", category="validation")
                current = staged.get(source.display)
                if current is None and source.display in staged:
                    raise ToolFailure("PATCH_FAILED", "Cannot update a deleted file.", category="validation")
                content = current if isinstance(current, str) else source.path.read_text(encoding="utf-8")
                updated = apply_update_hunks(content, op.hunks)
                if op.move_to:
                    dest = self.workspace.resolve_for_write(op.move_to)
                    staged[source.display] = None
                    staged[dest.display] = updated
                    affected.append({"path": dest.display, "old_path": source.display, "operation": "move"})
                    summaries.append(f"R {source.display} -> {dest.display}")
                else:
                    staged[source.display] = updated
                    affected.append({"path": source.display, "operation": "update"})
                    summaries.append(f"M {source.display}")
        if not affected:
            raise ToolFailure("PATCH_FAILED", "No files were modified.", category="validation")
        if not dry_run:
            self._commit_staged_files(staged)
        return {
            "dry_run": dry_run,
            "clean": True,
            "summary": "\n".join(summaries),
            "affected_files": affected,
            "warnings": [],
        }

    def _validate_patch_path(self, raw_path: str, *, require_existing: bool) -> None:
        if require_existing:
            self.workspace.resolve_existing(raw_path)
        else:
            self.workspace.resolve_for_write(raw_path)

    def _commit_staged_files(self, staged: dict[str, str | None]) -> None:
        backups: dict[Path, bytes | None] = {}
        try:
            for rel, content in staged.items():
                path = self.workspace.resolve_for_write(rel).path
                backups[path] = path.read_bytes() if path.exists() and not path.is_dir() else None
                if rel not in self.patch_baselines:
                    if backups[path] is None:
                        self.patch_baselines[rel] = None
                    else:
                        self.patch_baselines[rel] = backups[path].decode("utf-8", errors="replace")
                if content is None:
                    if path.exists():
                        if path.is_dir():
                            raise ToolFailure("PATCH_FAILED", "Cannot delete a directory.", category="validation")
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
        except Exception:
            for path, data in backups.items():
                try:
                    if data is None:
                        if path.exists() and not path.is_dir():
                            path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                except OSError:
                    pass
            raise

    def exec_command(self, args: dict[str, Any]) -> dict[str, Any]:
        cmd = str(args.get("cmd", ""))
        if not cmd:
            raise ToolFailure("INVALID_ARGUMENT", "cmd is required.", category="validation")
        workdir = self.workspace.resolve_existing(str(args.get("workdir", ".")))
        if not workdir.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "workdir is not a directory.", category="validation")
        self._check_command_policy(cmd, args)
        timeout_ms = int(args.get("timeout_ms", 30000))
        yield_ms = int(args.get("yield_time_ms", 1000))
        max_output_bytes = int(args.get("max_output_bytes", 65536))
        tty = bool(args.get("tty", False))
        stdin_text = str(args.get("stdin", ""))
        env = self._command_env(args.get("env", {}))
        start = time.time()
        process = subprocess.Popen(
            cmd,
            cwd=str(workdir.path),
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        session = self._make_session(process)
        start_reader_threads(session)
        if stdin_text and process.stdin is not None:
            process.stdin.write(stdin_text.encode("utf-8"))
            process.stdin.flush()
            if not tty:
                process.stdin.close()
        initial_wait = max(0, min(yield_ms, 30000)) / 1000.0
        deadline = start + (timeout_ms / 1000.0)
        while True:
            if process.poll() is not None:
                session.refresh_status()
                payload = session.snapshot_since_cursor(max_output_bytes)
                payload.update(
                    {
                        "status": "exited",
                        "elapsed_ms": int((time.time() - start) * 1000),
                    }
                )
                return payload
            now = time.time()
            if not tty and now >= deadline:
                self._terminate_process_group(process, signal.SIGTERM)
                session.refresh_status()
                payload = session.snapshot_since_cursor(max_output_bytes)
                payload.update(
                    {
                        "status": "timeout",
                        "timed_out": True,
                        "elapsed_ms": int((time.time() - start) * 1000),
                    }
                )
                return payload
            if now - start >= initial_wait or tty:
                with self.sessions_lock:
                    self.sessions[session.session_id] = session
                payload = session.snapshot_since_cursor(max_output_bytes)
                payload.update(
                    {
                        "status": "running",
                        "elapsed_ms": int((time.time() - start) * 1000),
                    }
                )
                return payload
            time.sleep(0.02)

    def _check_command_policy(self, cmd: str, args: dict[str, Any]) -> None:
        env = args.get("env", {})
        if isinstance(env, dict) and any(SENSITIVE_ENV_RE.search(str(key)) for key in env):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Sensitive environment variables require explicit permission.",
                category="permission",
                details={"permission": "sensitive_env"},
            )
        self._check_command_paths(cmd)
        compact = " ".join(cmd.split()).lower()
        if re.search(r"(^|[;&|]\s*)rm\s+(-[^\s]*r[^\s]*f|-?[^\s]*f[^\s]*r)\s+/", compact):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Destructive commands are blocked without explicit permission.",
                category="permission",
                details={"permission": "destructive_command"},
            )
        if DESTRUCTIVE_RE.search(cmd):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Destructive commands are blocked without explicit permission.",
                category="permission",
                details={"permission": "destructive_command"},
            )
        if NETWORK_RE.search(cmd):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Network access is denied by default.",
                category="permission",
                details={"permission": "network"},
            )

    def _check_command_paths(self, cmd: str) -> None:
        try:
            tokens = shlex_split(cmd)
        except ValueError:
            tokens = cmd.split()
        for index, token in enumerate(tokens):
            if not token or token.startswith("-"):
                continue
            if index == 0 and token.startswith("/") and os.access(token, os.X_OK):
                continue
            if token.startswith("/") or token.startswith("~") or "../" in token or token == "..":
                raise ToolFailure(
                    "PERMISSION_REQUIRED",
                    "Command path escapes the workspace and is blocked.",
                    category="permission",
                    details={"path": token},
                )
            if "/" not in token and "." not in token:
                continue
            try:
                self.workspace.resolve_existing(token)
            except ToolFailure as exc:
                if exc.code in {"PATH_OUTSIDE_WORKSPACE", "ABSOLUTE_PATH_DENIED", "SYMLINK_ESCAPE"}:
                    raise ToolFailure(
                        "PERMISSION_REQUIRED",
                        "Command path escapes the workspace and is blocked.",
                        category="permission",
                        details={"path": token},
                    ) from exc

    def _command_env(self, extra: Any) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in ("PATH", "LANG", "LC_ALL"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["HOME"] = str(self.workspace.root)
        env["TMPDIR"] = str(self.workspace.root / ".tmp")
        (self.workspace.root / ".tmp").mkdir(exist_ok=True)
        if isinstance(extra, dict):
            for key, value in extra.items():
                key_text = str(key)
                if SENSITIVE_ENV_RE.search(key_text):
                    continue
                env[key_text] = str(value)
        return env

    def _make_session(self, process: subprocess.Popen[bytes]) -> ExecSession:
        return ExecSession(session_id=secrets.token_urlsafe(18), process=process)

    def write_stdin(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._get_session(session_id)
        session.refresh_status()
        chars = str(args.get("chars", ""))
        if session.process.poll() is not None:
            if chars:
                raise ToolFailure("SESSION_CLOSED", "Session is closed; stdin write blocked.", category="runtime")
            return session.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))
        if chars:
            if session.process.stdin is None:
                raise ToolFailure("SESSION_CLOSED", "Session stdin is closed.", category="runtime")
            session.process.stdin.write(chars.encode("utf-8"))
            session.process.stdin.flush()
        wait_until = time.time() + (int(args.get("yield_time_ms", 1000)) / 1000.0)
        while time.time() < wait_until and session.process.poll() is None:
            time.sleep(0.02)
            with session.lock:
                if len(session.stdout) > session.stdout_cursor or len(session.stderr) > session.stderr_cursor:
                    break
        return session.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))

    def kill_session(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._get_session(session_id)
        signal_name = str(args.get("signal", "TERM"))
        signum = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT}.get(signal_name, signal.SIGTERM)
        if session.process.poll() is None:
            self._terminate_process_group(session.process, signum)
            wait_until = time.time() + (int(args.get("wait_ms", 5000)) / 1000.0)
            while time.time() < wait_until and session.process.poll() is None:
                time.sleep(0.02)
            killed = True
            status = "terminated" if session.process.poll() is not None else "terminated"
        else:
            killed = False
            status = "exited"
        payload = session.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))
        payload.update({"killed": killed, "status": status})
        return payload

    def _get_session(self, session_id: str) -> ExecSession:
        with self.sessions_lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise ToolFailure("SESSION_NOT_FOUND", "Session not found; stdin access denied.", category="not_found")
        return session

    def _terminate_process_group(self, process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
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
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                process.kill()

    def git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.workspace.resolve_existing(str(args.get("path", ".")))
        max_entries = int(args.get("max_entries", 1000))
        include_untracked = bool(args.get("include_untracked", True))
        git = require_git()
        root_check = subprocess.run(
            [git, "-C", str(resolved.path), "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if root_check.returncode != 0:
            return {"is_repo": False, "clean": True, "entries": [], "truncated": False}
        status_cmd = [git, "-C", str(resolved.path), "status", "--porcelain=v1", "-b"]
        if not include_untracked:
            status_cmd.append("--untracked-files=no")
        completed = subprocess.run(status_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.strip() or "git status failed", category="runtime")
        lines = completed.stdout.splitlines()
        branch = ""
        upstream = ""
        ahead = 0
        behind = 0
        entries: list[dict[str, Any]] = []
        for line in lines:
            if line.startswith("## "):
                branch, upstream, ahead, behind = parse_branch_line(line[3:])
                continue
            if not line:
                continue
            path_text = line[3:]
            original = None
            if " -> " in path_text:
                original, path_text = path_text.split(" -> ", 1)
            entries.append(
                {
                    "path": path_text,
                    "original_path": original,
                    "index_status": line[0],
                    "worktree_status": line[1],
                }
            )
            if len(entries) >= max_entries:
                break
        return {
            "is_repo": True,
            "branch": branch,
            "head": git_rev_parse(resolved.path, "HEAD"),
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "clean": not entries,
            "entries": entries,
            "truncated": len(entries) >= max_entries and len(lines) > max_entries + 1,
        }

    def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        staged = bool(args.get("staged", False))
        unstaged = bool(args.get("unstaged", True))
        context = int(args.get("context_lines", 3))
        max_bytes = int(args.get("max_bytes", 262144))
        path_filters: list[str] = []
        if isinstance(args.get("path"), str):
            path_filters.append(str(args["path"]))
        if isinstance(args.get("paths"), list):
            path_filters.extend(str(item) for item in args["paths"])
        for path in path_filters:
            self.workspace.resolve_for_write(path)
        if not is_git_repo(self.workspace.root):
            return self._fallback_diff(path_filters, max_bytes)
        cmd = [git, "-C", str(self.workspace.root), "diff", f"--unified={context}"]
        if staged and not unstaged:
            cmd.append("--cached")
        if path_filters:
            cmd.append("--")
            cmd.extend(path_filters)
        completed = subprocess.run(cmd, text=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if completed.returncode not in {0, 1}:
            raise ToolFailure("GIT_ERROR", completed.stderr.decode("utf-8", errors="replace"), category="runtime")
        diff_text, truncated = truncate_bytes(completed.stdout, max_bytes)
        return {
            "diff": diff_text,
            "files": parse_diff_files(diff_text),
            "truncated": truncated,
            "warnings": ["diff truncated"] if truncated else [],
        }

    def _fallback_diff(self, path_filters: list[str], max_bytes: int) -> dict[str, Any]:
        selected = set(path_filters)
        chunks: list[str] = []
        files: list[dict[str, Any]] = []
        for rel, before in sorted(self.patch_baselines.items()):
            if selected and rel not in selected:
                continue
            current_path = self.workspace.resolve_for_write(rel).path
            after = current_path.read_text(encoding="utf-8") if current_path.exists() and not current_path.is_dir() else None
            if before == after:
                continue
            before_lines = [] if before is None else before.splitlines(keepends=True)
            after_lines = [] if after is None else after.splitlines(keepends=True)
            chunks.extend(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                    lineterm="",
                )
            )
            status = "added" if before is None else "deleted" if after is None else "modified"
            files.append({"path": rel, "status": status, "binary": False})
        diff = "\n".join(chunks)
        if diff and not diff.endswith("\n"):
            diff += "\n"
        diff_text, truncated = truncate_bytes(diff.encode("utf-8"), max_bytes)
        return {
            "diff": diff_text,
            "files": files,
            "truncated": truncated,
            "warnings": ["non-git diff fallback"] + (["diff truncated"] if truncated else []),
        }

    def request_permissions(self, args: dict[str, Any]) -> dict[str, Any]:
        raise ToolFailure(
            "ELICITATION_UNSUPPORTED",
            "Permission elicitation is not available for this client.",
            category="permission",
            details={"status": "unsupported", "requested": args},
        )

    def view_image(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.workspace.resolve_existing(str(args.get("path", "")))
        max_bytes = int(args.get("max_bytes", 5_242_880))
        data = resolved.path.read_bytes()
        if len(data) > max_bytes:
            raise ToolFailure("OUTPUT_TOO_LARGE", "Image exceeds max_bytes.", category="validation")
        mime_type, width, height = identify_image(data, resolved.path)
        if mime_type is None:
            raise ToolFailure("BINARY_FILE", "File is not a supported image.", category="validation")
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "path": resolved.display,
            "mime_type": mime_type,
            "bytes": len(data),
            "width": width,
            "height": height,
            "data_url": f"data:{mime_type};base64,{encoded}",
            "warnings": [],
        }
        return payload


@dataclass
class PatchOperation:
    kind: str
    path: str
    add_content: str | None = None
    hunks: list[list[str]] = field(default_factory=list)
    move_to: str | None = None


def parse_patch(patch: str) -> list[PatchOperation]:
    lines = patch.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise ToolFailure("PATCH_FAILED", "Patch must use *** Begin Patch / *** End Patch envelope.", category="validation")
    operations: list[PatchOperation] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if not line:
            i += 1
            continue
        if line.startswith("*** Add File: "):
            path = line.removeprefix("*** Add File: ").strip()
            i += 1
            content_lines: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ToolFailure("PATCH_FAILED", "Add file lines must start with '+'.", category="validation")
                content_lines.append(lines[i][1:])
                i += 1
            operations.append(PatchOperation("add", path, add_content="\n".join(content_lines) + "\n"))
            continue
        if line.startswith("*** Delete File: "):
            path = line.removeprefix("*** Delete File: ").strip()
            operations.append(PatchOperation("delete", path))
            i += 1
            continue
        if line.startswith("*** Update File: "):
            path = line.removeprefix("*** Update File: ").strip()
            i += 1
            move_to: str | None = None
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                move_to = lines[i].removeprefix("*** Move to: ").strip()
                i += 1
            hunks: list[list[str]] = []
            current: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if current:
                        hunks.append(current)
                    current = []
                else:
                    current.append(lines[i])
                i += 1
            if current:
                hunks.append(current)
            operations.append(PatchOperation("update", path, hunks=hunks, move_to=move_to))
            continue
        raise ToolFailure("PATCH_FAILED", f"Unrecognized patch line: {line}", category="validation")
    return operations


def apply_update_hunks(content: str, hunks: list[list[str]]) -> str:
    if not hunks:
        return content
    had_trailing_newline = content.endswith("\n")
    lines = content.splitlines()
    for hunk in hunks:
        old: list[str] = []
        new: list[str] = []
        for raw in hunk:
            if raw == "*** End of File":
                continue
            if not raw:
                raise ToolFailure("PATCH_FAILED", "Invalid empty patch line.", category="validation")
            marker = raw[0]
            value = raw[1:] if marker in {" ", "-", "+"} else raw
            if marker == " ":
                old.append(value)
                new.append(value)
            elif marker == "-":
                old.append(value)
            elif marker == "+":
                new.append(value)
            else:
                raise ToolFailure("PATCH_FAILED", "Update lines must start with space, '-' or '+'.", category="validation")
        index = find_subsequence(lines, old)
        if index < 0:
            raise ToolFailure("PATCH_FAILED", "Patch context did not match.", category="validation")
        lines = lines[:index] + new + lines[index + len(old) :]
    updated = "\n".join(lines)
    if had_trailing_newline or lines:
        updated += "\n"
    return updated


def find_subsequence(lines: list[str], needle: list[str]) -> int:
    if not needle:
        return 0
    limit = len(lines) - len(needle) + 1
    for index in range(max(0, limit)):
        if lines[index : index + len(needle)] == needle:
            return index
    return -1


def walk_files(root: Path) -> list[Path]:
    if root.is_file() or root.is_symlink():
        return [root]
    results: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in DEFAULT_EXCLUDED_NAMES]
        current_path = Path(current)
        for name in files:
            results.append(current_path / name)
    return results


def find_literal(line: str, query: str, case_sensitive: bool) -> Any:
    haystack = line if case_sensitive else line.lower()
    needle = query if case_sensitive else query.lower()
    index = haystack.find(needle)
    if index < 0:
        return None

    class Match:
        def start(self) -> int:
            return index

    return Match()


def shlex_split(command: str) -> list[str]:
    return shlex.split(command, posix=True)


def entry_for_path(path: Path, root: Path) -> dict[str, Any]:
    stat = path.lstat()
    if path.is_symlink():
        kind = "symlink"
    elif path.is_dir():
        kind = "directory"
    elif path.is_file():
        kind = "file"
    else:
        kind = "other"
    item: dict[str, Any] = {
        "name": path.name,
        "path": normalize_rel_display(path, root),
        "type": kind,
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "is_hidden": path.name.startswith("."),
        "is_ignored": False,
    }
    if path.is_symlink():
        try:
            item["symlink_target"] = os.readlink(path)
        except OSError:
            pass
    return item


def sort_value(item: dict[str, Any], sort_key: str) -> Any:
    if sort_key == "type":
        return (item.get("type", ""), item.get("name", ""))
    if sort_key == "modified":
        return (item.get("modified", ""), item.get("name", ""))
    return item.get("name", "")


def parse_branch_line(line: str) -> tuple[str, str, int, int]:
    branch = line
    upstream = ""
    ahead = 0
    behind = 0
    if "..." in line:
        branch, rest = line.split("...", 1)
        upstream = rest.split(" ", 1)[0]
    if "[" in line and "]" in line:
        meta = line.split("[", 1)[1].split("]", 1)[0]
        ahead_match = re.search(r"ahead (\d+)", meta)
        behind_match = re.search(r"behind (\d+)", meta)
        ahead = int(ahead_match.group(1)) if ahead_match else 0
        behind = int(behind_match.group(1)) if behind_match else 0
    return branch.strip(), upstream.strip(), ahead, behind


def git_rev_parse(path: Path, rev: str) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    completed = subprocess.run([git, "-C", str(path), "rev-parse", rev], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def is_git_repo(path: Path) -> bool:
    git = shutil.which("git")
    if not git:
        return False
    completed = subprocess.run(
        [git, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def require_git() -> str:
    git = shutil.which("git")
    if not git:
        raise ToolFailure("GIT_ERROR", "git executable not found.", category="runtime")
    return git


def parse_diff_files(diff_text: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                current = {"path": path, "status": "modified", "binary": False}
                files.append(current)
        elif current is not None and line.startswith("new file mode"):
            current["status"] = "added"
        elif current is not None and line.startswith("deleted file mode"):
            current["status"] = "deleted"
        elif current is not None and line.startswith("Binary files"):
            current["binary"] = True
    return files


def start_reader_threads(session: ExecSession) -> None:
    def reader(stream: Any, append: Any) -> None:
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                append(chunk)
        except Exception:
            return

    if session.process.stdout is not None:
        threading.Thread(target=reader, args=(session.process.stdout, session.append_stdout), daemon=True).start()
    if session.process.stderr is not None:
        threading.Thread(target=reader, args=(session.process.stderr, session.append_stderr), daemon=True).start()


def identify_image(data: bytes, path: Path) -> tuple[str | None, int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return "image/png", width, height
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return "image/gif", width, height
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg", None, None
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed, None, None
    return None, None, None


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def tool_result(payload: dict[str, Any], *, is_error: bool, content: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    text = json.dumps(payload, sort_keys=True)
    result_content = content or []
    result_content.append({"type": "text", "text": text})
    return {"content": result_content, "structuredContent": payload, "isError": is_error}


def object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def tool_definition(name: str) -> dict[str, Any]:
    schemas = input_schemas()
    annotations = tool_annotations(name)
    descriptions = {
        "read_file": "Read a UTF-8 text file slice inside the configured workspace.",
        "list_dir": "List directory entries inside the configured workspace.",
        "list_files": "List workspace files using glob filters.",
        "search_text": "Search UTF-8 workspace files for text or regex matches.",
        "apply_patch": "Apply a Codex-style patch envelope transactionally inside the workspace.",
        "exec_command": "Run a bounded command in the workspace under runtime policy.",
        "write_stdin": "Write characters to a server-managed running command session.",
        "kill_session": "Terminate a server-managed running command session.",
        "git_status": "Return git working tree status for the workspace.",
        "git_diff": "Return unified git diff for workspace changes.",
        "request_permissions": "Request a scoped permission grant for dangerous runtime operations.",
        "view_image": "Return a workspace image as MCP image content.",
    }
    return {
        "name": name,
        "title": annotations["title"],
        "description": descriptions[name],
        "inputSchema": schemas[name],
        "outputSchema": object_schema({"ok": {"type": "boolean"}}, ["ok"]),
        "annotations": annotations,
    }


def tool_annotations(name: str) -> dict[str, Any]:
    read_only = name in {"read_file", "list_dir", "list_files", "search_text", "git_status", "git_diff", "request_permissions", "view_image"}
    destructive = name in {"apply_patch", "exec_command", "kill_session"}
    idempotent = name in {"read_file", "list_dir", "list_files", "search_text", "git_status", "git_diff", "view_image"}
    open_world = name == "exec_command"
    titles = {
        "read_file": "Read file",
        "list_dir": "List directory",
        "list_files": "List files",
        "search_text": "Search text",
        "apply_patch": "Apply patch",
        "exec_command": "Execute command",
        "write_stdin": "Write stdin",
        "kill_session": "Kill session",
        "git_status": "Git status",
        "git_diff": "Git diff",
        "request_permissions": "Request permissions",
        "view_image": "View image",
    }
    return {
        "title": titles[name],
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


def input_schemas() -> dict[str, dict[str, Any]]:
    string = {"type": "string"}
    integer = {"type": "integer"}
    boolean = {"type": "boolean"}
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "read_file": object_schema(
            {
                "path": {**string, "minLength": 1},
                "start_line": {**integer, "minimum": 1, "default": 1},
                "end_line": {**integer, "minimum": 1},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 131072},
                "encoding": {**string, "enum": ["utf-8"], "default": "utf-8"},
            },
            ["path"],
        ),
        "list_dir": object_schema(
            {
                "path": {**string, "default": "."},
                "recursive": {**boolean, "default": False},
                "max_depth": {**integer, "minimum": 1, "maximum": 20, "default": 1},
                "max_entries": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
                "include_hidden": {**boolean, "default": False},
                "include_ignored": {**boolean, "default": False},
                "sort": {**string, "enum": ["name", "type", "modified"], "default": "name"},
            }
        ),
        "list_files": object_schema(
            {
                "path": {**string, "default": "."},
                "patterns": string_array,
                "glob": string,
                "exclude_patterns": string_array,
                "include_hidden": {**boolean, "default": False},
                "include_ignored": {**boolean, "default": False},
                "max_results": {**integer, "minimum": 1, "maximum": 50000, "default": 5000},
                "sort": {**string, "enum": ["path", "modified"], "default": "path"},
            }
        ),
        "search_text": object_schema(
            {
                "query": {**string, "minLength": 1},
                "path": {**string, "default": "."},
                "regex": {**boolean, "default": False},
                "case_sensitive": {**boolean, "default": False},
                "include_globs": string_array,
                "glob": string,
                "exclude_globs": string_array,
                "context_lines": {**integer, "minimum": 0, "maximum": 5, "default": 0},
                "max_results": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
                "max_preview_bytes": {**integer, "minimum": 80, "maximum": 4096, "default": 512},
            },
            ["query"],
        ),
        "apply_patch": object_schema({"patch": {**string, "minLength": 1}, "dry_run": {**boolean, "default": False}}, ["patch"]),
        "exec_command": object_schema(
            {
                "cmd": {**string, "minLength": 1},
                "workdir": {**string, "default": "."},
                "timeout_ms": {**integer, "minimum": 1, "maximum": 600000, "default": 30000},
                "yield_time_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 1000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "stdin": {**string, "default": ""},
                "tty": {**boolean, "default": False},
                "env": {"type": "object", "additionalProperties": {"type": "string"}, "default": {}},
                "permission_grant_id": string,
            },
            ["cmd"],
        ),
        "write_stdin": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "chars": {**string, "default": ""},
                "yield_time_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 1000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
            },
            ["session_id"],
        ),
        "kill_session": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "signal": {**string, "enum": ["TERM", "KILL", "INT"], "default": "TERM"},
                "wait_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 5000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
            },
            ["session_id"],
        ),
        "git_status": object_schema(
            {
                "path": {**string, "default": "."},
                "include_untracked": {**boolean, "default": True},
                "max_entries": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
            }
        ),
        "git_diff": object_schema(
            {
                "path": string,
                "paths": string_array,
                "staged": {**boolean, "default": False},
                "unstaged": {**boolean, "default": True},
                "context_lines": {**integer, "minimum": 0, "maximum": 20, "default": 3},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 262144},
            }
        ),
        "request_permissions": object_schema(
            {
                "tool_name": {**string, "enum": ["exec_command", "apply_patch"]},
                "permission": {
                    **string,
                    "enum": ["network", "destructive_command", "long_timeout", "sensitive_env", "write_generated_or_ignored"],
                },
                "reason": {**string, "minLength": 1},
                "arguments": {"type": "object", "additionalProperties": True},
                "scope": {**string, "enum": ["once", "session"], "default": "once"},
                "ttl_seconds": {**integer, "minimum": 1, "maximum": 3600, "default": 300},
            },
            ["tool_name", "permission", "reason", "arguments"],
        ),
        "view_image": object_schema(
            {
                "path": {**string, "minLength": 1},
                "max_bytes": {**integer, "minimum": 1024, "maximum": 10485760, "default": 5242880},
                "output": {**string, "enum": ["mcp_image", "data_url"], "default": "mcp_image"},
            },
            ["path"],
        ),
    }


class MCPHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CodexToolRuntimeMCP/0.1"

    @property
    def runtime(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args, file=sys.stderr)

    def do_GET(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.end_headers()

    def do_POST(self) -> None:
        if posixpath.normpath(self.path) != "/mcp":
            self.send_json({"jsonrpc": "2.0", "error": {"code": -32601, "message": "Unknown endpoint"}}, status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not (origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")):
            self.send_json({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Origin denied"}}, status=403)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            request = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}, status=400)
            return
        if not isinstance(request, dict):
            self.send_json({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Request"}}, status=400)
            return
        response = self.handle_rpc(request)
        if response is None:
            self.send_response(202)
            self.send_header("Mcp-Session-Id", self.runtime.http_session_id)
            self.end_headers()
            return
        self.send_json(response)

    def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        try:
            if method == "initialize":
                result = self.runtime.initialize()
            elif method == "notifications/initialized":
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self.runtime.list_tools()
            elif method == "tools/call":
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    raise JsonRpcError(-32602, "tools/call requires a tool name")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise JsonRpcError(-32602, "tools/call arguments must be an object")
                result = self.runtime.call_tool(params["name"], arguments)
            else:
                raise JsonRpcError(-32601, f"Unknown method: {method}")
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JsonRpcError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            response: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
            if request_id is not None:
                response["id"] = request_id
            return response
        except Exception as exc:  # noqa: BLE001
            response = {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(exc)}}
            if request_id is not None:
                response["id"] = request_id
            return response

    def send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json_response_payload(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", self.runtime.http_session_id)
        self.end_headers()
        self.wfile.write(body)


class RuntimeHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[MCPHandler], runtime: Runtime) -> None:
        super().__init__(address, handler)
        self.runtime = runtime


def run_http(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.environ.get("CODEX_TOOL_RUNTIME_WORKSPACE") or os.getcwd())
    runtime = Runtime(workspace, enable_view_image=args.enable_view_image)
    server = RuntimeHTTPServer((args.host, args.port), MCPHandler, runtime)
    print(f"{SERVER_NAME} listening on http://{args.host}:{args.port}/mcp", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def run_stdio(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.environ.get("CODEX_TOOL_RUNTIME_WORKSPACE") or os.getcwd())
    runtime = Runtime(workspace, enable_view_image=args.enable_view_image)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            fake = StdioDispatcher(runtime)
            response = fake.handle_rpc(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "error": {"code": -32603, "message": str(exc)}}) + "\n")
            sys.stdout.flush()
    return 0


class StdioDispatcher:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}
        try:
            if method == "initialize":
                result = self.runtime.initialize()
            elif method == "notifications/initialized":
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self.runtime.list_tools()
            elif method == "tools/call":
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    raise JsonRpcError(-32602, "tools/call requires a tool name")
                result = self.runtime.call_tool(params["name"], params.get("arguments") or {})
            else:
                raise JsonRpcError(-32601, f"Unknown method: {method}")
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JsonRpcError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            response: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
            if request_id is not None:
                response["id"] = request_id
            return response


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve Codex-style coding runtime primitives over MCP.")
    parser.add_argument("--workspace", help="workspace root; defaults to CODEX_TOOL_RUNTIME_WORKSPACE or cwd")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--stdio", action="store_true", help="serve newline-delimited JSON-RPC over stdio")
    parser.add_argument(
        "--enable-view-image",
        action="store_true",
        default=os.environ.get("CODEX_TOOL_RUNTIME_ENABLE_VIEW_IMAGE") == "1",
        help="enable the P1 view_image tool",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_stdio(args) if args.stdio else run_http(args)
