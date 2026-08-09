"""Deterministic bounded handoff projection for durable Agent Sessions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .agent_session_store import AgentSessionRecord
from .workspace_catalog import WorkspaceEntry


MAX_HANDOFF_CHANGED_PATHS = 100
MAX_HANDOFF_CONTEXT_CHANGES = 20
MAX_HANDOFF_JOBS = 50
MAX_HANDOFF_FAILURE_MESSAGE = 300


def build_session_handoff(
    record: AgentSessionRecord,
    workspace: WorkspaceEntry,
    *,
    events: Iterable[Mapping[str, Any]] = (),
    validation: Mapping[str, Any] | None = None,
    jobs: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Project stable runtime facts without transcript or secret material."""

    event_items = [dict(item) for item in events][-500:]
    fingerprint = dict(record.repo_fingerprint or {})
    unresolved_approval = _unresolved_approval(event_items)
    recent_failure = _recent_failure(event_items)
    active_jobs = _active_jobs(jobs)
    validation_payload = _validation_payload(validation)
    context_changed = bool(fingerprint.get("context_changed", False))
    context_changes = _bounded_strings(
        fingerprint.get("changes"),
        MAX_HANDOFF_CONTEXT_CHANGES,
        200,
    )
    changed_paths = _bounded_strings(
        fingerprint.get("changed_paths"),
        MAX_HANDOFF_CHANGED_PATHS,
        512,
    )

    payload: dict[str, Any] = {
        "version": 1,
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "target": workspace.target,
            "runner_id": workspace.runner_id,
        },
        "agent_session": {
            "session_id": record.session_id,
            "backend_kind": record.backend_kind,
            "status": record.status,
            "conversation_id": record.conversation_id,
            "last_turn_id": record.last_turn_id,
        },
        "repository": {
            "branch": _optional_bounded_string(fingerprint.get("branch"), 256),
            "head": _optional_bounded_string(fingerprint.get("head"), 128),
            "changed_paths": changed_paths,
            "git_status_summary": {
                "changed_path_count": len(changed_paths),
                "worktree_digest": _optional_bounded_string(
                    fingerprint.get("worktree_digest"),
                    128,
                ),
            },
            "context_changed": context_changed,
            "context_changes": context_changes,
        },
        "validation": validation_payload,
        "recent_failure": recent_failure,
        "active_jobs": active_jobs,
        "unresolved_approval": unresolved_approval,
    }
    payload["next_actions"] = _next_actions(payload)
    return payload


def _validation_payload(validation: Mapping[str, Any] | None) -> dict[str, Any]:
    if validation is None:
        return {"status": "unavailable", "recipe": None}
    return {
        "status": _optional_bounded_string(validation.get("status"), 32) or "unavailable",
        "recipe": _optional_bounded_string(validation.get("recipe"), 128),
        "exit_code": (
            validation.get("exit_code")
            if isinstance(validation.get("exit_code"), int)
            and not isinstance(validation.get("exit_code"), bool)
            else None
        ),
    }


def _active_jobs(jobs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for item in jobs:
        status = _optional_bounded_string(item.get("status"), 32)
        if status not in {"running", "recovering"}:
            continue
        job_id = _optional_bounded_string(item.get("job_id") or item.get("id"), 256)
        if not job_id:
            continue
        active.append({"job_id": job_id, "status": status})
        if len(active) >= MAX_HANDOFF_JOBS:
            break
    return active


def _unresolved_approval(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    resolved: set[str] = set()
    for event in events:
        if event.get("method") != "approval/resolved":
            continue
        approval_id = _approval_id(event)
        if approval_id:
            resolved.add(approval_id)
    for event in reversed(events):
        if event.get("kind") != "approval" and event.get("method") != "approval/requested":
            continue
        approval_id = _approval_id(event)
        if not approval_id or approval_id in resolved:
            continue
        params = event.get("params") if isinstance(event.get("params"), Mapping) else {}
        return {
            "approval_id": approval_id,
            "reason": _optional_bounded_string(
                params.get("reason") or params.get("message"),
                300,
            ),
        }
    return None


def _recent_failure(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("kind") != "error" and event.get("method") not in {
            "turn/failed",
            "session/error",
        }:
            continue
        params = event.get("params") if isinstance(event.get("params"), Mapping) else {}
        return {
            "code": _optional_bounded_string(params.get("code"), 128),
            "message": _optional_bounded_string(
                params.get("message") or params.get("reason"),
                MAX_HANDOFF_FAILURE_MESSAGE,
            ),
            "retryable": bool(params.get("retryable", False)),
        }
    return None


def _approval_id(event: Mapping[str, Any]) -> str | None:
    value = event.get("approval_id")
    if value is None and isinstance(event.get("params"), Mapping):
        value = event["params"].get("approval_id")
    return _optional_bounded_string(value, 256)


def _next_actions(payload: Mapping[str, Any]) -> list[str]:
    actions: list[str] = []
    if payload.get("unresolved_approval") is not None:
        actions.append("resolve_approval")
    repository = payload.get("repository")
    if isinstance(repository, Mapping) and repository.get("context_changed"):
        actions.append("review_context_changes")
    jobs = payload.get("active_jobs")
    if isinstance(jobs, list) and any(
        isinstance(item, Mapping) and item.get("status") == "recovering"
        for item in jobs
    ):
        actions.append("wait_for_job_recovery")
    validation = payload.get("validation")
    if isinstance(validation, Mapping) and validation.get("status") == "failed":
        actions.append("fix_validation_failures")
    session = payload.get("agent_session")
    if isinstance(session, Mapping) and session.get("status") in {"failed", "unavailable"}:
        actions.append("resume_session")
    if not actions:
        actions.append("continue_session")
    return actions[:8]


def _bounded_strings(value: Any, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        result.append(item[:item_limit])
        if len(result) >= limit:
            break
    return result


def _optional_bounded_string(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


__all__ = ["build_session_handoff"]
