"""Bounded repository fingerprinting for durable Agent Session continuity."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .project_context import load_project_context


MAX_GIT_OUTPUT_BYTES = 64 * 1024
MAX_CHANGED_PATHS = 100
GIT_TIMEOUT_SECONDS = 5


def build_repo_fingerprint(root: str | Path) -> dict[str, Any]:
    """Return a small local repository fingerprint without installing anything.

    The projection intentionally contains no file contents. Git failures are
    represented as an unavailable Git component rather than making an Agent
    Session unusable for a non-Git workspace.
    """

    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return {
            "version": 1,
            "git_available": False,
            "head": None,
            "worktree_digest": None,
            "instruction_digest": None,
            "changed_paths": [],
            "context_changed": False,
            "changes": [],
            "error": str(exc)[:200],
        }

    head_raw = _git_output(resolved, "rev-parse", "--verify", "HEAD")
    status_raw = _git_output(
        resolved,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        "-z",
    )
    git_available = head_raw is not None and status_raw is not None
    head = _first_line(head_raw) if head_raw is not None else None
    changed_paths = _status_paths(status_raw or b"")
    worktree_digest = (
        hashlib.sha256(status_raw).hexdigest() if status_raw is not None else None
    )

    try:
        context = load_project_context(resolved)
        instruction_digest = hashlib.sha256(
            context.server_instructions().encode("utf-8")
        ).hexdigest()
    except (OSError, RuntimeError, ValueError):
        instruction_digest = None

    return {
        "version": 1,
        "git_available": git_available,
        "head": head,
        "worktree_digest": worktree_digest,
        "instruction_digest": instruction_digest,
        "changed_paths": changed_paths,
        "context_changed": False,
        "changes": [],
    }


def fingerprint_changes(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return deterministic bounded change labels between two fingerprints."""

    if not previous:
        return ()
    changes: list[str] = []
    if previous.get("head") != current.get("head"):
        changes.append("HEAD changed")
    if previous.get("instruction_digest") != current.get("instruction_digest"):
        changes.append("Project instructions changed")
    if previous.get("worktree_digest") != current.get("worktree_digest"):
        changes.append("Working tree changed")
    if previous.get("git_available") != current.get("git_available"):
        changes.append("Git availability changed")
    return tuple(changes[:20])


def with_context_changes(
    current: Mapping[str, Any],
    changes: tuple[str, ...],
) -> dict[str, Any]:
    result = dict(current)
    result["context_changed"] = bool(changes)
    result["changes"] = list(changes)
    return result


def _git_output(root: Path, *args: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout[:MAX_GIT_OUTPUT_BYTES]


def _first_line(raw: bytes) -> str | None:
    line = raw.splitlines()[0].strip() if raw.splitlines() else b""
    if not line:
        return None
    try:
        return line.decode("ascii")[:128]
    except UnicodeDecodeError:
        return None


def _status_paths(raw: bytes) -> list[str]:
    paths: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        payload = item[3:] if len(item) >= 3 else b""
        try:
            path = payload.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - bytes.decode is defensive here
            continue
        if path:
            paths.append(path[:512])
        if len(paths) >= MAX_CHANGED_PATHS:
            break
    return paths


__all__ = [
    "MAX_CHANGED_PATHS",
    "build_repo_fingerprint",
    "fingerprint_changes",
    "with_context_changes",
]
