from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .transcript import TranscriptStore, sanitized_text


CODEX_DESKTOP_SOURCE = "codex-desktop"
SUPPORTED_SUFFIXES = {".jsonl", ".json", ".md", ".txt"}
DEFAULT_MAX_CANDIDATES = 200
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_MESSAGES = 20_000
SENSITIVE_PATH_RE = re.compile(r"(^|[\\/._-])(auth|token|secret|credential|password|passwd|cookies?|keychain|oauth)([\\/._-]|$)", re.I)
SESSION_NAME_RE = re.compile(r"(session|conversation|transcript|chat|rollout|codex)", re.I)
ROLE_ALIASES = {
    "human": "user",
    "client": "user",
    "ai": "assistant",
    "model": "assistant",
    "bot": "assistant",
}


def scan_codex_session_candidates(
    *,
    roots: list[str] | None = None,
    limit: int = DEFAULT_MAX_CANDIDATES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    max_depth = max(0, min(int(max_depth), 16))
    max_file_bytes = max(1024, min(int(max_file_bytes), 100 * 1024 * 1024))
    max_messages = max(1, min(int(max_messages), 100_000))
    resolved_roots = _expanded_roots(roots)
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped_count = 0
    seen_files: set[str] = set()
    for root in resolved_roots:
        root_info = _root_info(root)
        if not root_info["exists"]:
            continue
        for path in _iter_candidate_files(root, max_depth=max_depth):
            file_key = _path_key(path)
            if file_key in seen_files:
                continue
            seen_files.add(file_key)
            if len(candidates) >= limit:
                warnings.append("candidate limit reached")
                break
            try:
                stat = path.stat()
            except OSError:
                skipped_count += 1
                continue
            if stat.st_size > max_file_bytes:
                skipped_count += 1
                continue
            parsed = parse_codex_session_file(path, max_messages=max_messages)
            if parsed is None:
                skipped_count += 1
                continue
            candidates.append(_preview_candidate(parsed))
        if len(candidates) >= limit:
            break
    return {
        "ok": True,
        "roots": [_root_info(root) for root in resolved_roots],
        "candidates": candidates,
        "candidate_count": len(candidates),
        "skipped_count": skipped_count,
        "warnings": warnings,
        "source": CODEX_DESKTOP_SOURCE,
    }


def import_codex_session_candidates(
    store: TranscriptStore,
    *,
    roots: list[str] | None = None,
    candidate_ids: list[str] | None = None,
    import_all: bool = False,
    limit: int = DEFAULT_MAX_CANDIDATES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    mode: str = "import",
) -> dict[str, Any]:
    scan = scan_codex_session_candidates(
        roots=roots,
        limit=limit,
        max_depth=max_depth,
        max_file_bytes=max_file_bytes,
        max_messages=max_messages,
    )
    selected_ids = {str(item) for item in candidate_ids or [] if str(item).strip()}
    if not import_all and not selected_ids:
        return {
            "ok": True,
            "mode": mode,
            "imported_count": 0,
            "message_count": 0,
            "inserted_count": 0,
            "duplicate_count": 0,
            "skipped_count": scan.get("skipped_count", 0),
            "candidates": scan.get("candidates", []),
            "candidate_count": scan.get("candidate_count", 0),
            "warning": "no candidate_ids selected; pass import_all=true to import every candidate",
        }
    selected = [
        candidate
        for candidate in scan.get("candidates", [])
        if import_all or str(candidate.get("candidate_id")) in selected_ids
    ]
    imported: list[dict[str, Any]] = []
    message_count = 0
    inserted_count = 0
    duplicate_count = 0
    skipped_count = int(scan.get("skipped_count", 0))
    for candidate in selected:
        source_path = candidate.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            skipped_count += 1
            continue
        parsed = parse_codex_session_file(Path(source_path), max_messages=max_messages)
        if parsed is None:
            skipped_count += 1
            continue
        messages = parsed.get("messages") if isinstance(parsed.get("messages"), list) else []
        result = store.record_chat_messages(
            conversation_id=str(parsed["conversation_id"]),
            messages=messages,
            source=CODEX_DESKTOP_SOURCE,
            conversation_title=str(parsed.get("title") or ""),
            conversation_uid=str(parsed.get("session_id") or parsed.get("candidate_id") or ""),
            project_id=_optional_str(parsed.get("project_id")),
            project_name=_optional_str(parsed.get("project_name")),
            project_path=_optional_str(parsed.get("project_path")),
            project_workspace=_optional_str(parsed.get("project_workspace")),
            project_metadata=_project_metadata(parsed),
        )
        message_count += int(result.get("message_count", 0))
        inserted_count += int(result.get("inserted_count", 0))
        duplicate_count += int(result.get("duplicate_count", 0))
        skipped_count += int(result.get("skipped_count", 0))
        imported.append({**_preview_candidate(parsed), "result": result})
    missing_ids = sorted(selected_ids - {str(candidate.get("candidate_id")) for candidate in selected})
    return {
        "ok": True,
        "mode": mode,
        "imported": imported,
        "imported_count": len(imported),
        "message_count": message_count,
        "inserted_count": inserted_count,
        "duplicate_count": duplicate_count,
        "skipped_count": skipped_count,
        "missing_candidate_ids": missing_ids,
        "candidate_count": scan.get("candidate_count", 0),
        "source": CODEX_DESKTOP_SOURCE,
    }


def parse_codex_session_file(path: Path, *, max_messages: int = DEFAULT_MAX_MESSAGES) -> dict[str, Any] | None:
    if not _is_supported_session_file(path):
        return None
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    file_id = _short_hash(_path_key(path), 24)
    metadata: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        _collect_jsonl_messages(text, file_id=file_id, metadata=metadata, messages=messages, max_messages=max_messages)
        source_format = "jsonl"
    elif suffix == ".json":
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return None
        _collect_json_messages(document, file_id=file_id, metadata=metadata, messages=messages, max_messages=max_messages)
        source_format = "json"
    else:
        _collect_markdown_messages(text, file_id=file_id, metadata=metadata, messages=messages, max_messages=max_messages)
        source_format = "markdown"
    if not messages:
        return None
    first_seen = _first_text(message.get("timestamp") for message in messages)
    last_seen = _last_text(message.get("timestamp") for message in messages)
    session_id = _metadata_text(metadata, "session_id") or file_id
    cwd = _metadata_text(metadata, "cwd") or _metadata_text(metadata, "workspace")
    title = _metadata_text(metadata, "title") or _friendly_title(path, session_id)
    candidate = {
        "candidate_id": file_id,
        "conversation_id": f"codex-desktop:{_safe_id(session_id or file_id)}"[:192],
        "session_id": session_id,
        "title": title,
        "source": CODEX_DESKTOP_SOURCE,
        "source_path": str(path),
        "source_format": source_format,
        "source_size": int(stat.st_size),
        "source_mtime": _iso_from_timestamp(stat.st_mtime),
        "message_count": len(messages),
        "role_counts": _role_counts(messages),
        "first_seen": first_seen,
        "last_seen": last_seen or _iso_from_timestamp(stat.st_mtime),
        "project_id": _project_id_from_path(cwd),
        "project_name": _project_name_from_path(cwd),
        "project_path": cwd,
        "project_workspace": _workspace_from_path(cwd),
        "messages": messages,
    }
    return candidate


def default_codex_session_roots() -> list[Path]:
    roots: list[Path] = []
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        roots.extend([Path(codex_home) / "sessions", Path(codex_home)])
    home = Path.home()
    roots.extend([home / ".codex" / "sessions", home / ".codex" / "history"])
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        base_path = Path(base)
        roots.extend(
            [
                base_path / "Codex" / "sessions",
                base_path / "Codex",
                base_path / "OpenAI" / "Codex" / "sessions",
                base_path / "OpenAI" / "Codex",
            ]
        )
    return _dedupe_paths(roots)


def _expanded_roots(roots: list[str] | None) -> list[Path]:
    if roots:
        expanded = [Path(os.path.expandvars(item)).expanduser() for item in roots if isinstance(item, str) and item.strip()]
        return _dedupe_paths(expanded)
    return default_codex_session_roots()


def _iter_candidate_files(root: Path, *, max_depth: int) -> list[Path]:
    if root.is_file():
        return [root] if _is_supported_session_file(root) else []
    if not root.is_dir():
        return []
    result: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth or _is_sensitive_path(current):
            continue
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for child in children:
            if _is_sensitive_path(child):
                continue
            try:
                if child.is_dir() and depth < max_depth and not child.is_symlink():
                    stack.append((child, depth + 1))
                elif child.is_file() and _is_supported_session_file(child):
                    result.append(child)
            except OSError:
                continue
    return sorted(result, key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)


def _is_supported_session_file(path: Path) -> bool:
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return False
    if _is_sensitive_path(path):
        return False
    if path.suffix.lower() in {".md", ".txt"}:
        return bool(SESSION_NAME_RE.search(path.name))
    return True


def _is_sensitive_path(path: Path) -> bool:
    return bool(SENSITIVE_PATH_RE.search(str(path)))


def _collect_jsonl_messages(
    text: str,
    *,
    file_id: str,
    metadata: dict[str, Any],
    messages: list[dict[str, Any]],
    max_messages: int,
) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if len(messages) >= max_messages:
            return
        raw = line.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _merge_record_metadata(record, metadata)
        message = _message_from_record(record, timestamp_hint=_record_timestamp(record), source_index=f"line-{line_number}")
        if message:
            _append_message(messages, message, file_id=file_id, source_index=f"line-{line_number}", max_messages=max_messages)


def _collect_json_messages(
    value: Any,
    *,
    file_id: str,
    metadata: dict[str, Any],
    messages: list[dict[str, Any]],
    max_messages: int,
    depth: int = 0,
) -> None:
    if len(messages) >= max_messages or depth > 6:
        return
    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            _collect_json_messages(item, file_id=file_id, metadata=metadata, messages=messages, max_messages=max_messages, depth=depth + 1)
            if len(messages) >= max_messages:
                return
        return
    if not isinstance(value, dict):
        return
    _merge_record_metadata(value, metadata)
    message = _message_from_record(value, timestamp_hint=_record_timestamp(value), source_index=f"json-{len(messages) + 1}")
    if message:
        _append_message(messages, message, file_id=file_id, source_index=f"json-{len(messages) + 1}", max_messages=max_messages)
        return
    for key in ("messages", "items", "events", "entries", "turns", "records", "data"):
        child = value.get(key)
        if isinstance(child, (list, dict)):
            _collect_json_messages(child, file_id=file_id, metadata=metadata, messages=messages, max_messages=max_messages, depth=depth + 1)
    mapping = value.get("mapping")
    if isinstance(mapping, dict):
        _collect_json_messages(list(mapping.values()), file_id=file_id, metadata=metadata, messages=messages, max_messages=max_messages, depth=depth + 1)
    conversation = value.get("conversation")
    if isinstance(conversation, (list, dict)):
        _collect_json_messages(conversation, file_id=file_id, metadata=metadata, messages=messages, max_messages=max_messages, depth=depth + 1)


def _collect_markdown_messages(
    text: str,
    *,
    file_id: str,
    metadata: dict[str, Any],
    messages: list[dict[str, Any]],
    max_messages: int,
) -> None:
    role_re = re.compile(r"^(?:#{1,6}\s*)?(user|assistant|system|tool|human|ai)\s*:?\s*(.*)$", re.I)
    current_role: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_role, current_lines
        if current_role and current_lines:
            content = "\n".join(current_lines).strip()
            if content:
                index = f"md-{len(messages) + 1}"
                _append_message(
                    messages,
                    {"role": _normalized_role(current_role), "content": content, "metadata": {"source_index": index}},
                    file_id=file_id,
                    source_index=index,
                    max_messages=max_messages,
                )
        current_role = None
        current_lines = []

    for line in text.splitlines():
        if len(messages) >= max_messages:
            return
        match = role_re.match(line.strip())
        if match:
            flush()
            current_role = match.group(1)
            if match.group(2):
                current_lines.append(match.group(2))
        elif current_role:
            current_lines.append(line)
    flush()
    if messages:
        metadata.setdefault("title", "Markdown transcript")


def _message_from_record(record: dict[str, Any], *, timestamp_hint: str | None, source_index: str) -> dict[str, Any] | None:
    payload = record.get("payload")
    record_type = _record_type(record)
    if isinstance(payload, dict):
        nested = _message_from_record(payload, timestamp_hint=timestamp_hint or _record_timestamp(record), source_index=source_index)
        if nested:
            nested.setdefault("metadata", {})["source_record_type"] = record_type or "payload"
            return nested
    content_value = _first_present(record, ("content", "text", "message", "output", "markdown"))
    role = _role_from_record(record)
    if role is None and record_type:
        if "user" in record_type:
            role = "user"
        elif "assistant" in record_type or "agent" in record_type:
            role = "assistant"
        elif "system" in record_type:
            role = "system"
        elif "tool" in record_type or "function_call_output" in record_type:
            role = "tool"
    content = _content_to_text(content_value)
    if not role or not content:
        return None
    message_id = _first_present(record, ("message_id", "id", "uuid", "event_id", "call_id"))
    return {
        "message_id": str(message_id) if message_id else None,
        "role": role,
        "timestamp": _record_timestamp(record) or timestamp_hint,
        "content": content,
        "source": CODEX_DESKTOP_SOURCE,
        "metadata": {"source_index": source_index, "source_record_type": record_type or None},
    }


def _append_message(
    messages: list[dict[str, Any]],
    message: dict[str, Any],
    *,
    file_id: str,
    source_index: str,
    max_messages: int,
) -> None:
    if len(messages) >= max_messages:
        return
    message = dict(message)
    if not message.get("message_id"):
        message["message_id"] = f"codex:{file_id}:{source_index}"
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    metadata.update({"candidate_id": file_id, "source_index": source_index})
    message["metadata"] = metadata
    messages.append(message)


def _merge_record_metadata(record: Any, metadata: dict[str, Any]) -> None:
    if not isinstance(record, dict):
        return
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    record_type = _record_type(record)
    if record_type == "session_meta" and isinstance(payload, dict):
        for source_key, target_key in (
            ("id", "session_id"),
            ("session_id", "session_id"),
            ("title", "title"),
            ("name", "title"),
            ("cwd", "cwd"),
            ("workspace", "workspace"),
            ("timestamp", "created_at"),
            ("originator", "originator"),
            ("cli_version", "cli_version"),
            ("source", "codex_source"),
        ):
            value = payload.get(source_key)
            if isinstance(value, str) and value.strip():
                metadata.setdefault(target_key, sanitized_text(value).strip())
    for source_key, target_key in (("conversation_id", "session_id"), ("session_id", "session_id"), ("title", "title")):
        value = record.get(source_key)
        if isinstance(value, str) and value.strip():
            metadata.setdefault(target_key, sanitized_text(value).strip())


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return sanitized_text(value).strip()
    if isinstance(value, list):
        parts = [_content_to_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output", "markdown"):
            if key in value:
                return _content_to_text(value.get(key))
        item_type = str(value.get("type") or "")
        if "image" in item_type.lower():
            return "[image]"
    return ""


def _record_type(record: dict[str, Any]) -> str:
    for key in ("type", "event", "kind"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _record_timestamp(record: dict[str, Any]) -> str | None:
    value = _first_present(record, ("timestamp", "created_at", "time", "date"))
    return sanitized_text(value).strip() if value else None


def _role_from_record(record: dict[str, Any]) -> str | None:
    value = _first_present(record, ("role", "author_role", "sender", "from"))
    if isinstance(value, dict):
        value = value.get("role") or value.get("name")
    if not isinstance(value, str) or not value.strip():
        author = record.get("author")
        if isinstance(author, dict):
            value = author.get("role") or author.get("name")
    if not isinstance(value, str) or not value.strip():
        return None
    return _normalized_role(value)


def _normalized_role(value: str) -> str:
    role = value.strip().lower()
    return ROLE_ALIASES.get(role, role)[:64]


def _first_present(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _preview_candidate(parsed: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in parsed.items() if key != "messages"}


def _project_metadata(parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": CODEX_DESKTOP_SOURCE,
        "candidate_id": parsed.get("candidate_id"),
        "session_id": parsed.get("session_id"),
        "source_path": parsed.get("source_path"),
        "source_format": parsed.get("source_format"),
        "source_mtime": parsed.get("source_mtime"),
    }


def _root_info(root: Path) -> dict[str, Any]:
    return {"path": str(root), "exists": root.exists(), "is_dir": root.is_dir(), "is_file": root.is_file()}


def _path_key(path: Path) -> str:
    try:
        text = str(path.resolve())
    except OSError:
        text = str(path)
    return text.lower() if os.name == "nt" else text


def _short_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", sanitized_text(value)).strip(".-:") or "session"


def _friendly_title(path: Path, session_id: str) -> str:
    stem = re.sub(r"[_-]+", " ", path.stem).strip()
    return stem or f"Codex session {session_id[:8]}"


def _metadata_text(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return sanitized_text(value).strip() if isinstance(value, str) and value.strip() else None


def _first_text(values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _last_text(values: Any) -> str | None:
    result: str | None = None
    for value in values:
        if isinstance(value, str) and value.strip():
            result = value
    return result


def _role_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role") or "message")
        counts[role] = counts.get(role, 0) + 1
    return counts


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _project_id_from_path(value: str | None) -> str:
    if not value:
        return "codex-desktop"
    name = _project_name_from_path(value)
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip(".-") or "codex-desktop"


def _project_name_from_path(value: str | None) -> str:
    if not value:
        return "Codex Desktop"
    text = value.replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1] or "Codex Desktop"


def _workspace_from_path(value: str | None) -> str | None:
    if not value:
        return None
    text = value.replace("\\", "/").rstrip("/")
    if "/" not in text:
        return None
    return text.rsplit("/", 1)[0]


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result
