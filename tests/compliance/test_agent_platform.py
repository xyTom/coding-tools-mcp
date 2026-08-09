from __future__ import annotations

import sqlite3
import sys
import time
import unittest
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.agent_backends.base import (
    AgentBackendError,
    AgentBackendEvent,
    BackendHealth,
    BackendThread,
    BackendTurn,
)
from coding_tools_mcp.agent_backends.codex_app_server import (
    CodexAppServerBackend,
    CodexAppServerConfig,
)
from coding_tools_mcp.agent_session_store import AgentSessionStore, AgentSessionStoreError
from coding_tools_mcp.agent_sessions import AgentSessionService, AgentSessionServiceError
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


class FakeBackend:
    def __init__(self, backend_kind: str, *, fail_create: bool = False) -> None:
        self._backend_kind = backend_kind
        self.fail_create = fail_create
        self.closed = False
        self.resumed: list[str] = []
        self.approvals: list[tuple[str, str]] = []
        self.interruptions: list[tuple[str, str]] = []
        self._events: list[AgentBackendEvent] = []
        self._sequence = 0

    @property
    def backend_kind(self) -> str:
        return self._backend_kind

    def health(self) -> BackendHealth:
        return BackendHealth(not self.closed, self.backend_kind)

    def create_thread(self, *, instructions: str | None = None) -> BackendThread:
        if self.fail_create:
            raise AgentBackendError(
                "AGENT_BACKEND_UNAVAILABLE",
                "fake backend unavailable",
                retryable=True,
            )
        return BackendThread("thread-1", {"instructions": instructions})

    def resume_thread(self, thread_id: str, *, instructions: str | None = None) -> BackendThread:
        self.resumed.append(thread_id)
        return BackendThread(thread_id, {"instructions": instructions})

    def send_turn(self, thread_id: str, message: str) -> BackendTurn:
        self._sequence += 1
        self._events.append(
            AgentBackendEvent(
                self._sequence,
                "approval",
                "item/commandExecution/requestApproval",
                {"threadId": thread_id, "command": "echo safe"},
                "approval-1",
            )
        )
        return BackendTurn("turn-1", {"message": message})

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interruptions.append((thread_id, turn_id))

    def approve(self, approval_id: str, decision: str) -> None:
        self.approvals.append((approval_id, decision))
        self._sequence += 1
        self._events.append(
            AgentBackendEvent(
                self._sequence,
                "notification",
                "turn/completed",
                {"turn": {"id": "turn-1", "status": "completed"}},
            )
        )

    def list_threads(self, *, limit: int = 50) -> list[BackendThread]:
        return [BackendThread("thread-1", {})][:limit]

    def close_thread(self, thread_id: str) -> None:
        return None

    def stream_events(self, *, timeout: float | None = None) -> Iterator[AgentBackendEvent]:
        yield from self.drain_events(limit=100)

    def drain_events(self, *, limit: int = 100) -> list[AgentBackendEvent]:
        result = self._events[:limit]
        del self._events[:limit]
        return result

    def close(self) -> None:
        self.closed = True


class FakeBackendFactory:
    def __init__(self) -> None:
        self.backends: list[FakeBackend] = []

    def __call__(self, workspace: WorkspaceEntry, backend_kind: str) -> FakeBackend:
        backend = FakeBackend(backend_kind, fail_create=backend_kind == "unavailable")
        self.backends.append(backend)
        return backend


class AgentSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reopen_close_idempotency_and_corruption_detection(self) -> None:
        path = self.root / "agent-sessions.sqlite3"
        store = AgentSessionStore(path)
        created = store.create(
            workspace_id="workspace-a",
            owner_principal_id="alice",
            backend_kind="fake",
            repo_fingerprint={"head": "abc"},
        )
        reopened = AgentSessionStore(path)
        self.assertEqual(
            reopened.get(created.session_id, "workspace-a", "alice").repo_fingerprint,
            {"head": "abc"},
        )
        first_close = reopened.close(created.session_id, "workspace-a", "alice")
        second_close = reopened.close(created.session_id, "workspace-a", "alice")
        self.assertEqual(first_close.status, "closed")
        self.assertEqual(second_close.status, "closed")

        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE agent_sessions SET repo_fingerprint_json='[]' WHERE session_id=?",
                (created.session_id,),
            )
        with self.assertRaisesRegex(AgentSessionStoreError, "JSON object"):
            reopened.get(created.session_id, "workspace-a", "alice")

    def test_newer_schema_version_fails_closed(self) -> None:
        path = self.root / "newer.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(AgentSessionStoreError, "newer version"):
            AgentSessionStore(path)


class AgentSessionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace_a = self.root / "workspace-a"
        self.workspace_b = self.root / "workspace-b"
        self.workspace_a.mkdir()
        self.workspace_b.mkdir()
        self.catalog = WorkspaceCatalog(
            [
                WorkspaceEntry("a", "A", self.workspace_a, True, True),
                WorkspaceEntry("b", "B", self.workspace_b, True, False),
            ],
            "a",
        )
        self.store = AgentSessionStore(self.root / "agent-sessions.sqlite3")
        self.factory = FakeBackendFactory()
        self.service = AgentSessionService(self.store, self.catalog, self.factory)

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def test_create_turn_approval_and_cross_principal_isolation(self) -> None:
        record = self.service.create_session(
            workspace_id="a",
            owner_principal_id="alice",
            backend_kind="fake",
            instructions="Keep changes focused.",
            conversation_id="conversation-1",
            repo_fingerprint={"head": "abc"},
        )
        self.assertEqual(record.status, "ready")
        self.assertEqual(record.backend_thread_id, "thread-1")
        self.assertNotIn("backend_thread_id", record.summary_payload())
        self.assertEqual(record.explicit_instructions, "Keep changes focused.")

        running = self.service.send_turn(record.session_id, "alice", "change one thing")
        self.assertEqual(running.status, "running")
        self.assertEqual(running.last_turn_id, "turn-1")

        events = self.service.drain_events(record.session_id, "alice")
        self.assertEqual(events[0].approval_id, "approval-1")
        self.assertEqual(self.store.get(record.session_id, "a", "alice").status, "waiting_approval")
        approved = self.service.approve(record.session_id, "alice", "approval-1", "accept")
        self.assertEqual(approved.status, "running")
        self.service.drain_events(record.session_id, "alice")
        self.assertEqual(self.store.get(record.session_id, "a", "alice").status, "ready")

        with self.assertRaises(AgentSessionServiceError) as caught:
            self.service.send_turn(record.session_id, "bob", "not authorized")
        self.assertEqual(caught.exception.code, "AGENT_SESSION_NOT_FOUND")
        self.assertEqual(self.service.list_sessions("a", "bob"), [])

    def test_service_rebuild_resumes_durable_backend_thread(self) -> None:
        record = self.service.create_session(
            workspace_id="a",
            owner_principal_id="alice",
            backend_kind="fake",
        )
        self.service.close()

        second_factory = FakeBackendFactory()
        second_service = AgentSessionService(self.store, self.catalog, second_factory)
        try:
            resumed = second_service.resume_session(record.session_id, "alice")
            self.assertEqual(resumed.status, "ready")
            self.assertEqual(second_factory.backends[0].resumed, ["thread-1"])
            closed = second_service.close_session(record.session_id, "alice")
            self.assertEqual(closed.status, "closed")
            with self.assertRaises(AgentSessionServiceError) as caught:
                second_service.resume_session(record.session_id, "alice")
            self.assertEqual(caught.exception.code, "AGENT_SESSION_CLOSED")
        finally:
            second_service.close()

    def test_backend_unavailable_is_durable_and_retryable(self) -> None:
        with self.assertRaises(AgentSessionServiceError) as caught:
            self.service.create_session(
                workspace_id="a",
                owner_principal_id="alice",
                backend_kind="unavailable",
            )
        self.assertEqual(caught.exception.code, "AGENT_BACKEND_UNAVAILABLE")
        self.assertTrue(caught.exception.retryable)
        records = self.store.list("a", "alice")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "unavailable")


class CodexAppServerBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fake_codex_app_server.py"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _wait_for_method(
        backend: CodexAppServerBackend,
        method: str,
        *,
        timeout: float = 2.0,
    ) -> list[AgentBackendEvent]:
        deadline = time.monotonic() + timeout
        events: list[AgentBackendEvent] = []
        while time.monotonic() < deadline:
            events.extend(backend.drain_events(limit=50))
            if any(event.method == method for event in events):
                return events
            time.sleep(0.01)
        return events

    def test_jsonl_thread_turn_approval_and_interrupt_round_trip(self) -> None:
        config = CodexAppServerConfig.create(
            self.workspace,
            command=(sys.executable, str(self.fixture)),
            request_timeout_seconds=2.0,
        )
        backend = CodexAppServerBackend(config)
        try:
            health = backend.health()
            self.assertTrue(health.available)
            self.assertEqual(health.version, "test")
            thread = backend.create_thread(instructions="Use the fake backend.")
            self.assertEqual(thread.thread_id, "thread-1")
            resumed = backend.resume_thread(thread.thread_id)
            self.assertEqual(resumed.thread_id, "thread-1")

            turn = backend.send_turn(thread.thread_id, "hello")
            self.assertEqual(turn.turn_id, "turn-1")
            approval_events = self._wait_for_method(
                backend,
                "item/commandExecution/requestApproval",
            )
            approval = next(
                event
                for event in approval_events
                if event.method == "item/commandExecution/requestApproval"
            )
            self.assertEqual(approval.kind, "approval")
            self.assertEqual(approval.approval_id, "approval-1")

            with self.assertRaises(AgentBackendError):
                backend.approve("approval-1", "maybe")
            backend.approve("approval-1", "accept")
            completed = self._wait_for_method(backend, "turn/completed")
            self.assertTrue(any(event.method == "turn/completed" for event in completed))

            backend.interrupt_turn(thread.thread_id, turn.turn_id)
            self.assertEqual(backend.list_threads()[0].thread_id, "thread-1")
            backend.close_thread(thread.thread_id)
        finally:
            backend.close()


if __name__ == "__main__":
    unittest.main()
