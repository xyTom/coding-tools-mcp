from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_tools_mcp.conversation_continuity import (
    ClientWindowIdentity,
    ConversationBindingStore,
    ConversationBindingStoreError,
    ConversationContinuityService,
)


class ConversationBindingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConversationBindingStore(Path(self.tmp.name) / "bindings.sqlite3")
        self.window_a = ClientWindowIdentity.from_mcp_session("session-a")
        self.window_b = ClientWindowIdentity.from_mcp_session("session-b")

    def tearDown(self):
        self.tmp.cleanup()

    def test_domain_and_transport_change_digest(self):
        mcp = ClientWindowIdentity.from_transport("mcp", "same-opaque-id")
        actions = ClientWindowIdentity.from_transport("actions", "same-opaque-id")
        self.assertNotEqual(mcp.window_digest, actions.window_digest)
        self.assertNotEqual(mcp.window_digest, "same-opaque-id")
        self.assertNotIn("same-opaque-id", repr(mcp))

    def test_exact_binding_is_restart_safe_and_windows_are_isolated(self):
        common = {
            "principal_scope": "principal-1",
            "workspace_id": "ws-a",
            "repo_scope": "repo-1",
        }
        first = self.store.bind(conversation_id="conversation-a", identity=self.window_a, **common)
        second = self.store.bind(conversation_id="conversation-b", identity=self.window_b, **common)
        self.assertNotEqual(first.binding_digest, second.binding_digest)
        resolved = self.store.resolve(identity=self.window_a, **common)
        assert resolved is not None
        self.assertEqual(resolved.conversation_id, "conversation-a")
        changed_principal = {**common, "principal_scope": "principal-2"}
        self.assertIsNone(self.store.resolve(identity=self.window_a, **changed_principal))

    def test_explicit_resume_requires_existing_workspace_conversation(self):
        class Transcripts:
            def conversation_detail(self, workspace_id, conversation_id, **_kwargs):
                if (workspace_id, conversation_id) == ("ws-a", "conversation-a"):
                    return {"conversation_id": conversation_id}
                return None

            def record_messages(self, workspace_id, conversation_id, _messages, **kwargs):
                return {"conversation_id": conversation_id, **kwargs}

            def list_conversations(self, workspace_id, **_kwargs):
                return {"items": [{"workspace_id": workspace_id}]}

        service = ConversationContinuityService(self.store, Transcripts())

        class Runtime:
            http_session_id = "fresh-session"
            workspace_binding = type("Binding", (), {"workspace_id": "ws-a", "root": Path(self.tmp.name)})()
            authorization_context = type(
                "Context",
                (),
                {"authorization_key": lambda _self, workspace_id: ("bearer", None, None, workspace_id)},
            )()

        resumed = service.resume_explicit(Runtime(), "conversation-a")
        self.assertEqual(resumed["conversation_id"], "conversation-a")
        with self.assertRaises(ConversationBindingStoreError):
            service.resume_explicit(Runtime(), "missing-conversation")


if __name__ == "__main__":
    unittest.main()
