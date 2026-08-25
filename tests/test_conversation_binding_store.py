from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path

from coding_tools_mcp.conversation_continuity import (
    ClientWindowIdentity,
    ConversationBindingStore,
    ConversationBindingStoreError,
    ConversationContinuityService,
    OWNERSHIP_AGENT,
    OWNERSHIP_MCP,
)
from coding_tools_mcp.transcript import TranscriptStore


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

    def test_schema_upgrades_and_reopens_ownership(self):
        common = {
            "principal_scope": "owner-digest",
            "workspace_id": "ws-a",
            "repo_scope": "repo-1",
        }
        binding = self.store.bind(conversation_id="conversation-a", identity=self.window_a, **common)
        reopened = ConversationBindingStore(self.store.path)
        self.assertEqual(reopened.resolve(identity=self.window_a, **common), binding)
        self.assertIsNone(reopened.ownership("ws-a", "conversation-a"))
        reopened.set_ownership(
            workspace_id="ws-a",
            conversation_id="conversation-a",
            owner_scope_digest="owner-digest",
            source="test",
        )
        self.assertEqual(
            reopened.ownership("ws-a", "conversation-a")["owner_scope_digest"],
            "owner-digest",
        )

    def test_backfill_is_unambiguous_only(self):
        rows = [
            {"workspace_id": "ws", "conversation_id": "owned", "owner_principal_id": "alice"},
            {"workspace_id": "ws", "conversation_id": "owned", "owner_principal_id": "alice"},
            {"workspace_id": "ws", "conversation_id": "mixed", "owner_principal_id": "alice"},
            {"workspace_id": "ws", "conversation_id": "mixed", "owner_principal_id": "bob"},
        ]
        result = self.store.backfill_ownership(rows)
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(result["ambiguous"], 1)
        owned = self.store.ownership("ws", "owned")
        mixed = self.store.ownership("ws", "mixed")
        assert owned is not None and mixed is not None
        self.assertNotEqual(owned["owner_scope_digest"], mixed["owner_scope_digest"])

    def test_v2_schema_migrates_only_unambiguous_mcp_bindings(self):
        path = Path(self.tmp.name) / "v2.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO schema_version(version) VALUES (2)")
            connection.execute(
                """
                CREATE TABLE conversation_bindings (
                    binding_digest TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL, principal_scope_digest TEXT NOT NULL,
                    transport_kind TEXT NOT NULL, repo_scope_digest TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE conversation_ownership (
                    workspace_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    owner_scope_digest TEXT NOT NULL, source TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id, conversation_id)
                )
                """
            )
            connection.execute(
                "INSERT INTO conversation_bindings VALUES(?,?,?,?,?,?,?,?)",
                ("binding", "conversation", "ws", "scope", "mcp", "repo", 1.0, 1.0),
            )
            connection.execute(
                "INSERT INTO conversation_ownership VALUES(?,?,?,?,?,?)",
                ("ws", "conversation", "scope", "mcp", 1.0, 1.0),
            )
        migrated = ConversationBindingStore(path)
        ownership = migrated.ownership("ws", "conversation")
        assert ownership is not None
        self.assertEqual(ownership["owner_kind"], OWNERSHIP_MCP)
        self.assertTrue(ownership["mcp_claimable"])
        self.assertEqual(ownership["repo_scope_digest"], "repo")

    def test_newer_schema_fails_closed(self):
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("DELETE FROM schema_version")
            connection.execute("INSERT INTO schema_version(version) VALUES (99)")
        with self.assertRaises(ConversationBindingStoreError):
            ConversationBindingStore(self.store.path)

    def test_backfill_considers_owners_beyond_the_first_thousand_records(self):
        rows = [
            {"workspace_id": "ws", "conversation_id": "shared", "owner_principal_id": "owner-a"}
            for _index in range(1000)
        ]
        rows.append({"workspace_id": "ws", "conversation_id": "shared", "owner_principal_id": "owner-b"})
        result = self.store.backfill_ownership(rows)
        self.assertEqual(result["ambiguous"], 1)
        ownership = self.store.ownership("ws", "shared")
        assert ownership is not None
        self.assertEqual(ownership["owner_kind"], "ambiguous")
        self.assertFalse(ownership["mcp_claimable"])

    def test_agent_principal_ownership_cannot_be_claimed_by_mcp_scope(self):
        transcripts = TranscriptStore(Path(self.tmp.name) / "transcripts.sqlite3")
        transcripts.record_messages("ws", "agent-conversation", [], source="agent")
        self.store.set_ownership(
            workspace_id="ws",
            conversation_id="agent-conversation",
            owner_scope_digest="related-looking-id",
            owner_kind=OWNERSHIP_AGENT,
            source="legacy-agent-sessions",
        )
        service = ConversationContinuityService(self.store, transcripts)
        service._scope = lambda _runtime: {
            "principal_scope": "related-looking-id",
            "workspace_id": "ws",
            "repo_scope": "repo",
        }
        runtime = type("Runtime", (), {"http_session_id": "session", "current_conversation_id": None})()
        with self.assertRaises(ConversationBindingStoreError):
            service.resume_explicit(runtime, "agent-conversation")

    def test_mcp_list_filters_owner_and_repo_before_pagination(self):
        transcripts = TranscriptStore(Path(self.tmp.name) / "transcripts.sqlite3")
        service = ConversationContinuityService(self.store, transcripts)
        owner_scope = "owner-a"
        foreign_scope = "owner-b"
        transcripts.record_messages("ws", "owned", [], source="test")
        self.store.set_ownership(
            workspace_id="ws", conversation_id="owned", owner_scope_digest=owner_scope,
            owner_kind=OWNERSHIP_MCP, repo_scope_digest="repo-a", mcp_claimable=True, source="test",
        )
        for index in range(40):
            conversation_id = f"foreign-{index:02d}"
            transcripts.record_messages("ws", conversation_id, [], source="test")
            self.store.set_ownership(
                workspace_id="ws", conversation_id=conversation_id, owner_scope_digest=foreign_scope,
                owner_kind=OWNERSHIP_MCP, repo_scope_digest="repo-a", mcp_claimable=True, source="test",
            )
        service._scope = lambda _runtime: {
            "principal_scope": owner_scope,
            "workspace_id": "ws",
            "repo_scope": "repo-a",
        }
        runtime = object()
        payload = service.list_for_runtime(runtime, limit=1)
        self.assertEqual(payload["total"], 1)
        self.assertEqual([item["conversation_id"] for item in payload["items"]], ["owned"])
        self.assertFalse(payload["truncated"])

        transcripts.record_messages("ws", "owned-second", [], source="test")
        self.store.set_ownership(
            workspace_id="ws", conversation_id="owned-second", owner_scope_digest=owner_scope,
            owner_kind=OWNERSHIP_MCP, repo_scope_digest="repo-a", mcp_claimable=True, source="test",
        )
        truncated = service.list_for_runtime(runtime, limit=1)
        self.assertEqual(truncated["total"], 2)
        self.assertEqual(len(truncated["items"]), 1)
        self.assertTrue(truncated["truncated"])

    def test_repo_change_denies_list_and_resume_until_the_original_repo_returns(self):
        transcripts = TranscriptStore(Path(self.tmp.name) / "transcripts.sqlite3")
        service = ConversationContinuityService(self.store, transcripts)
        runtime = type("Runtime", (), {"http_session_id": "retained", "repo": "repo-a", "current_conversation_id": None})()
        service._scope = lambda item: {
            "principal_scope": "owner",
            "workspace_id": "ws",
            "repo_scope": item.repo,
        }
        created = service.start_or_resume_exact(runtime=runtime)
        runtime.repo = "repo-b"
        self.assertEqual(service.list_for_runtime(runtime)["total"], 0)
        self.assertIsNone(service.recover_retained(runtime))
        with self.assertRaises(ConversationBindingStoreError):
            service.resume_explicit(runtime, created["conversation_id"])
        runtime.repo = "repo-a"
        recovered = service.recover_retained(runtime)
        assert recovered is not None
        self.assertEqual(recovered["conversation_id"], created["conversation_id"])

    def test_random_ids_and_exact_restart_recovery(self):
        class Transcripts:
            def record_messages(self, _workspace_id, conversation_id, _messages, **_kwargs):
                return {"conversation_id": conversation_id}

            def record_context(self, *_args, **_kwargs):
                return {}

            def list_conversations(self, _workspace_id, **_kwargs):
                return {"items": []}

        service = ConversationContinuityService(self.store, Transcripts())
        owner = "owner-digest"

        def scope(_runtime):
            return {
                "principal_scope": owner,
                "workspace_id": "ws",
                "repo_scope": "repo",
            }

        service._scope = scope

        class Runtime:
            http_session_id = "retained-header"
            workspace_binding = type("Binding", (), {"workspace_id": "ws", "root": Path(self.tmp.name)})()
            authorization_context = type(
                "Context",
                (),
                {"authorization_key": lambda _self, _workspace_id: ("bearer", None, None, "ws")},
            )()

        other_store = ConversationBindingStore(Path(self.tmp.name) / "bindings-two.sqlite3")
        other_service = ConversationContinuityService(other_store, Transcripts())
        other_service._scope = scope
        first_runtime = Runtime()
        first_runtime.http_session_id = "window-one"
        first = service.start_or_resume_exact(runtime=first_runtime)
        second = other_service.start_or_resume_exact(runtime=Runtime())
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])

        # Simulate process replacement: a new Runtime carries the retained opaque
        # transport identity while durable stores remain intact.
        recovered_runtime = Runtime()
        recovered_runtime.http_session_id = "window-one"
        recovered = service.recover_retained(recovered_runtime)
        assert recovered is not None
        self.assertTrue(recovered["resumed"])
        self.assertEqual(recovered["conversation_id"], first["conversation_id"])
        self.assertEqual(recovered_runtime.current_conversation_id, first["conversation_id"])

        unknown_runtime = Runtime()
        unknown_runtime.http_session_id = "window-three"
        self.assertIsNone(other_service.recover_retained(unknown_runtime))

        # The retained identity that created the Conversation resolves again.
        retained_runtime = Runtime()
        retained_runtime.http_session_id = "window-one"
        retained = service.recover_retained(retained_runtime)
        assert retained is not None
        self.assertEqual(retained["conversation_id"], first["conversation_id"])

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
        self.store.set_ownership(
            workspace_id="ws-a",
            conversation_id="conversation-a",
            owner_scope_digest="current-owner",
            owner_kind=OWNERSHIP_MCP,
            repo_scope_digest="test-repo",
            mcp_claimable=True,
            source="test",
        )

        class Runtime:
            http_session_id = "fresh-session"
            workspace_binding = type("Binding", (), {"workspace_id": "ws-a", "root": Path(self.tmp.name)})()
            authorization_context = type(
                "Context",
                (),
                {
                    "authorization_key": lambda _self, workspace_id: (
                        "bearer",
                        None,
                        None,
                        workspace_id,
                    )
                },
            )()

        class Scope:
            @staticmethod
            def _scope(_runtime):
                return {
                    "principal_scope": "current-owner",
                    "workspace_id": "ws-a",
                    "repo_scope": "test-repo",
                }

        service._scope = Scope._scope

        resumed = service.resume_explicit(Runtime(), "conversation-a")
        self.assertEqual(resumed["conversation_id"], "conversation-a")
        with self.assertRaises(ConversationBindingStoreError):
            service.resume_explicit(Runtime(), "missing-conversation")


if __name__ == "__main__":
    unittest.main()
