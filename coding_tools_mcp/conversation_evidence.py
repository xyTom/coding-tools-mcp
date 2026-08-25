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
ACTIVE_JOB_STATUSES = frozenset({"active", "recovering", "running", "queued", "starting"})
PENDING_APPROVAL_STATUSES = frozenset({"pending", "requested", "required"})
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
    if not path or path in {".", ".."} or ":" in path or path.startswith("//") or ".." in segments:
        return ""
    return "/".join(segments[-8:])


def compact_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = (
        "attempt_id", "stable_id", "operation", "recipe", "status", "check",
        "job_id", "approval_id", "session_id", "old_path", "phase",
    )
    result = {key: value[key] for key in allowed if key in value}
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return {}
    return {} if len(encoded.encode("utf-8")) > MAX_METADATA_JSON_CHARS else result


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
    return {"kind": kind, "content": text, "metadata": compact_metadata(metadata)}


def _ordered_contexts(detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    contexts = [item for item in detail.get("contexts", []) if isinstance(item, Mapping)]
    if len(contexts) >= 2 and all(
        isinstance(item.get("updated_at") or item.get("created_at"), (int, float))
        for item in contexts
    ):
        contexts.sort(
            key=lambda item: (
                float(item.get("updated_at") or item.get("created_at") or 0),
                str(item.get("context_id")),
            ),
            reverse=True,
        )
    return contexts


def _entry_value(entries: Iterable[Mapping[str, Any]], kind: str) -> Mapping[str, Any] | None:
    for entry in entries:
        if entry.get("kind") == kind:
            return entry
    return None


def _bounded_paths(contexts: Iterable[Mapping[str, Any]], kind: str) -> tuple[list[str], int, bool]:
    unique: list[str] = []
    for item in contexts:
        if item.get("kind") != kind:
            continue
        path = compact_path(item.get("content"))
        if path and path not in unique:
            unique.append(path)
    return unique[:MAX_PATHS], len(unique), len(unique) > MAX_PATHS


def _collapse_states(contexts: Iterable[Mapping[str, Any]], kind: str, id_key: str) -> list[dict[str, Any]]:
    collapsed: dict[str, dict[str, Any]] = {}
    for item in contexts:
        if item.get("kind") != kind:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        identity = str(
            metadata.get("stable_id")
            or metadata.get(id_key)
            or item.get("context_id")
            or evidence_digest(item)
        )
        updated_at = float(item.get("updated_at") or item.get("created_at") or 0)
        projected = {
            "id": identity,
            "approval_id": str(metadata.get("approval_id") or ""),
            "status": compact_text(item.get("content"), limit=64),
            "attempt_id": str(metadata.get("attempt_id") or ""),
            "session_id": str(metadata.get("session_id") or ""),
            "updated_at": updated_at,
        }
        previous = collapsed.get(identity)
        previous_updated_at = (
            float(previous["updated_at"])
            if previous is not None and isinstance(previous.get("updated_at"), (int, float))
            else 0.0
        )
        if previous is None or updated_at >= previous_updated_at:
            collapsed[identity] = projected
    return list(collapsed.values())


def continuation_feedback(detail: Mapping[str, Any], executions: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Project durable evidence without repository I/O or side effects."""

    contexts = _ordered_contexts(detail)
    execution_list = [item for item in executions if isinstance(item, Mapping)]
    current = max(execution_list, key=lambda item: float(item.get("updated_at") or 0), default={})
    instruction = _entry_value(contexts, "task_instruction")
    metadata = instruction.get("metadata") if isinstance(instruction, Mapping) else {}
    current_attempt = str(metadata.get("attempt_id") or "") if isinstance(metadata, Mapping) else ""
    scoped = [
        item
        for item in contexts
        if not current_attempt or str(item.get("metadata", {}).get("attempt_id") or "") == current_attempt
    ]
    validation = _entry_value(scoped, "validation")
    validation_status = "unknown"
    if isinstance(validation, Mapping):
        raw_status = compact_text(validation.get("content"), limit=32).lower()
        validation_status = raw_status if raw_status in {"passed", "failed"} else "unknown"
    changed, changed_total, changed_truncated = _bounded_paths(scoped, "changed_path")
    explored, explored_total, explored_truncated = _bounded_paths(scoped, "explored_path")
    failures = [
        compact_text(item.get("content"), limit=256)
        for item in scoped
        if item.get("kind") == "failure" and compact_text(item.get("content"), limit=256)
    ][:MAX_FAILURES]
    jobs = _collapse_states(scoped, "job_state", "job_id")
    approvals = _collapse_states(scoped, "approval_state", "approval_id")
    active_jobs = [item for item in jobs if item["status"].lower() in ACTIVE_JOB_STATUSES]
    recovering_jobs = [item for item in jobs if item["status"].lower() == "recovering"]
    pending_approvals = [item for item in approvals if item["status"].lower() in PENDING_APPROVAL_STATUSES]
    unresolved_failures: list[str] = []
    for item in scoped:
        if item.get("kind") == "validation" and compact_text(item.get("content"), limit=32).lower() == "passed":
            break
        if item.get("kind") in {"failure"} or (
            item.get("kind") == "validation"
            and compact_text(item.get("content"), limit=32).lower() == "failed"
        ):
            value = compact_text(item.get("content"), limit=256)
            if value:
                unresolved_failures.append(value)
    unresolved_failures = unresolved_failures[:MAX_FAILURES]
    checkpoints = [
        compact_text(item.get("content"), limit=256)
        for item in scoped
        if item.get("kind") == "checkpoint"
    ][:20]
    suggested: list[str] = []
    if unresolved_failures:
        suggested.append("resolve_failure")
    if pending_approvals:
        suggested.append("resolve_approval")
    if active_jobs:
        suggested.append("observe_existing_job")
    if changed and validation_status != "passed":
        suggested.append("validate_changed_work")
    if not suggested:
        suggested.append("continue_latest_instruction")
    return {
        "status": "available" if instruction or execution_list else "empty",
        "attempt": {"attempt_id": current_attempt},
        "previous_instruction": compact_text(instruction.get("content")) if instruction else "",
        "execution": {
            "session_id": current.get("session_id"),
            "status": current.get("status"),
            "last_turn_id": current.get("last_turn_id"),
        },
        "exploration": {"paths": explored, "total": explored_total, "truncated": explored_truncated},
        "changes": {"paths": changed, "total": changed_total, "truncated": changed_truncated},
        "validation": {
            "status": validation_status,
            "failures": failures,
            "unresolved_failures": unresolved_failures,
            "unresolved_failure_count": len(unresolved_failures),
            "truncated": len(unresolved_failures) >= MAX_FAILURES,
        },
        "jobs": {
            "active": len(active_jobs),
            "recovering": len(recovering_jobs),
            "total": len(active_jobs),
            "truncated": len(active_jobs) > 20,
            "items": active_jobs[:20],
        },
        "approvals": {
            "pending": len(pending_approvals),
            "total": len(pending_approvals),
            "truncated": len(pending_approvals) > 20,
            "items": pending_approvals[:20],
        },
        "checkpoints": checkpoints,
        "suggested_actions": suggested,
    }


def handoff_brief(detail: Mapping[str, Any], executions: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    feedback = continuation_feedback(detail, executions)
    nested_conversation = detail.get("conversation")
    conversation_id = detail.get("conversation_id")
    if not conversation_id and isinstance(nested_conversation, Mapping):
        conversation_id = nested_conversation.get("conversation_id")
    brief: dict[str, Any] = {
        "version": 1,
        "conversation_id": conversation_id,
        "instruction": feedback["previous_instruction"],
        "execution": feedback["execution"],
        "changed_paths": feedback["changes"]["paths"],
        "explored_paths": feedback["exploration"]["paths"],
        "validation": feedback["validation"],
        "jobs": feedback["jobs"],
        "approvals": feedback["approvals"],
        "checkpoints": feedback["checkpoints"],
        "next_actions": feedback["suggested_actions"],
        "note": "Bounded durable evidence only; hidden model context is not reconstructed.",
    }

    def encoded() -> bytes:
        return json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    shrunk = False
    while len(encoded()) > MAX_HANDOFF_BYTES:
        if brief["changed_paths"]:
            brief["changed_paths"].pop()
        elif brief["explored_paths"]:
            brief["explored_paths"].pop()
        elif brief["next_actions"]:
            brief["next_actions"].pop()
        elif brief["instruction"]:
            brief["instruction"] = brief["instruction"][:-128]
        elif "note" in brief:
            del brief["note"]
        else:
            minimal = {"version": 1, "conversation_id": conversation_id, "truncated": True}
            assert len(json.dumps(minimal, separators=(",", ":")).encode("utf-8")) <= MAX_HANDOFF_BYTES
            return minimal
        shrunk = True
    if shrunk:
        brief["truncated"] = True
    assert len(encoded()) <= MAX_HANDOFF_BYTES
    return brief


def evidence_digest(entry: Mapping[str, Any]) -> str:
    payload = {"kind": entry.get("kind"), "content": entry.get("content"), "metadata": entry.get("metadata", {})}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "EVIDENCE_KINDS",
    "MAX_HANDOFF_BYTES",
    "PENDING_APPROVAL_STATUSES",
    "continuation_feedback",
    "evidence_digest",
    "evidence_entry",
    "handoff_brief",
]
