"""Application service for durable Agent Sessions and live backend lifecycles."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from typing import Any

from .agent_backends.base import AgentBackendError, AgentBackendEvent, AgentSessionBackend
from .agent_session_store import (
    AgentSessionNotFoundError,
    AgentSessionRecord,
    AgentSessionStore,
    AgentSessionStoreError,
)
from .repo_fingerprint import build_repo_fingerprint, fingerprint_changes, with_context_changes
from .workspace_catalog import WorkspaceCatalog, WorkspaceCatalogError, WorkspaceEntry


BackendFactory = Callable[[WorkspaceEntry, str], AgentSessionBackend]
FingerprintFactory = Callable[[WorkspaceEntry], dict[str, Any]]


class AgentSessionServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "details": dict(self.details),
        }


class AgentSessionService:
    """Coordinates durable metadata with disposable live backend processes.

    Backend instances are intentionally in-memory attachments.  Reconstructing
    this service and calling :meth:`resume_session` recreates the backend and
    resumes by the durable backend thread id from :class:`AgentSessionStore`.
    """

    def __init__(
        self,
        store: AgentSessionStore,
        workspace_catalog: WorkspaceCatalog,
        backend_factory: BackendFactory,
        fingerprint_factory: FingerprintFactory | None = None,
    ) -> None:
        self.store = store
        self.workspace_catalog = workspace_catalog
        self.backend_factory = backend_factory
        self.fingerprint_factory = fingerprint_factory or self._default_fingerprint
        self._backends: dict[str, AgentSessionBackend] = {}
        self._lock = threading.RLock()

    def create_session(
        self,
        *,
        workspace_id: str,
        owner_principal_id: str,
        backend_kind: str,
        instructions: str | None = None,
        conversation_id: str | None = None,
        repo_fingerprint: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        workspace = self._workspace(workspace_id)
        digest = self._instruction_digest(instructions)
        if repo_fingerprint is None:
            repo_fingerprint = self._fingerprint(workspace)
        try:
            record = self.store.create(
                workspace_id=workspace.id,
                owner_principal_id=owner_principal_id,
                backend_kind=backend_kind,
                conversation_id=conversation_id,
                status="creating",
                explicit_instructions=instructions,
                instruction_digest=digest,
                repo_fingerprint=repo_fingerprint,
            )
        except AgentSessionStoreError as exc:
            raise self._store_error(exc) from exc

        backend: AgentSessionBackend | None = None
        try:
            backend = self.backend_factory(workspace, backend_kind)
            if backend.backend_kind != backend_kind:
                raise AgentSessionServiceError(
                    "AGENT_BACKEND_KIND_MISMATCH",
                    "Agent backend factory returned an unexpected backend kind.",
                )
            thread = backend.create_thread(
                instructions=record.explicit_instructions or None,
            )
            ready = self.store.update_state(
                record.session_id,
                record.workspace_id,
                record.owner_principal_id,
                status="ready",
                backend_thread_id=thread.thread_id,
            )
            with self._lock:
                self._backends[record.session_id] = backend
            return ready
        except AgentBackendError as exc:
            if backend is not None:
                backend.close()
            self._mark_backend_failure(record, exc)
            raise self._backend_error(exc) from exc
        except AgentSessionServiceError:
            if backend is not None:
                backend.close()
            self._mark_status(record, "failed")
            raise
        except Exception as exc:  # noqa: BLE001
            if backend is not None:
                backend.close()
            self._mark_status(record, "failed")
            raise AgentSessionServiceError(
                "AGENT_BACKEND_FACTORY_FAILED",
                "Agent backend could not be created.",
                details={"reason": str(exc)[:300]},
            ) from exc

    def resume_session(
        self,
        session_id: str,
        owner_principal_id: str,
    ) -> AgentSessionRecord:
        record = self._record_for_owner(session_id, owner_principal_id)
        if record.status == "closed":
            raise AgentSessionServiceError(
                "AGENT_SESSION_CLOSED",
                "Agent Session is closed.",
            )
        workspace = self._workspace(record.workspace_id)
        record = self._refresh_repo_fingerprint(record, workspace)
        if record.backend_thread_id is None:
            raise AgentSessionServiceError(
                "AGENT_SESSION_NOT_READY",
                "Agent Session does not have a durable backend thread.",
            )
        with self._lock:
            existing = self._backends.get(record.session_id)
        if existing is not None:
            health = existing.health()
            if health.available:
                return record
            self._detach_backend(record.session_id, existing)

        backend: AgentSessionBackend | None = None
        try:
            backend = self.backend_factory(workspace, record.backend_kind)
            if backend.backend_kind != record.backend_kind:
                raise AgentSessionServiceError(
                    "AGENT_BACKEND_KIND_MISMATCH",
                    "Agent backend factory returned an unexpected backend kind.",
                )
            backend.resume_thread(
                record.backend_thread_id,
                instructions=record.explicit_instructions or None,
            )
            resumed = self.store.update_state(
                record.session_id,
                record.workspace_id,
                record.owner_principal_id,
                status="ready",
            )
            with self._lock:
                self._backends[record.session_id] = backend
            return resumed
        except AgentBackendError as exc:
            if backend is not None:
                backend.close()
            self._mark_backend_failure(record, exc)
            raise self._backend_error(exc) from exc
        except AgentSessionServiceError:
            if backend is not None:
                backend.close()
            raise
        except Exception as exc:  # noqa: BLE001
            if backend is not None:
                backend.close()
            self._mark_status(record, "unavailable")
            raise AgentSessionServiceError(
                "AGENT_BACKEND_FACTORY_FAILED",
                "Agent backend could not be created.",
                retryable=True,
                details={"reason": str(exc)[:300]},
            ) from exc

    def send_turn(
        self,
        session_id: str,
        owner_principal_id: str,
        message: str,
    ) -> AgentSessionRecord:
        record, backend = self._attached(session_id, owner_principal_id)
        if record.backend_thread_id is None:
            raise AgentSessionServiceError(
                "AGENT_SESSION_NOT_READY",
                "Agent Session does not have a backend thread.",
            )
        try:
            turn = backend.send_turn(record.backend_thread_id, message)
            return self.store.update_state(
                record.session_id,
                record.workspace_id,
                record.owner_principal_id,
                status="running",
                last_turn_id=turn.turn_id,
            )
        except AgentBackendError as exc:
            self._mark_backend_failure(record, exc)
            raise self._backend_error(exc) from exc

    def interrupt(
        self,
        session_id: str,
        owner_principal_id: str,
    ) -> AgentSessionRecord:
        record, backend = self._attached(session_id, owner_principal_id)
        if record.backend_thread_id is None or record.last_turn_id is None:
            raise AgentSessionServiceError(
                "AGENT_TURN_NOT_ACTIVE",
                "Agent Session does not have a turn to interrupt.",
            )
        try:
            backend.interrupt_turn(record.backend_thread_id, record.last_turn_id)
            return self.store.update_state(
                record.session_id,
                record.workspace_id,
                record.owner_principal_id,
                status="ready",
            )
        except AgentBackendError as exc:
            self._mark_backend_failure(record, exc)
            raise self._backend_error(exc) from exc

    def approve(
        self,
        session_id: str,
        owner_principal_id: str,
        approval_id: str,
        decision: str,
    ) -> AgentSessionRecord:
        record, backend = self._attached(session_id, owner_principal_id)
        try:
            backend.approve(approval_id, decision)
            return self.store.update_state(
                record.session_id,
                record.workspace_id,
                record.owner_principal_id,
                status="running",
            )
        except AgentBackendError as exc:
            self._mark_backend_failure(record, exc)
            raise self._backend_error(exc) from exc

    def drain_events(
        self,
        session_id: str,
        owner_principal_id: str,
        *,
        limit: int = 100,
    ) -> list[AgentBackendEvent]:
        record, backend = self._attached(session_id, owner_principal_id)
        try:
            events = backend.drain_events(limit=limit)
        except AgentBackendError as exc:
            self._mark_backend_failure(record, exc)
            raise self._backend_error(exc) from exc
        self._project_event_status(record, events)
        return events

    def list_sessions(
        self,
        workspace_id: str,
        owner_principal_id: str,
        *,
        limit: int = 100,
    ) -> list[AgentSessionRecord]:
        workspace = self._workspace(workspace_id)
        try:
            return self.store.list(workspace.id, owner_principal_id, limit=limit)
        except AgentSessionStoreError as exc:
            raise self._store_error(exc) from exc

    def get_session(
        self,
        session_id: str,
        owner_principal_id: str,
    ) -> AgentSessionRecord:
        record = self._record_for_owner(session_id, owner_principal_id)
        self._workspace(record.workspace_id)
        return record

    def bind_conversation(
        self,
        session_id: str,
        owner_principal_id: str,
        conversation_id: str,
    ) -> AgentSessionRecord:
        record = self._record_for_owner(session_id, owner_principal_id)
        self._workspace(record.workspace_id)
        return self.store.update_state(
            record.session_id,
            record.workspace_id,
            record.owner_principal_id,
            conversation_id=conversation_id,
        )

    def close_session(
        self,
        session_id: str,
        owner_principal_id: str,
    ) -> AgentSessionRecord:
        record = self._record_for_owner(session_id, owner_principal_id)
        with self._lock:
            backend = self._backends.pop(record.session_id, None)
        if backend is not None:
            try:
                if record.backend_thread_id is not None:
                    backend.close_thread(record.backend_thread_id)
            except AgentBackendError:
                pass
            finally:
                backend.close()
        try:
            return self.store.close(
                record.session_id,
                record.workspace_id,
                record.owner_principal_id,
            )
        except AgentSessionStoreError as exc:
            raise self._store_error(exc) from exc

    def close(self) -> None:
        with self._lock:
            backends = list(self._backends.values())
            self._backends.clear()
        for backend in backends:
            backend.close()

    def _fingerprint(self, workspace: WorkspaceEntry) -> dict[str, Any]:
        try:
            value = self.fingerprint_factory(workspace)
        except Exception as exc:  # noqa: BLE001 - continuity metadata must fail soft
            return {
                "version": 1,
                "git_available": False,
                "context_changed": False,
                "changes": [],
                "error": str(exc)[:200],
            }
        if not isinstance(value, dict):
            return {
                "version": 1,
                "git_available": False,
                "context_changed": False,
                "changes": [],
                "error": "fingerprint resolver returned a non-object value",
            }
        return dict(value)

    @staticmethod
    def _default_fingerprint(workspace: WorkspaceEntry) -> dict[str, Any]:
        if workspace.target != "local":
            return {
                "version": 1,
                "git_available": False,
                "head": None,
                "worktree_digest": None,
                "instruction_digest": None,
                "changed_paths": [],
                "context_changed": False,
                "changes": [],
                "remote": True,
            }
        return build_repo_fingerprint(workspace.root)

    def _refresh_repo_fingerprint(
        self,
        record: AgentSessionRecord,
        workspace: WorkspaceEntry,
    ) -> AgentSessionRecord:
        current = self._fingerprint(workspace)
        if not record.repo_fingerprint:
            try:
                return self.store.update_state(
                    record.session_id,
                    record.workspace_id,
                    record.owner_principal_id,
                    repo_fingerprint=with_context_changes(current, ()),
                )
            except AgentSessionStoreError as exc:
                raise self._store_error(exc) from exc
        changes = fingerprint_changes(record.repo_fingerprint, current)
        if not changes:
            return record
        try:
            return self.store.update_state(
                record.session_id,
                record.workspace_id,
                record.owner_principal_id,
                repo_fingerprint=with_context_changes(current, changes),
            )
        except AgentSessionStoreError as exc:
            raise self._store_error(exc) from exc

    def _workspace(self, workspace_id: str) -> WorkspaceEntry:
        try:
            return self.workspace_catalog.get(workspace_id)
        except WorkspaceCatalogError as exc:
            raise AgentSessionServiceError(
                "AGENT_WORKSPACE_UNAVAILABLE",
                "Workspace is unknown or disabled.",
            ) from exc

    def _record_for_owner(
        self,
        session_id: str,
        owner_principal_id: str,
    ) -> AgentSessionRecord:
        try:
            return self.store.get_for_owner(session_id, owner_principal_id)
        except AgentSessionNotFoundError as exc:
            raise AgentSessionServiceError(
                "AGENT_SESSION_NOT_FOUND",
                "Agent Session was not found.",
            ) from exc
        except AgentSessionStoreError as exc:
            raise self._store_error(exc) from exc

    def _attached(
        self,
        session_id: str,
        owner_principal_id: str,
    ) -> tuple[AgentSessionRecord, AgentSessionBackend]:
        record = self._record_for_owner(session_id, owner_principal_id)
        if record.status == "closed":
            raise AgentSessionServiceError(
                "AGENT_SESSION_CLOSED",
                "Agent Session is closed.",
            )
        self._workspace(record.workspace_id)
        with self._lock:
            backend = self._backends.get(record.session_id)
        if backend is None:
            raise AgentSessionServiceError(
                "AGENT_SESSION_DETACHED",
                "Agent Session is not attached to a live backend; resume it first.",
                retryable=True,
            )
        return record, backend

    def _detach_backend(self, session_id: str, backend: AgentSessionBackend) -> None:
        with self._lock:
            if self._backends.get(session_id) is backend:
                self._backends.pop(session_id, None)
        backend.close()

    def _mark_backend_failure(
        self,
        record: AgentSessionRecord,
        error: AgentBackendError,
    ) -> None:
        self._mark_status(record, "unavailable" if error.retryable else "failed")

    def _mark_status(self, record: AgentSessionRecord, status: str) -> None:
        try:
            self.store.update_state(
                record.session_id,
                record.workspace_id,
                record.owner_principal_id,
                status=status,
            )
        except AgentSessionStoreError:
            pass

    def _project_event_status(
        self,
        record: AgentSessionRecord,
        events: list[AgentBackendEvent],
    ) -> None:
        status: str | None = None
        for event in events:
            if event.kind == "approval":
                status = "waiting_approval"
            elif event.method == "turn/started":
                status = "running"
            elif event.method == "turn/completed":
                status = "ready"
        if status is not None:
            self._mark_status(record, status)

    @staticmethod
    def _instruction_digest(instructions: str | None) -> str | None:
        if not instructions:
            return None
        return hashlib.sha256(instructions.encode("utf-8")).hexdigest()

    @staticmethod
    def _backend_error(error: AgentBackendError) -> AgentSessionServiceError:
        return AgentSessionServiceError(
            error.code,
            str(error),
            retryable=error.retryable,
            details=error.details,
        )

    @staticmethod
    def _store_error(error: AgentSessionStoreError) -> AgentSessionServiceError:
        if isinstance(error, AgentSessionNotFoundError):
            return AgentSessionServiceError(
                "AGENT_SESSION_NOT_FOUND",
                "Agent Session was not found.",
            )
        return AgentSessionServiceError(
            "AGENT_SESSION_STORE_ERROR",
            str(error),
        )


__all__ = [
    "AgentSessionService",
    "AgentSessionServiceError",
    "BackendFactory",
]
