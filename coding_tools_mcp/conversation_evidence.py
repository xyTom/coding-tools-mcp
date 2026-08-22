"""Bounded evidence normalization and pure continuation projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


MAX_TEXT_CHARS = 512
MAX_PATHS = 50
MAX_FAILURES = 20
MAX_METADATA_JSON_CHARS = 2048
MAX_HANDOFF_BYTES = 8192
EVIDENCE_KINDS = frozenset(
    {
        "task_instruction",
        "explored_path",
        "changed_path",
        "validation",
        "failure",
        "job_state",
        "approval_state",
        "checkpoint",
        "guidance",
        "handoff_note",
    }
)


def compact_text(value: Any, *, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.replace("\x00", "").split())
    return normalized[:limit]


def compact_path(value: Any) -> str:
    path = compact_text(value, limit=512).replace("\\", "/").strip("/")
    segments = [part for part in path.split("/") if part]
    if (
        not path
        or path in {".", ".."}
        or ":" in path
        or path.startswith("//")
        or ".." in segments
    ):
        return ""
    return "/".join(segments[-8:])


def compact_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = ("operation", "recipe", "status", "check", "job_id", "approval_id", "phase")
    result = {key: value[key] for key in allowed if key in value}
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return {}
    if len(encoded.encode("utf-8")) > MAX_METADATA_JSON_CHARS:
        return {"truncated": True}
    return result


def evidence_entry(kind: str, content: Any, metadata: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    if kind not in EVIDENCE_KINDS:
        return None
    if kind in {"explored_path", "changed_path"}:
        path = compact_path(content)
        if not path:
            return None
        text = path
    else:
        text = compact_text(content)
        if not text:
            return None
    return {
        "kind": kind,
        "content": text,
        "metadata": compact_metadata(metadata),
    }


def _entry_value(entries: Iterable[Mapping[str, Any]], kind: str, *, newest: bool = True) -> Mapping[str, Any] | None:
    ordered = list(entries) if newest else list(entries)[::-1]
    for entry in ordered:
        if entry.get("kind") == kind:
            return entry
    return None


def _unique_paths(entries: Iterable[Mapping[str, Any]], kind: str) -> list[str]:
    result: list[str] = []
    for entry in entries:
        if entry.get("kind") != kind:
            continue
        path = compact_path(entry.get("content"))
        if path and path not in result:
            result.append(path)
        if len(result) >= MAX_PATHS:
            break
    return result


def continuation_feedback(detail: Mapping[str, Any], executions: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Project durable evidence without repository I/O or side effects."""

    contexts = [item for item in detail.get("contexts", []) if isinstance(item, Mapping)]
    execution_list = [item for item in executions if isinstance(item, Mapping)]
    current = execution_list[-1] if execution_list else {}
    instruction = _entry_value(contexts, "task_instruction")
    validation = _entry_value(contexts, "validation")
    failure = _entry_value(contexts, "failure")
    jobs = [item for item in contexts if item.get("kind") == "job_state"]
    approvals = [item for item in contexts if item.get("kind") == "approval_state"]
    validation_status = "unknown"
    if isinstance(validation, Mapping):
        raw_status = compact_text(validation.get("content"), limit=32).lower()
        validation_status = raw_status if raw_status in {"passed", "failed"} else "unknown"
    changed = _unique_paths(contexts, "changed_path")
    explored = _unique_paths(contexts, "explored_path")
    failures: list[str] = []
    if isinstance(failure, Mapping):
        text = compact_text(failure.get("content"), limit=256)
        if text:
            failures.append(text)
    suggested: list[str] = []
    if failures:
        suggested.append("resolve_failure")
    if approvals:
        suggested.append("resolve_approval")
    if jobs:
        suggested.append("observe_existing_job")
    if changed and validation_status != "passed":
        suggested.append("validate_changed_work")
    if not suggested:
        suggested.append("continue_latest_instruction")
    return {
        "status": "available" if instruction or execution_list else "empty",
        "previous_instruction": compact_text(instruction.get("content")) if instruction else "",
        "execution": {
            "session_id": current.get("session_id"),
            "status": current.get("status"),
            "last_turn_id": current.get("last_turn_id"),
        },
        "exploration": {"paths": explored},
        "changes": {"paths": changed},
        "validation": {"status": validation_status, "failures": failures},
        "jobs": {"active": len(jobs[:MAX_FAILURES])},
        "approvals": {"pending": len(approvals[:MAX_FAILURES])},
        "suggested_actions": suggested,
    }


def handoff_brief(detail: Mapping[str, Any], executions: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    feedback = continuation_feedback(detail, executions)
    brief = {
        "version": 1,
        "conversation_id": detail.get("conversation_id"),
        "instruction": feedback["previous_instruction"],
        "execution": feedback["execution"],
        "changed_paths": feedback["changes"]["paths"],
        "explored_paths": feedback["exploration"]["paths"],
        "validation": feedback["validation"],
        "jobs": feedback["jobs"],
        "approvals": feedback["approvals"],
        "next_actions": feedback["suggested_actions"],
        "note": "Bounded durable evidence only; hidden model context is not reconstructed.",
    }
    encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_HANDOFF_BYTES:
        return brief
    while len(encoded) > MAX_HANDOFF_BYTES and (brief["changed_paths"] or brief["explored_paths"]):
        if len(brief["changed_paths"]) >= len(brief["explored_paths"]) and brief["changed_paths"]:
            brief["changed_paths"].pop()
        elif brief["explored_paths"]:
            brief["explored_paths"].pop()
        encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    while len(encoded) > MAX_HANDOFF_BYTES:
        brief["instruction"] = brief["instruction"][: max(0, len(brief["instruction"]) - 128)]
        encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    brief["truncated"] = True
    return brief


def evidence_digest(entry: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": entry.get("kind"), "content": entry.get("content"), "metadata": entry.get("metadata", {})},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


__all__ = [
    "EVIDENCE_KINDS",
    "MAX_HANDOFF_BYTES",
    "continuation_feedback",
    "evidence_digest",
    "evidence_entry",
    "handoff_brief",
]
