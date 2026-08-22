from __future__ import annotations

import json
import tempfile
import unittest
from collections import deque
from pathlib import Path

from coding_tools_mcp.agent_backends.base import (
    AgentBackendEvent,
    BackendHealth,
    BackendThread,
    BackendTurn,
)
from coding_tools_mcp.agent_session_store import AgentSessionStore
from coding_tools_mcp.agent_sessions import AgentSessionService, AgentSessionServiceError
from coding_tools_mcp.operator_api import OperatorAPIError, OperatorAPIService, OperatorPrincipal
from coding_tools_mcp.transcript import TranscriptStore
from coding_tools_mcp.validation import ValidationResult
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


class FakeAgentBackend:
    backend_kind = "codex-app-server"

    def __init__(self) -> None:
        self.thread_id = "thread-1"
        self.closed = False
        self.events: deque[AgentBackendEvent] = deque()
        self.approvals: list[tuple[str, str]] = []
        self.interrupts: list[tuple[str, str]] = []

    def health(self) -> BackendHealth:
        return BackendHealth(not self.closed, self.backend_kind)

    def create_thread(self, *, instructions: str | None = None) -> BackendThread:
        return BackendThread(self.thread_id, {"instructions": instructions})

    def resume_thread(
        self,
        thread_id: str,
        *,
        instructions: str | None = None,
    ) -> BackendThread:
        self.thread_id = thread_id
        return BackendThread(thread_id, {"instructions": instructions})

    def send_turn(self, thread_id: str, message: str) -> BackendTurn:
        self.events.append(
            AgentBackendEvent(
                sequence=len(self.events) + 1,
                kind="assistant",
                method="turn/assistant",
                params={"text": message},
            )
        )
        return BackendTurn("turn-1", {"thread_id": thread_id})

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))

    def approve(self, approval_id: str, decision: str) -> None:
        self.approvals.append((approval_id, decision))

    def list_threads(self, *, limit: int = 50) -> list[BackendThread]:
        return [BackendThread(self.thread_id, {})][:limit]

    def close_thread(self, thread_id: str) -> None:
        del thread_id

    def stream_events(self, *, timeout: float | None = None):
        del timeout
        while self.events:
            yield self.events.popleft()

    def drain_events(self, *, limit: int = 100) -> list[AgentBackendEvent]:
        result: list[AgentBackendEvent] = []
        while self.events and len(result) < limit:
            result.append(self.events.popleft())
        return result

    def close(self) -> None:
        self.closed = True


class FakeValidationBackend:
    def __init__(self) -> None:
        self.closed = False

    def status(self) -> dict[str, object]:
        return {"ok": True, "backend": "fake", "status": "ready", "recipes": ["python:test"]}

    def run(self, recipe: str) -> ValidationResult:
        return ValidationResult(
            "passed",
            recipe,
            command="python -m unittest",
            exit_code=0,
            duration_ms=7,
        )

    def close(self) -> None:
        self.closed = True


class OperatorAPIServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        first = root / "first"
        second = root / "second"
        first.mkdir()
        second.mkdir()
        self.catalog = WorkspaceCatalog(
            [
                WorkspaceEntry("ws-a", "Alpha", first, True, True),
                WorkspaceEntry("ws-b", "Beta", second, True, False),
            ],
            "ws-a",
        )
        self.backends: list[FakeAgentBackend] = []

        def backend_factory(_workspace: WorkspaceEntry, backend_kind: str) -> FakeAgentBackend:
            self.assertEqual(backend_kind, "codex-app-server")
            backend = FakeAgentBackend()
            self.backends.append(backend)
            return backend

        store = AgentSessionStore(root / "agent-sessions.sqlite3")
        self.transcripts = TranscriptStore(root / "transcripts.sqlite3")
        sessions = AgentSessionService(store, self.catalog, backend_factory)
        self.handoff_jobs: list[dict[str, object]] = []
        self.validation_backends: list[FakeValidationBackend] = []

        def validation_factory(_workspace: WorkspaceEntry) -> FakeValidationBackend:
            backend = FakeValidationBackend()
            self.validation_backends.append(backend)
            return backend

        self.service = OperatorAPIService(
            sessions,
            self.catalog,
            handoff_jobs=lambda principal, workspace: (
                tuple(self.handoff_jobs)
                if principal.principal_id == "oauth:alice:grant-1" and workspace.id == "ws-a"
                else ()
            ),
            validation_backend_factory=validation_factory,
            transcript_store=self.transcripts,
        )
        self.alice = OperatorPrincipal("oauth:alice:grant-1", ("ws-a",))
        self.bob = OperatorPrincipal("oauth:bob:grant-2", ("ws-b",))

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_workspace_projection_is_authorized_and_does_not_leak_roots(self) -> None:
        payload = self.service.list_workspaces(self.alice)
        self.assertEqual([item["id"] for item in payload["workspaces"]], ["ws-a"])
        self.assertNotIn("root", payload["workspaces"][0])
        self.assertTrue(payload["workspaces"][0]["authorized"])
        with self.assertRaises(OperatorAPIError) as denied:
            self.service.list_sessions(self.alice, "ws-b")
        self.assertEqual(denied.exception.status, 404)

    def test_create_list_get_and_cross_principal_access_are_partitioned(self) -> None:
        created = self.service.create_session(
            self.alice,
            {
                "workspace_id": "ws-a",
                "backend_kind": "codex",
                "instructions": "Keep changes focused.",
            },
        )["session"]
        self.assertEqual(created["backend_kind"], "codex-app-server")
        self.assertEqual(created["explicit_instructions"], "Keep changes focused.")
        self.assertNotIn("backend_thread_id", created)
        listed = self.service.list_sessions(self.alice, "ws-a")["sessions"]
        self.assertEqual([item["session_id"] for item in listed], [created["session_id"]])
        detail = self.service.get_session(self.alice, created["session_id"])["session"]
        self.assertEqual(detail["session_id"], created["session_id"])
        with self.assertRaises(AgentSessionServiceError) as denied:
            self.service.get_session(self.bob, created["session_id"])
        self.assertEqual(denied.exception.code, "AGENT_SESSION_NOT_FOUND")

    def test_event_buffer_supports_multiple_windows_after_backend_drain(self) -> None:
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        session_id = created["session_id"]
        self.service.send_turn(self.alice, session_id, {"message": "hello"})

        first_window = self.service.events(self.alice, session_id, after=0)
        self.assertEqual(len(first_window["events"]), 1)
        self.assertEqual(first_window["events"][0]["sequence"], 1)
        self.assertEqual(first_window["events"][0]["params"]["text"], "hello")

        second_window = self.service.events(self.alice, session_id, after=0)
        self.assertEqual(second_window["events"], first_window["events"])
        caught_up = self.service.events(self.alice, session_id, after=1)
        self.assertEqual(caught_up["events"], [])

        detail = self.transcripts.conversation_detail("ws-a", created["conversation_id"])
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual([item["role"] for item in detail["messages"]], ["user", "assistant"])
        self.assertEqual([item["content"] for item in detail["messages"]], ["hello", "hello"])
        listed = self.service.list_sessions(self.alice, "ws-a")["sessions"]
        self.assertEqual(listed[0]["title"], "hello")
        self.assertEqual(listed[0]["summary"], "hello")

    def test_operator_decisions_map_to_codex_approval_values(self) -> None:
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        session_id = created["session_id"]
        self.service.approve(self.alice, session_id, "approval-1", {"decision": "approve"})
        self.service.approve(self.alice, session_id, "approval-2", {"decision": "deny"})
        self.assertEqual(
            self.backends[0].approvals,
            [("approval-1", "accept"), ("approval-2", "decline")],
        )

    def test_validation_result_is_bounded_runtime_evidence_in_handoff(self) -> None:
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        session_id = created["session_id"]
        validation = self.service.run_validation(
            self.alice,
            session_id,
            {"recipe": "python:test"},
        )["validation"]
        self.assertEqual(validation["status"], "passed")
        self.assertEqual(validation["recipe"], "python:test")
        self.assertEqual(validation["exit_code"], 0)
        self.assertTrue(self.validation_backends[-1].closed)
        handoff = self.service.handoff(self.alice, session_id)["handoff"]
        self.assertEqual(
            handoff["validation"],
            {"status": "passed", "recipe": "python:test", "exit_code": 0},
        )
        with self.assertRaises(AgentSessionServiceError):
            self.service.run_validation(self.bob, session_id, {"recipe": "python:test"})

    def test_resume_persists_repo_context_changes_across_windows(self) -> None:
        first = {
            "version": 1,
            "git_available": True,
            "branch": "main",
            "head": "aaa",
            "worktree_digest": "clean",
            "instruction_digest": "instructions-a",
            "changed_paths": [],
            "context_changed": False,
            "changes": [],
        }
        second = {
            **first,
            "branch": "feature/continuity",
            "head": "bbb",
            "instruction_digest": "instructions-b",
        }
        self.service.agent_sessions.fingerprint_factory = lambda _workspace: dict(first)
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        self.assertFalse(created["repo_fingerprint"]["context_changed"])

        self.service.agent_sessions.fingerprint_factory = lambda _workspace: dict(second)
        changed = self.service.get_session(self.alice, created["session_id"])["session"]
        self.assertTrue(changed["repo_fingerprint"]["context_changed"])
        self.assertEqual(
            changed["repo_fingerprint"]["changes"],
            ["Branch changed", "HEAD changed", "Project instructions changed"],
        )

        reopened = self.service.get_session(self.alice, created["session_id"])["session"]
        self.assertTrue(reopened["repo_fingerprint"]["context_changed"])
        self.assertEqual(reopened["repo_fingerprint"]["head"], "bbb")

    def test_legacy_fingerprint_without_branch_does_not_report_upgrade_drift(self) -> None:
        previous = {
            "version": 1,
            "git_available": True,
            "head": "aaa",
            "worktree_digest": "clean",
            "instruction_digest": "instructions-a",
            "changed_paths": [],
            "context_changed": False,
            "changes": [],
        }
        current = {**previous, "branch": "main"}
        self.service.agent_sessions.fingerprint_factory = lambda _workspace: dict(previous)
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        self.service.agent_sessions.fingerprint_factory = lambda _workspace: dict(current)
        resumed = self.service.get_session(self.alice, created["session_id"])["session"]
        self.assertFalse(resumed["repo_fingerprint"]["context_changed"])
        self.assertEqual(resumed["repo_fingerprint"]["changes"], [])

    def test_handoff_is_deterministic_bounded_and_contains_no_workspace_root_or_backend_thread(self) -> None:
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        session_id = created["session_id"]
        record = self.service.agent_sessions.get_session(session_id, self.alice.principal_id)
        self.service.agent_sessions.store.update_state(
            record.session_id,
            record.workspace_id,
            record.owner_principal_id,
            repo_fingerprint={
                "version": 1,
                "git_available": True,
                "branch": "feature/handoff",
                "head": "abc123",
                "worktree_digest": "digest",
                "instruction_digest": "instructions",
                "changed_paths": ["src/main.py"],
                "context_changed": True,
                "changes": ["HEAD changed"],
            },
        )
        self.backends[0].events.append(
            AgentBackendEvent(
                sequence=1,
                kind="approval",
                method="approval/requested",
                params={"reason": "write file"},
                approval_id="approval-1",
            )
        )
        self.handoff_jobs[:] = [
            {"job_id": "job-running", "status": "running"},
            {"job_id": "job-recovering", "status": "recovering"},
            {"job_id": "job-complete", "status": "completed"},
        ]

        first = self.service.handoff(self.alice, session_id)["handoff"]
        second = self.service.handoff(self.alice, session_id)["handoff"]
        self.assertEqual(first, second)
        self.assertEqual(first["workspace"]["id"], "ws-a")
        self.assertEqual(first["repository"]["branch"], "feature/handoff")
        self.assertEqual(first["repository"]["head"], "abc123")
        self.assertEqual(first["repository"]["changed_paths"], ["src/main.py"])
        self.assertEqual(
            first["active_jobs"],
            [
                {"job_id": "job-running", "status": "running"},
                {"job_id": "job-recovering", "status": "recovering"},
            ],
        )
        self.assertEqual(first["unresolved_approval"]["approval_id"], "approval-1")
        self.assertIn("resolve_approval", first["next_actions"])
        self.assertIn("review_context_changes", first["next_actions"])
        self.assertIn("wait_for_job_recovery", first["next_actions"])
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn(str(self.catalog.get("ws-a").root), serialized)
        self.assertNotIn("thread-1", serialized)


if __name__ == "__main__":
    unittest.main()
