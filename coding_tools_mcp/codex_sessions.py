"""Bounded, Workspace-confined discovery and import of Codex session files."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .transcript import TranscriptStore, WorkspaceScope


DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_MESSAGES = 20_000
SUPPORTED_SUFFIXES = frozenset({".jsonl", ".json", ".md", ".markdown"})


class CodexSessionError(ValueError):
    pass


@dataclass(frozen=True)
class ScanPolicy:
    max_depth: int = DEFAULT_MAX_DEPTH
    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_messages: int = DEFAULT_MAX_MESSAGES

    def validated(self) -> "ScanPolicy":
        values = {
            "max_depth": (self.max_depth, 0, 32),
            "max_files": (self.max_files, 1, 10_000),
            "max_file_bytes": (self.max_file_bytes, 1024, 100 * 1024 * 1024),
            "max_total_bytes": (self.max_total_bytes, 1024, 1024 * 1024 * 1024),
            "max_messages": (self.max_messages, 1, 100_000),
        }
        for name, (value, minimum, maximum) in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise CodexSessionError(f"{name} must be between {minimum} and {maximum}.")
        return self


class CodexSessionScanner:
    """Scanner whose cache and candidates are keyed by explicit Workspace identity."""

    def __init__(self, *, max_cache_entries: int = 1024) -> None:
        if not 1 <= max_cache_entries <= 100_000:
            raise ValueError("max_cache_entries must be between 1 and 100000.")
        self.max_cache_entries = max_cache_entries
        self._cache: dict[tuple[str, str, int, int], dict[str, Any]] = {}

    def _cache_put(self, key: tuple[str, str, int, int], parsed: dict[str, Any]) -> None:
        workspace_id, relative, _mtime, _size = key
        for old_key in list(self._cache):
            if old_key[0] == workspace_id and old_key[1] == relative and old_key != key:
                self._cache.pop(old_key, None)
        while len(self._cache) >= self.max_cache_entries:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = parsed

    def scan(
        self,
        scope: WorkspaceScope,
        *,
        roots: list[str] | None = None,
        policy: ScanPolicy | None = None,
    ) -> dict[str, Any]:
        policy = (policy or ScanPolicy()).validated()
        resolved_roots = _resolve_roots(scope, roots or ["."])
        candidates: list[dict[str, Any]] = []
        scan_errors: list[dict[str, Any]] = []
        seen_files = 0
        read_bytes = 0
        stop_scan = False
        for root in resolved_roots:
            if stop_scan:
                break
            try:
                iterator = _iter_candidate_files(scope, root, max_depth=policy.max_depth)
                for path in iterator:
                    if seen_files >= policy.max_files:
                        scan_errors.append({"code": "file_limit", "message": "Candidate file limit reached."})
                        stop_scan = True
                        break
                    seen_files += 1
                    try:
                        stat = path.stat()
                    except OSError:
                        candidates.append(_file_error_candidate(scope, path, "stat_failed"))
                        continue
                    relative = path.relative_to(scope.workspace_root).as_posix()
                    if stat.st_size > policy.max_file_bytes:
                        candidates.append(
                            _file_error_candidate(
                                scope,
                                path,
                                "file_too_large",
                                file_size=stat.st_size,
                                file_mtime_ns=stat.st_mtime_ns,
                            )
                        )
                        continue
                    if read_bytes + stat.st_size > policy.max_total_bytes:
                        scan_errors.append({"code": "total_byte_limit", "message": "Total transcript read limit reached."})
                        stop_scan = True
                        break
                    cache_key = (scope.workspace_id, relative, stat.st_mtime_ns, stat.st_size)
                    parsed = self._cache.get(cache_key)
                    if parsed is None:
                        parsed = parse_codex_session_file(
                            scope,
                            path,
                            max_file_bytes=policy.max_file_bytes,
                            max_messages=policy.max_messages,
                        )
                        self._cache_put(cache_key, parsed)
                    read_bytes += stat.st_size
                    candidates.append(_preview(parsed))
            except OSError as exc:
                scan_errors.append({"code": "root_unreadable", "message": type(exc).__name__})
        candidates.sort(key=lambda item: (item.get("relative_path", ""), item.get("session_id", "")))
        return {
            "workspace_id": scope.workspace_id,
            "candidates": candidates,
            "candidate_count": len(candidates),
            "scan_errors": scan_errors,
            "scan_error_count": len(scan_errors),
            "files_considered": seen_files,
            "bytes_read": read_bytes,
            "limits": {
                "max_depth": policy.max_depth,
                "max_files": policy.max_files,
                "max_file_bytes": policy.max_file_bytes,
                "max_total_bytes": policy.max_total_bytes,
                "max_messages": policy.max_messages,
            },
        }

    def import_candidates(
        self,
        store: TranscriptStore,
        scope: WorkspaceScope,
        *,
        candidate_ids: list[str],
        roots: list[str] | None = None,
        policy: ScanPolicy | None = None,
    ) -> dict[str, Any]:
        if not isinstance(candidate_ids, list) or not candidate_ids:
            raise CodexSessionError("candidate_ids must be a non-empty list.")
        wanted = {str(item) for item in candidate_ids}
        policy = (policy or ScanPolicy()).validated()
        resolved_roots = _resolve_roots(scope, roots or ["."])
        found: dict[str, dict[str, Any]] = {}
        files_seen = 0
        total_bytes = 0
        for root in resolved_roots:
            for path in _iter_candidate_files(scope, root, max_depth=policy.max_depth):
                if files_seen >= policy.max_files or len(found) == len(wanted):
                    break
                files_seen += 1
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size > policy.max_file_bytes or total_bytes + stat.st_size > policy.max_total_bytes:
                    continue
                relative = path.relative_to(scope.workspace_root).as_posix()
                candidate_id = _candidate_id(scope.workspace_id, relative)
                if candidate_id not in wanted:
                    continue
                cache_key = (scope.workspace_id, relative, stat.st_mtime_ns, stat.st_size)
                parsed = self._cache.get(cache_key)
                if parsed is None:
                    parsed = parse_codex_session_file(
                        scope,
                        path,
                        max_file_bytes=policy.max_file_bytes,
                        max_messages=policy.max_messages,
                    )
                    self._cache_put(cache_key, parsed)
                total_bytes += stat.st_size
                found[candidate_id] = parsed
        inserted = 0
        duplicates = 0
        imported_sessions = 0
        errors: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            parsed = found.get(candidate_id)
            if parsed is None:
                errors.append({"candidate_id": candidate_id, "code": "not_found"})
                continue
            if parsed.get("fatal_error"):
                store.upsert_imported_session(scope.workspace_id, parsed)
                errors.append({"candidate_id": candidate_id, "code": str(parsed["fatal_error"])})
                continue
            result = store.record_messages(
                scope.workspace_id,
                str(parsed["conversation_id"]),
                list(parsed.get("messages") or []),
                title=str(parsed.get("title") or parsed["session_id"]),
                source="codex-session-import",
            )
            parsed = dict(parsed)
            parsed["imported_at"] = __import__("time").time()
            store.upsert_imported_session(scope.workspace_id, parsed)
            inserted += int(result["inserted_count"])
            duplicates += int(result["duplicate_count"])
            imported_sessions += 1
        return {
            "workspace_id": scope.workspace_id,
            "requested_count": len(candidate_ids),
            "imported_session_count": imported_sessions,
            "inserted_count": inserted,
            "duplicate_count": duplicates,
            "errors": errors,
            "error_count": len(errors),
        }


def parse_codex_session_file(
    scope: WorkspaceScope,
    path: Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> dict[str, Any]:
    safe_path = _safe_file(scope, path)
    relative = safe_path.relative_to(scope.workspace_root).as_posix()
    candidate_id = _candidate_id(scope.workspace_id, relative)
    try:
        stat = safe_path.stat()
    except OSError as exc:
        return _base_result(scope, relative, candidate_id, fatal_error="stat_failed", parse_errors=[_error("stat_failed", type(exc).__name__)])
    if stat.st_size > max_file_bytes:
        return _base_result(
            scope,
            relative,
            candidate_id,
            fatal_error="file_too_large",
            file_size=stat.st_size,
            file_mtime_ns=stat.st_mtime_ns,
            parse_errors=[_error("file_too_large", "File exceeds configured size limit.")],
        )
    try:
        raw = safe_path.read_bytes()
    except (OSError, PermissionError) as exc:
        return _base_result(
            scope,
            relative,
            candidate_id,
            fatal_error="read_failed",
            file_size=stat.st_size,
            file_mtime_ns=stat.st_mtime_ns,
            parse_errors=[_error("read_failed", type(exc).__name__)],
        )
    text, encoding, decode_error = _decode_text(raw)
    if decode_error:
        return _base_result(
            scope,
            relative,
            candidate_id,
            fatal_error="invalid_encoding",
            file_size=stat.st_size,
            file_mtime_ns=stat.st_mtime_ns,
            parse_errors=[_error("invalid_encoding", decode_error)],
        )
    metadata: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    suffix = safe_path.suffix.lower()
    if suffix == ".jsonl":
        _parse_jsonl(text, messages, metadata, parse_errors, max_messages=max_messages)
    elif suffix == ".json":
        _parse_json(text, messages, metadata, parse_errors, max_messages=max_messages)
    else:
        _parse_markdown(text, messages, parse_errors, max_messages=max_messages)
    source_session_id = _safe_session_id(metadata.get("session_id") or metadata.get("id") or safe_path.stem)
    session_id = candidate_id
    conversation_id = f"codex-{candidate_id}"
    for message in messages:
        message["message_id"] = f"{candidate_id}:{message['message_id']}"
    title = _clean_text(metadata.get("title") or safe_path.stem, 500)
    return {
        "workspace_id": scope.workspace_id,
        "candidate_id": candidate_id,
        "session_id": session_id,
        "source_session_id": source_session_id,
        "conversation_id": conversation_id,
        "relative_path": relative,
        "source_kind": "codex",
        "title": title,
        "summary": f"{len(messages)} parsed messages from {safe_path.name}",
        "message_count": len(messages),
        "messages": messages,
        "parse_errors": parse_errors,
        "parse_error_count": len(parse_errors),
        "fatal_error": None,
        "file_size": stat.st_size,
        "file_mtime_ns": stat.st_mtime_ns,
        "encoding": encoding,
    }


def _resolve_roots(scope: WorkspaceScope, roots: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for raw in roots:
        if not isinstance(raw, str) or not raw.strip():
            raise CodexSessionError("Scan roots must be non-empty relative paths.")
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise CodexSessionError("Absolute paths and '..' are not allowed in scan roots.")
        try:
            path = (scope.workspace_root / candidate).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CodexSessionError(f"Scan root cannot be resolved: {exc}") from exc
        _ensure_within(scope.workspace_root, path)
        if path.is_symlink() or not path.is_dir():
            raise CodexSessionError("Scan root must be a real directory inside the Workspace.")
        resolved.append(path)
    return list(dict.fromkeys(resolved))


def _iter_candidate_files(scope: WorkspaceScope, root: Path, *, max_depth: int) -> Iterator[Path]:
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
        except OSError:
            raise
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                resolved = entry_path.resolve(strict=True)
                _ensure_within(scope.workspace_root, resolved)
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        stack.append((resolved, depth + 1))
                elif entry.is_file(follow_symlinks=False) and resolved.suffix.lower() in SUPPORTED_SUFFIXES:
                    yield resolved
            except (OSError, RuntimeError, CodexSessionError):
                continue


def _safe_file(scope: WorkspaceScope, path: Path) -> Path:
    if path.is_absolute():
        candidate = path
    else:
        candidate = scope.workspace_root / path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CodexSessionError(f"Session file cannot be resolved: {exc}") from exc
    _ensure_within(scope.workspace_root, resolved)
    if resolved.is_symlink() or not resolved.is_file():
        raise CodexSessionError("Session path must be a real file inside the Workspace.")
    return resolved


def _ensure_within(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CodexSessionError("Session path escapes the registered Workspace.") from exc


def _decode_text(raw: bytes) -> tuple[str, str, str | None]:
    attempts: list[tuple[str, str]] = []
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        attempts.append(("utf-16", "utf-16"))
    if raw.startswith(b"\xef\xbb\xbf"):
        attempts.append(("utf-8-sig", "utf-8-sig"))
    attempts.extend((("utf-8", "utf-8"), ("gb18030", "gb18030")))
    seen: set[str] = set()
    for codec, label in attempts:
        if codec in seen:
            continue
        seen.add(codec)
        try:
            return raw.decode(codec), label, None
        except UnicodeDecodeError:
            continue
    return "", "unknown", "File is not valid UTF-8, UTF-16, or GB18030 text."


def _parse_jsonl(
    text: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    errors: list[dict[str, Any]],
    *,
    max_messages: int,
) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            errors.append({"line": line_number, "code": "invalid_json", "message": "Line is not complete valid JSON."})
            continue
        _consume_record(record, messages, metadata, errors, line_number=line_number, max_messages=max_messages)


def _parse_json(
    text: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    errors: list[dict[str, Any]],
    *,
    max_messages: int,
) -> None:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        errors.append({"line": 1, "code": "invalid_json", "message": "Document is not complete valid JSON."})
        return
    records = document if isinstance(document, list) else document.get("messages", []) if isinstance(document, dict) else []
    if isinstance(document, dict):
        metadata.update({key: document.get(key) for key in ("id", "session_id", "title") if document.get(key) is not None})
    if not isinstance(records, list):
        errors.append({"line": 1, "code": "invalid_shape", "message": "JSON messages field must be a list."})
        return
    for index, record in enumerate(records, start=1):
        _consume_record(record, messages, metadata, errors, line_number=index, max_messages=max_messages)


def _parse_markdown(text: str, messages: list[dict[str, Any]], errors: list[dict[str, Any]], *, max_messages: int) -> None:
    blocks = [block.strip() for block in text.replace("\r\n", "\n").split("\n\n") if block.strip()]
    for index, block in enumerate(blocks, start=1):
        if len(messages) >= max_messages:
            errors.append({"line": index, "code": "message_limit", "message": "Message limit reached."})
            break
        role = "unknown"
        lowered = block.lower()
        for candidate in ("user", "assistant", "system", "developer", "tool"):
            if lowered.startswith(candidate + ":"):
                role = candidate
                block = block.split(":", 1)[1].lstrip()
                break
        messages.append({"message_id": f"md-{index}", "role": role, "content": block, "timestamp": None, "source": "codex-markdown"})


def _consume_record(
    record: Any,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    errors: list[dict[str, Any]],
    *,
    line_number: int,
    max_messages: int,
) -> None:
    if not isinstance(record, dict):
        errors.append({"line": line_number, "code": "invalid_record", "message": "Record must be a JSON object."})
        return
    record_type = str(record.get("type") or "")
    raw_payload = record.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    if record_type in {"session_meta", "session_metadata"}:
        metadata.update({key: payload.get(key) for key in ("id", "session_id", "title", "cwd") if payload.get(key) is not None})
        return
    role: str | None = None
    content: Any = None
    message_id: Any = None
    if record_type in {"user_message", "assistant_message", "system_message"}:
        role = record_type.removesuffix("_message")
        content = record.get("message", record.get("content"))
        message_id = record.get("id")
    elif record_type in {"response_item", "message"}:
        source = payload if payload else record
        if source.get("type") == "message" or record_type == "message":
            role = str(source.get("role") or "unknown")
            content = source.get("content")
            message_id = source.get("id")
    elif "role" in record and "content" in record:
        role = str(record.get("role") or "unknown")
        content = record.get("content")
        message_id = record.get("id") or record.get("message_id")
    if role is None:
        return
    if len(messages) >= max_messages:
        if not any(item.get("code") == "message_limit" for item in errors):
            errors.append({"line": line_number, "code": "message_limit", "message": "Message limit reached."})
        return
    text = _content_text(content)
    if not text:
        return
    normalized_role = role.lower()
    if normalized_role not in {"user", "assistant", "system", "developer", "tool"}:
        normalized_role = "unknown"
    messages.append(
        {
            "message_id": _safe_session_id(str(message_id or f"line-{line_number}")),
            "role": normalized_role,
            "content": text,
            "timestamp": record.get("timestamp") or payload.get("timestamp"),
            "source": "codex-session",
        }
    )


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        return text if isinstance(text, str) else ""
    return ""


def _candidate_id(workspace_id: str, relative_path: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}\0{relative_path}".encode("utf-8")).hexdigest()[:24]
    return f"cand-{digest}"


def _safe_session_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-._~" else "-" for char in value.strip())
    return (cleaned.strip("-") or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16])[:200]


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").encode("utf-8", errors="replace").decode("utf-8", errors="replace")[:limit]


def _preview(parsed: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in parsed.items() if key != "messages"}


def _base_result(
    scope: WorkspaceScope,
    relative: str,
    candidate_id: str,
    *,
    fatal_error: str,
    parse_errors: list[dict[str, Any]],
    file_size: int = 0,
    file_mtime_ns: int = 0,
) -> dict[str, Any]:
    source_session_id = _safe_session_id(Path(relative).stem)
    return {
        "workspace_id": scope.workspace_id,
        "candidate_id": candidate_id,
        "session_id": candidate_id,
        "source_session_id": source_session_id,
        "conversation_id": f"codex-{candidate_id}",
        "relative_path": relative,
        "source_kind": "codex",
        "title": Path(relative).name,
        "summary": "Session could not be parsed.",
        "message_count": 0,
        "messages": [],
        "parse_errors": parse_errors,
        "parse_error_count": len(parse_errors),
        "fatal_error": fatal_error,
        "file_size": file_size,
        "file_mtime_ns": file_mtime_ns,
        "encoding": None,
    }


def _file_error_candidate(
    scope: WorkspaceScope,
    path: Path,
    code: str,
    *,
    file_size: int = 0,
    file_mtime_ns: int = 0,
) -> dict[str, Any]:
    relative = path.relative_to(scope.workspace_root).as_posix()
    return _preview(
        _base_result(
            scope,
            relative,
            _candidate_id(scope.workspace_id, relative),
            fatal_error=code,
            parse_errors=[_error(code, "Session file could not be processed.")],
            file_size=file_size,
            file_mtime_ns=file_mtime_ns,
        )
    )


def _error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


__all__ = [
    "CodexSessionError",
    "CodexSessionScanner",
    "ScanPolicy",
    "parse_codex_session_file",
]
