from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_tools_mcp.agent_backends.base import BackendHealth, BackendThread, BackendTurn
from coding_tools_mcp.agent_session_store import AgentSessionStore
from coding_tools_mcp.agent_sessions import AgentSessionService
from coding_tools_mcp.operator_api import OperatorAPIService, OperatorPrincipal
from coding_tools_mcp.transcript import TranscriptStore
from coding_tools_mcp.validation import ValidationResult
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


class FakeBackend:
    backend_kind = "codex-app-server"

    def health(self):
        return BackendHealth(True, self.backend_kind)

    def create_thread(self, *, instructions=None):
        return BackendThread("thread-1", {})

    def resume_thread(self, thread_id, *, instructions=None):
        return BackendThread(thread_id, {})

    def send_turn(self, thread_id, message):
        return BackendTurn("turn-1", {})

    def interrupt_turn(self, thread_id, turn_id):
        return None

    def approve(self, approval_id, decision):
        return None

    def list_threads(self, *, limit=50):
        return []

    def close_thread(self, thread_id):
        return None

    def stream_events(self, *, timeout=None):
        return iter(())

    def drain_events(self, *, limit=100):
        return []

    def close(self):
        return None


class FakeValidationBackend:
    def __init__(self):
        self.ran = []
        self.closed = False

    def status(self):
        return {"ok": True, "backend": "fake", "status": "ready", "recipes": []}

    def run(self, recipe):
        self.ran.append(recipe)
        return ValidationResult("passed", recipe, exit_code=0)

    def close(self):
        self.closed = True


class ConversationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        workspace_root = root / "workspace"
        workspace_root.mkdir()
        self.catalog = WorkspaceCatalog(
            [WorkspaceEntry("ws-a", "Alpha", workspace_root, True, True)],
            "ws-a",
        )
        self.transcripts = TranscriptStore(root / "transcripts.sqlite3")
        self.sessions = AgentSessionService(
            AgentSessionStore(root / "agent-sessions.sqlite3"),
            self.catalog,
            lambda _workspace, _backend: FakeBackend(),
        )
        self.service = OperatorAPIService(
            self.sessions,
            self.catalog,
            transcript_store=self.transcripts,
            validation_backend_factory=lambda _workspace: FakeValidationBackend(),
        )
        self.principal = OperatorPrincipal("user-1", ("ws-a",))

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_list_unifies_imported_and_agent_backed_conversations(self):
        self.transcripts.record_messages(
            "ws-a",
            "imported-1",
            [{"message_id": "m1", "role": "user", "content": "imported"}],
            title="Imported",
            source="codex-import",
        )
        created = self.service.create_session(
            self.alice_principal(),
            {
                "workspace_id": "ws-a",
                "backend_kind": "codex",
                "instructions": "work",
            },
        )["session"]
        payload = self.service.list_conversations(self.principal, "ws-a")
        by_id = {item["conversation_id"]: item for item in payload["items"]}
        self.assertIn("imported-1", by_id)
        self.assertIsNone(by_id["imported-1"]["execution"])
        self.assertIsNotNone(by_id[created["conversation_id"]]["execution"])
        self.assertNotIn("backend_thread_id", by_id[created["conversation_id"]]["execution"])

    def test_detail_keeps_multiple_executions_on_one_conversation(self):
        first = self.service.create_session(
            self.principal,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        conversation_id = first["conversation_id"]
        second = self.service.create_execution(
            self.principal,
            "ws-a",
            conversation_id,
            {"backend_kind": "codex"},
        )["execution"]
        detail = self.service.get_conversation(self.principal, "ws-a", conversation_id)
        self.assertEqual(
            [item["session_id"] for item in detail["executions"]],
            [first["session_id"], second["session_id"]],
        )

    def test_admin_can_operate_historical_non_admin_execution(self):
        owner = OperatorPrincipal("legacy-owner", ("ws-a",))
        self.transcripts.record_messages("ws-a", "conversation-legacy", [], source="legacy")
        created = self.service.create_execution(
            owner,
            "ws-a",
            "conversation-legacy",
            {"backend_kind": "codex"},
        )["execution"]
        admin = OperatorPrincipal("admin", ("ws-a",), role="admin")
        sent = self.service.send_conversation_turn(
            admin,
            "ws-a",
            "conversation-legacy",
            {"session_id": created["session_id"], "message": "continue legacy work"},
        )["session"]
        self.assertEqual(sent["status"], "running")
        resumed = self.service.resume_conversation(
            admin,
            "ws-a",
            "conversation-legacy",
            {"session_id": created["session_id"]},
        )["execution"]
        self.assertEqual(resumed["status"], "running")
        stored = self.sessions.store.admin_get(created["session_id"], "ws-a")
        self.service._record_evidence(
            stored,
            "approval_state",
            "pending",
            {
                "approval_id": "approval-1",
                "session_id": created["session_id"],
                "status": "pending",
            },
        )
        self.service.approve_conversation_execution(
            admin,
            "ws-a",
            "conversation-legacy",
            "approval-1",
            {"session_id": created["session_id"], "decision": "approve"},
        )["execution"]
        closed = self.service.close_conversation_execution(
            admin,
            "ws-a",
            "conversation-legacy",
            {"session_id": created["session_id"]},
        )["execution"]
        stored = self.sessions.store.admin_get(created["session_id"], "ws-a")
        self.assertEqual(stored.owner_principal_id, "legacy-owner")
        self.assertEqual(closed["status"], "closed")

    def test_admin_can_validate_historical_non_admin_execution(self):
        owner = OperatorPrincipal("legacy-owner", ("ws-a",))
        self.transcripts.record_messages("ws-a", "conversation-legacy-validation", [], source="legacy")
        created = self.service.create_execution(
            owner,
            "ws-a",
            "conversation-legacy-validation",
            {"backend_kind": "codex"},
        )["execution"]
        admin = OperatorPrincipal("admin", ("ws-a",), role="admin")
        payload = self.service.run_conversation_validation(
            admin,
            "ws-a",
            "conversation-legacy-validation",
            {"session_id": created["session_id"], "recipe": "pytest:focus"},
        )
        self.assertEqual(payload["validation"]["status"], "passed")
        contexts = self.transcripts.list_recent_context(
            "ws-a",
            "conversation-legacy-validation",
            limit=10,
        )
        validation = [item for item in contexts if item["kind"] == "validation"]
        self.assertEqual(len(validation), 1)
        self.assertEqual(validation[0]["metadata"]["recipe"], "pytest:focus")

    def test_list_progress_and_detail_projection_use_the_same_evidence(self):
        created = self.service.create_session(
            self.principal,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        record = self.sessions.store.get(
            created["session_id"],
            "ws-a",
            self.principal.principal_id,
        )
        self.service._record_evidence(record, "task_instruction", "change test")
        self.service._record_evidence(record, "changed_path", "src/example.py")
        self.service._record_evidence(record, "explored_path", "tests/example.py")
        self.service._record_evidence(record, "validation", "passed", {"status": "passed"})
        listed = self.service.list_conversations(self.principal, "ws-a")
        item = next(entry for entry in listed["items"] if entry["conversation_id"] == created["conversation_id"])
        self.assertEqual(item["progress"]["changed_path_count"], 1)
        self.assertEqual(item["progress"]["explored_path_count"], 1)
        self.assertEqual(item["progress"]["validation_status"], "passed")
        continuation = self.service.continuation(self.principal, "ws-a", created["conversation_id"])["continuation"]
        self.assertEqual(item["progress"]["changed_path_count"], continuation["changes"]["total"])
        self.assertEqual(item["progress"]["validation_status"], continuation["validation"]["status"])

    def test_detail_paginates_messages_and_contexts_independently(self):
        self.transcripts.record_messages(
            "ws-a",
            "paged",
            [
                {"message_id": f"message-{index}", "role": "user", "content": f"message {index}"}
                for index in range(105)
            ],
        )
        self.transcripts.record_context(
            "ws-a",
            "paged",
            [
                {"context_id": f"context-{index}", "kind": "checkpoint", "content": f"context {index}"}
                for index in range(105)
            ],
        )
        first = self.service.get_conversation(
            self.principal, "ws-a", "paged", message_page_size=100, context_page_size=100,
        )
        second = self.service.get_conversation(
            self.principal, "ws-a", "paged", message_page=2, message_page_size=100,
            context_page=2, context_page_size=100,
        )
        self.assertEqual(first["messages_total"], 105)
        self.assertEqual(first["contexts_total"], 105)
        self.assertNotEqual(first["messages"][0]["message_id"], second["messages"][0]["message_id"])
        self.assertNotEqual(first["contexts"][0]["context_id"], second["contexts"][0]["context_id"])

    def test_approval_routes_to_the_requested_older_execution(self):
        first = self.service.create_session(
            self.principal,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        second = self.service.create_execution(
            self.principal,
            "ws-a",
            first["conversation_id"],
            {"backend_kind": "codex"},
        )["execution"]
        for session_id in (first["session_id"], second["session_id"]):
            record = self.sessions.store.get(session_id, "ws-a", self.principal.principal_id)
            self.service._record_evidence(
                record,
                "approval_state",
                "pending",
                {"approval_id": "shared-approval", "session_id": session_id, "status": "pending"},
            )
        result = self.service.approve_conversation_execution(
            self.principal,
            "ws-a",
            first["conversation_id"],
            "shared-approval",
            {"session_id": first["session_id"], "decision": "approve"},
        )
        self.assertEqual(result["execution"]["session_id"], first["session_id"])
        continuation = self.service.continuation(self.principal, "ws-a", first["conversation_id"])["continuation"]
        pending = continuation["approvals"]["items"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["session_id"], second["session_id"])

    def alice_principal(self):
        return self.principal


if __name__ == "__main__":
    unittest.main()
