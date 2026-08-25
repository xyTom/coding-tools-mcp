"""Central producer boundary for bounded Conversation evidence."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .conversation_evidence import MAX_TEXT_CHARS, compact_metadata, evidence_digest, evidence_entry
from .transcript import TranscriptStore, TranscriptStoreError


EVIDENCE_RETENTION_LIMITS = {
    "task_instruction": 20,
    "explored_path": 100,
    "changed_path": 100,
    "validation": 40,
    "failure": 40,
    "job_state": 100,
    "approval_state": 100,
    "checkpoint": 20,
    "guidance": 20,
    "handoff_note": 20,
}
EVIDENCE_HARD_CAP = 500


class ConversationEvidenceRecorder:
    """Normalize evidence and own all retention policy for TranscriptStore."""

    def __init__(
        self,
        store: TranscriptStore,
        *,
        limits: Mapping[str, int] | None = None,
        hard_cap: int = EVIDENCE_HARD_CAP,
    ) -> None:
        self.store = store
        self.limits = dict(limits or EVIDENCE_RETENTION_LIMITS)
        self.hard_cap = int(hard_cap)

    def current_attempt(self, workspace_id: str, conversation_id: str) -> str | None:
        lookup = getattr(self.store, "latest_instruction_attempt", None)
        if callable(lookup):
            return lookup(workspace_id, conversation_id)
        # Compatibility for narrow test doubles that do not implement the
        # indexed Store query.
        contexts = self.store.list_recent_context(workspace_id, conversation_id, limit=200)
        for item in contexts:
            metadata = item.get("metadata")
            if (
                item.get("kind") == "task_instruction"
                and isinstance(metadata, Mapping)
                and isinstance(metadata.get("attempt_id"), str)
            ):
                return str(metadata["attempt_id"])
        return None

    def record(
        self,
        workspace_id: str,
        conversation_id: str,
        kind: str,
        content: Any,
        metadata: Mapping[str, Any] | None = None,
        *,
        source: str = "conversation-evidence",
    ) -> dict[str, Any] | None:
        entry = evidence_entry(kind, content, metadata)
        if entry is None:
            return None
        values = dict(entry["metadata"])
        if kind == "task_instruction":
            attempt_id = f"attempt-{uuid.uuid4().hex}"
        else:
            attempt_id = self.current_attempt(workspace_id, conversation_id)
            if attempt_id is None:
                # Keep orphan operational facts attributable without inventing a task.
                attempt_id = "no-instruction"
        values["attempt_id"] = attempt_id

        if kind == "approval_state":
            approval_id = values.get("approval_id")
            session_id = values.get("session_id")
            stable_value = (
                f"{session_id}:{approval_id}"
                if isinstance(session_id, str) and session_id and isinstance(approval_id, str) and approval_id
                else approval_id
            )
        else:
            stable_value = values.get("job_id") if kind == "job_state" else values.get("stable_id")
        stable_id = str(stable_value) if isinstance(stable_value, str) and stable_value else ""
        upsert = False
        if kind in {"job_state", "approval_state"}:
            if not stable_id:
                stable_id = evidence_digest({**entry, "metadata": values})
            upsert = True
            context_id = f"state:{kind}:{stable_id}"[:256]
        elif kind in {"explored_path", "changed_path"}:
            context_id = f"dedupe:{kind}:{attempt_id}:{evidence_digest({**entry, 'metadata': values})}"[:256]
        elif values.get("stable_id"):
            context_id = f"event:{kind}:{values['stable_id']}"[:256]
        else:
            context_id = f"event:{kind}:{uuid.uuid4().hex}"[:256]
        values["stable_id"] = stable_id or context_id
        entry["metadata"] = compact_metadata(values)
        entry["context_id"] = context_id
        result = self.store.record_context(
            workspace_id,
            conversation_id,
            [entry],
            source=source,
            upsert_context_ids={context_id} if upsert else None,
        )
        self.store.prune_context(
            workspace_id,
            conversation_id,
            limits=self.limits,
            hard_cap=self.hard_cap,
        )
        return result

    def record_instruction(self, workspace_id: str, conversation_id: str, instruction: str) -> bool:
        try:
            result = self.record(
                workspace_id,
                conversation_id,
                "task_instruction",
                instruction,
                source="agent-turn",
            )
        except (TranscriptStoreError, OSError):
            return False
        return result is not None


__all__ = [
    "EVIDENCE_HARD_CAP",
    "EVIDENCE_RETENTION_LIMITS",
    "ConversationEvidenceRecorder",
    "MAX_TEXT_CHARS",
]
