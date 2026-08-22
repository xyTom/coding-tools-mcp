"""Operator Web API application service for durable Agent Sessions.

The HTTP server owns authentication and transport framing. This module owns
the ordinary-user projection: authorized workspaces, bounded session payloads,
and an ephemeral multi-window event cursor over the live backend queue.
"""

from __future__ import annotations

import hashlib
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .agent_backends.base import AgentBackendEvent
from .agent_session_store import AgentSessionRecord
from .agent_sessions import AgentSessionService, AgentSessionServiceError
from .handoff import build_session_handoff
from .transcript import TranscriptStore, TranscriptStoreError
from .validation import ValidationBackend
from .workspace_catalog import WorkspaceCatalog, WorkspaceCatalogError, WorkspaceEntry


OPERATOR_API_PREFIX = "/api/app"
MAX_OPERATOR_EVENTS = 500
MAX_OPERATOR_EVENT_PULL = 100


class OperatorAPIError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class OperatorPrincipal:
    principal_id: str
    workspace_ids: tuple[str, ...]

    def can_access(self, workspace_id: str) -> bool:
        return workspace_id in self.workspace_ids


class OperatorAPIService:
    """Principal-scoped facade over :class:`AgentSessionService`.

    Agent backend queues are destructive reads. To support multiple browser
    windows without making AgentSession itself window-scoped, drained backend
    events are copied into one bounded per-session buffer and assigned a local
    monotonically increasing cursor.
    """

    def __init__(
        self,
        agent_sessions: AgentSessionService,
        workspace_catalog: WorkspaceCatalog,
        workspace_status: Callable[[WorkspaceEntry], Mapping[str, Any]] | None = None,
        handoff_jobs: (
            Callable[[OperatorPrincipal, WorkspaceEntry], Iterable[Mapping[str, Any]]] | None
        ) = None,
        validation_backend_factory: Callable[[WorkspaceEntry], ValidationBackend] | None = None,
        transcript_store: TranscriptStore | None = None,
    ) -> None:
        self.agent_sessions = agent_sessions
        self.workspace_catalog = workspace_catalog
        self.workspace_status = workspace_status
        self.handoff_jobs = handoff_jobs
        self.validation_backend_factory = validation_backend_factory
        self.transcript_store = transcript_store
        self._event_lock = threading.RLock()
        self._events: dict[str, deque[dict[str, Any]]] = {}
        self._next_sequence: dict[str, int] = {}
        self._validation_results: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self.agent_sessions.close()
        with self._event_lock:
            self._events.clear()
            self._next_sequence.clear()
            self._validation_results.clear()

    def list_workspaces(self, principal: OperatorPrincipal) -> dict[str, Any]:
        workspaces: list[dict[str, Any]] = []
        for workspace_id in principal.workspace_ids:
            try:
                entry = self.workspace_catalog.get(workspace_id)
            except WorkspaceCatalogError:
                continue
            workspaces.append(self._workspace_payload(entry))
        workspaces.sort(key=lambda item: (str(item["name"]).casefold(), str(item["id"])))
        return {"workspaces": workspaces}

    def list_sessions(
        self,
        principal: OperatorPrincipal,
        workspace_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._require_workspace(principal, workspace_id)
        records = self.agent_sessions.list_sessions(
            workspace_id,
            principal.principal_id,
            limit=max(1, min(int(limit), 200)),
        )
        summaries = self._conversation_summary_map(workspace_id)
        normalized_records: list[AgentSessionRecord] = []
        summaries_changed = False
        for record in records:
            if not record.conversation_id or record.conversation_id not in summaries:
                record = self._ensure_transcript_conversation(record)
                summaries_changed = True
            normalized_records.append(record)
        if summaries_changed:
            summaries = self._conversation_summary_map(workspace_id)
        return {
            "sessions": [
                self._session_summary_payload(record, summaries.get(record.conversation_id or ""))
                for record in normalized_records
            ]
        }

    def create_session(
        self,
        principal: OperatorPrincipal,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_id = self._required_string(body.get("workspace_id"), "workspace_id", 128)
        self._require_workspace(principal, workspace_id)
        raw_backend = self._required_string(body.get("backend_kind", "codex"), "backend_kind", 128)
        backend_kind = {
            "codex": "codex-app-server",
            "codex-app-server": "codex-app-server",
        }.get(raw_backend)
        if backend_kind is None:
            raise OperatorAPIError(
                "operator_backend_unsupported",
                "Unsupported Agent backend.",
                status=400,
            )
        instructions = body.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise OperatorAPIError(
                "operator_invalid_request",
                "instructions must be a string.",
                status=400,
            )
        record = self.agent_sessions.create_session(
            workspace_id=workspace_id,
            owner_principal_id=principal.principal_id,
            backend_kind=backend_kind,
            instructions=instructions,
        )
        record = self._ensure_transcript_conversation(record)
        return {"session": self._detail_payload(record)}

    def get_session(
        self,
        principal: OperatorPrincipal,
        session_id: str,
        *,
        resume: bool = True,
    ) -> dict[str, Any]:
        session_id = self._required_string(session_id, "session_id", 256)
        record = self.agent_sessions.get_session(session_id, principal.principal_id)
        record = self._ensure_transcript_conversation(record)
        self._require_workspace(principal, record.workspace_id)
        backend_error: dict[str, Any] | None = None
        if resume and record.status != "closed":
            try:
                record = self.agent_sessions.resume_session(session_id, principal.principal_id)
            except AgentSessionServiceError as exc:
                if not exc.retryable:
                    raise
                record = self.agent_sessions.get_session(session_id, principal.principal_id)
                backend_error = exc.payload()
        try:
            events = self.events(principal, session_id, after=0, pull_backend=True)["events"]
        except AgentSessionServiceError as exc:
            if backend_error is None and exc.retryable:
                backend_error = exc.payload()
            events = self._buffered_events(session_id, after=0)
        payload: dict[str, Any] = {
            "session": self._detail_payload(record),
            "events": events,
        }
        if backend_error is not None:
            payload["backend_error"] = backend_error
        return payload

    def send_turn(
        self,
        principal: OperatorPrincipal,
        session_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise OperatorAPIError(
                "operator_invalid_request",
                "message must be non-empty text.",
                status=400,
            )
        record = self._resume_for_action(principal, session_id)
        first_turn = record.last_turn_id is None
        record = self.agent_sessions.send_turn(record.session_id, principal.principal_id, message)
        self._record_transcript_messages(
            record,
            [
                {
                    "message_id": f"{record.session_id}:{record.last_turn_id or 'turn'}:user",
                    "role": "user",
                    "content": message,
                    "source": "operator-app",
                }
            ],
            title=self._message_title(message) if first_turn else None,
        )
        return {"session": self._session_summary_payload(record)}

    def interrupt(
        self,
        principal: OperatorPrincipal,
        session_id: str,
    ) -> dict[str, Any]:
        record = self._resume_for_action(principal, session_id)
        record = self.agent_sessions.interrupt(record.session_id, principal.principal_id)
        return {"session": record.summary_payload()}

    def approve(
        self,
        principal: OperatorPrincipal,
        session_id: str,
        approval_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        decision = body.get("decision")
        mapped = {
            "approve": "accept",
            "accept": "accept",
            "acceptForSession": "acceptForSession",
            "deny": "decline",
            "decline": "decline",
            "cancel": "cancel",
        }.get(decision if isinstance(decision, str) else "")
        if mapped is None:
            raise OperatorAPIError(
                "operator_invalid_request",
                "decision must be approve, deny, accept, acceptForSession, decline, or cancel.",
                status=400,
            )
        record = self._resume_for_action(principal, session_id)
        record = self.agent_sessions.approve(
            record.session_id,
            principal.principal_id,
            self._required_string(approval_id, "approval_id", 256),
            mapped,
        )
        return {"session": record.summary_payload()}

    def events(
        self,
        principal: OperatorPrincipal,
        session_id: str,
        *,
        after: int = 0,
        pull_backend: bool = True,
    ) -> dict[str, Any]:
        session_id = self._required_string(session_id, "session_id", 256)
        record = self.agent_sessions.get_session(session_id, principal.principal_id)
        self._require_workspace(principal, record.workspace_id)
        if pull_backend and record.status != "closed":
            try:
                backend_events = self.agent_sessions.drain_events(
                    session_id,
                    principal.principal_id,
                    limit=MAX_OPERATOR_EVENT_PULL,
                )
            except AgentSessionServiceError as exc:
                if exc.code != "AGENT_SESSION_DETACHED":
                    raise
                self.agent_sessions.resume_session(session_id, principal.principal_id)
                backend_events = self.agent_sessions.drain_events(
                    session_id,
                    principal.principal_id,
                    limit=MAX_OPERATOR_EVENT_PULL,
                )
            self._append_events(record, backend_events)
        events = self._buffered_events(session_id, after=max(0, int(after)))
        cursor = events[-1]["sequence"] if events else max(0, int(after))
        return {"events": events, "cursor": cursor}

    def run_validation(
        self,
        principal: OperatorPrincipal,
        session_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        session_id = self._required_string(session_id, "session_id", 256)
        recipe = self._required_string(body.get("recipe"), "recipe", 128)
        record = self.agent_sessions.get_session(session_id, principal.principal_id)
        workspace = self._require_workspace(principal, record.workspace_id)
        factory = self.validation_backend_factory
        if factory is None:
            raise OperatorAPIError(
                "operator_validation_unavailable",
                "Structured validation is unavailable.",
                status=503,
                retryable=True,
            )
        try:
            backend = factory(workspace)
            try:
                result = backend.run(recipe)
            finally:
                backend.close()
        except OperatorAPIError:
            raise
        except Exception as exc:  # noqa: BLE001 - backend failures stay behind Operator boundary
            raise OperatorAPIError(
                "operator_validation_unavailable",
                "Structured validation backend is unavailable.",
                status=503,
                retryable=True,
            ) from exc
        payload = result.payload()
        with self._event_lock:
            self._validation_results[session_id] = dict(payload)
        return {"validation": payload}

    def handoff(
        self,
        principal: OperatorPrincipal,
        session_id: str,
    ) -> dict[str, Any]:
        session_id = self._required_string(session_id, "session_id", 256)
        record = self.agent_sessions.get_session(session_id, principal.principal_id)
        workspace = self._require_workspace(principal, record.workspace_id)
        try:
            events = self.events(
                principal,
                session_id,
                after=0,
                pull_backend=record.status != "closed",
            )["events"]
        except AgentSessionServiceError as exc:
            if not exc.retryable:
                raise
            events = self._buffered_events(session_id, after=0)
        record = self.agent_sessions.get_session(session_id, principal.principal_id)
        jobs = (
            tuple(self.handoff_jobs(principal, workspace))
            if self.handoff_jobs is not None
            else ()
        )
        with self._event_lock:
            validation = dict(self._validation_results[session_id]) if session_id in self._validation_results else None
        return {
            "handoff": build_session_handoff(
                record,
                workspace,
                events=events,
                validation=validation,
                jobs=jobs,
            )
        }

    def _resume_for_action(
        self,
        principal: OperatorPrincipal,
        session_id: str,
    ) -> AgentSessionRecord:
        session_id = self._required_string(session_id, "session_id", 256)
        record = self.agent_sessions.get_session(session_id, principal.principal_id)
        self._require_workspace(principal, record.workspace_id)
        if record.status != "closed":
            record = self.agent_sessions.resume_session(session_id, principal.principal_id)
        return record

    def _append_events(self, record: AgentSessionRecord, events: list[AgentBackendEvent]) -> None:
        if not events:
            return
        session_id = record.session_id
        transcript_messages: list[dict[str, Any]] = []
        with self._event_lock:
            buffer = self._events.setdefault(session_id, deque(maxlen=MAX_OPERATOR_EVENTS))
            sequence = self._next_sequence.get(session_id, 0)
            for event in events:
                sequence += 1
                buffer.append(
                    {
                        "sequence": sequence,
                        "kind": event.kind,
                        "method": event.method,
                        "params": dict(event.params),
                        "approval_id": event.approval_id,
                    }
                )
                message = self._assistant_transcript_message(record, event)
                if message is not None:
                    transcript_messages.append(message)
            self._next_sequence[session_id] = sequence
        if transcript_messages:
            self._record_transcript_messages(record, transcript_messages)

    def _buffered_events(self, session_id: str, *, after: int) -> list[dict[str, Any]]:
        with self._event_lock:
            buffer = self._events.get(session_id)
            if buffer is None:
                return []
            return [dict(event) for event in buffer if int(event["sequence"]) > after]

    def _require_workspace(
        self,
        principal: OperatorPrincipal,
        workspace_id: str,
    ) -> WorkspaceEntry:
        if not principal.can_access(workspace_id):
            raise OperatorAPIError(
                "operator_workspace_not_found",
                "Workspace is unavailable.",
                status=404,
            )
        try:
            return self.workspace_catalog.get(workspace_id)
        except WorkspaceCatalogError as exc:
            raise OperatorAPIError(
                "operator_workspace_not_found",
                "Workspace is unavailable.",
                status=404,
            ) from exc

    def _ensure_transcript_conversation(self, record: AgentSessionRecord) -> AgentSessionRecord:
        store = self.transcript_store
        if store is None:
            return record
        if not record.conversation_id:
            record = self.agent_sessions.bind_conversation(
                record.session_id,
                record.owner_principal_id,
                f"agent-{record.session_id}",
            )
        try:
            existing = store.conversation_detail(
                record.workspace_id,
                record.conversation_id or "",
                message_page_size=1,
                context_page_size=1,
            )
            if existing is None:
                store.record_messages(
                    record.workspace_id,
                    record.conversation_id or "",
                    [],
                    title=f"Agent Session {record.session_id[:8]}",
                    source="operator-app",
                )
        except (TranscriptStoreError, OSError):
            return record
        return record

    def _conversation_summary_map(self, workspace_id: str) -> dict[str, dict[str, Any]]:
        store = self.transcript_store
        if store is None:
            return {}
        try:
            payload = store.list_conversations(workspace_id, page=1, page_size=200)
        except (TranscriptStoreError, OSError):
            return {}
        return {
            str(item.get("conversation_id")): dict(item)
            for item in payload.get("items", [])
            if isinstance(item, dict) and item.get("conversation_id")
        }

    def _session_summary_payload(
        self,
        record: AgentSessionRecord,
        summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = record.summary_payload()
        if summary is None and record.conversation_id:
            summary = self._conversation_summary_map(record.workspace_id).get(record.conversation_id)
        if summary:
            title = summary.get("title")
            preview = summary.get("preview")
            if isinstance(title, str) and title.strip():
                payload["title"] = title.strip()
            if isinstance(preview, str) and preview.strip():
                payload["summary"] = preview.strip()
            payload["message_count"] = int(summary.get("message_count") or 0)
        return payload

    def _record_transcript_messages(
        self,
        record: AgentSessionRecord,
        messages: list[dict[str, Any]],
        *,
        title: str | None = None,
    ) -> bool:
        store = self.transcript_store
        if store is None or not record.conversation_id:
            return False
        try:
            store.record_messages(
                record.workspace_id,
                record.conversation_id,
                messages,
                title=title,
                source="operator-app",
            )
        except (TranscriptStoreError, OSError):
            return False
        return True

    def _assistant_transcript_message(
        self,
        record: AgentSessionRecord,
        event: AgentBackendEvent,
    ) -> dict[str, Any] | None:
        text = ""
        message_id = ""
        metadata: dict[str, Any] = {"method": event.method}
        if event.method == "item/completed":
            item = event.params.get("item")
            if not isinstance(item, dict) or item.get("type") not in {"agentMessage", "agent_message"}:
                return None
            raw_text = item.get("text")
            if isinstance(raw_text, str):
                text = raw_text.strip()
            raw_id = item.get("id")
            if isinstance(raw_id, str) and raw_id.strip():
                message_id = raw_id.strip()
            phase = item.get("phase")
            if isinstance(phase, str) and phase:
                metadata["phase"] = phase
        elif event.kind == "assistant" or event.method in {"turn/assistant", "assistant/message"}:
            for key in ("text", "message", "content"):
                value = event.params.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
        if not text:
            return None
        if not message_id:
            digest = hashlib.sha256(
                f"{event.method}\0{event.sequence}\0{text}".encode("utf-8")
            ).hexdigest()[:24]
            message_id = f"event-{digest}"
        return {
            "message_id": f"{record.session_id}:{message_id}:assistant",
            "role": "assistant",
            "content": text,
            "source": "operator-app",
            "metadata": metadata,
        }

    @staticmethod
    def _message_title(message: str) -> str:
        normalized = " ".join(message.split())
        return normalized[:120] if normalized else "Agent Session"

    def _workspace_payload(self, entry: WorkspaceEntry) -> dict[str, Any]:
        runner_status = "local"
        if entry.target == "runner":
            runner_status = "unknown"
            if self.workspace_status is not None:
                try:
                    status = self.workspace_status(entry)
                    runner_status = "connected" if bool(status.get("connected")) else "disconnected"
                except Exception:  # noqa: BLE001 - status projection must fail closed
                    runner_status = "unavailable"
        return {
            "id": entry.id,
            "name": entry.name,
            "enabled": True,
            "authorized": True,
            "target": entry.target,
            "runner_status": runner_status,
        }

    def _detail_payload(self, record: AgentSessionRecord) -> dict[str, Any]:
        payload = self._session_summary_payload(record)
        payload.update(
            {
                "explicit_instructions": record.explicit_instructions,
                "instruction_digest": record.instruction_digest,
                "repo_fingerprint": dict(record.repo_fingerprint),
            }
        )
        return payload

    @staticmethod
    def _required_string(value: Any, field: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise OperatorAPIError(
                "operator_invalid_request",
                f"{field} must be a non-empty string up to {limit} characters.",
                status=400,
            )
        return value.strip()


def operator_error_status(exc: AgentSessionServiceError) -> int:
    if exc.code in {"AGENT_SESSION_NOT_FOUND", "AGENT_WORKSPACE_UNAVAILABLE"}:
        return 404
    if exc.code in {"AGENT_SESSION_CLOSED", "AGENT_TURN_NOT_ACTIVE"}:
        return 409
    if exc.code.startswith("AGENT_INPUT") or exc.code.startswith("AGENT_APPROVAL_INVALID"):
        return 400
    if exc.retryable or exc.code in {
        "AGENT_BACKEND_UNAVAILABLE",
        "AGENT_BACKEND_FACTORY_FAILED",
        "AGENT_SESSION_DETACHED",
    }:
        return 503
    return 400


__all__ = [
    "MAX_OPERATOR_EVENTS",
    "OPERATOR_API_PREFIX",
    "OperatorAPIError",
    "OperatorAPIService",
    "OperatorPrincipal",
    "operator_error_status",
]
