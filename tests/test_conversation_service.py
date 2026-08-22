from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_tools_mcp.agent_backends.base import BackendHealth, BackendThread, BackendTurn
from coding_tools_mcp.agent_session_store import AgentSessionStore
from coding_tools_mcp.agent_sessions import AgentSessionService
from coding_tools_mcp.operator_api import OperatorAPIService, OperatorPrincipal
from coding_tools_mcp.transcript import TranscriptStore
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

    def alice_principal(self):
        return self.principal


if __name__ == "__main__":
    unittest.main()
