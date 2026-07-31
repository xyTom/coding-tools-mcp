from __future__ import annotations

import ctypes
import inspect
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import chat_cli
from coding_tools_mcp.admin import AdminNotFoundError, AdminService, AdminServiceError, gateway_file_revision
from coding_tools_mcp.codex_sessions import CodexSessionError, CodexSessionScanner, ScanPolicy
from coding_tools_mcp.secret_vault import SecretVault
from coding_tools_mcp.settings_store import ServerSettingsStore
from coding_tools_mcp.transcript import TranscriptStore, WorkspaceScope
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


class ChatPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a = self.root / "workspace-a"
        self.b = self.root / "workspace-b"
        self.a.mkdir()
        self.b.mkdir()
        self.store = TranscriptStore(self.root / "transcripts.sqlite3")
        self.scope_a = WorkspaceScope.create("a", self.a)
        self.scope_b = WorkspaceScope.create("b", self.b)
        self.scanner = CodexSessionScanner()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_workspace_identity_partitions_queries_cache_and_stable_deletes(self) -> None:
        service_a = self.store.scoped("a", self.a)
        service_b = self.store.scoped("b", self.b)
        service_a.record_messages(
            "same-conversation",
            [{"message_id": "same-message", "role": "user", "content": "secret-a"}],
        )
        service_b.record_messages(
            "same-conversation",
            [{"message_id": "same-message", "role": "user", "content": "secret-b"}],
        )
        self.assertEqual(service_a.list_conversations()["total"], 1)
        self.assertEqual(service_b.list_conversations()["total"], 1)
        self.assertEqual(
            service_a.conversation_detail("same-conversation")["messages"][0]["content"],
            "secret-a",
        )
        deleted = self.store.delete_message("a", "same-message")
        self.assertEqual(deleted["affected_count"], 1)
        self.assertEqual(self.store.delete_message("a", "same-message")["affected_count"], 0)
        self.assertEqual(
            service_b.conversation_detail("same-conversation")["messages"][0]["content"],
            "secret-b",
        )
        with self.assertRaises(TypeError):
            service_a.list_conversations(workspace_id="b")

    def test_database_reopen_preserves_workspace_partition(self) -> None:
        self.store.record_messages(
            "a",
            "reopen",
            [{"message_id": "reopen-message", "role": "user", "content": "persisted"}],
        )
        reopened = TranscriptStore(self.root / "transcripts.sqlite3")
        self.assertEqual(reopened.list_conversations("a")["total"], 1)
        self.assertEqual(reopened.list_conversations("b")["total"], 0)
        self.assertEqual(
            reopened.conversation_detail("a", "reopen")["messages"][0]["content"],
            "persisted",
        )

    def test_summary_pagination_omits_full_content_until_detail(self) -> None:
        for index in range(5):
            self.store.record_messages(
                "a",
                f"conversation-{index}",
                [{"message_id": f"m-{index}", "role": "user", "content": "X" * 1000}],
                title=f"Title {index}",
            )
        first = self.store.list_conversations("a", page=1, page_size=2)
        second = self.store.list_conversations("a", page=2, page_size=2)
        self.assertEqual(first["total"], 5)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 2)
        serialized = json.dumps(first)
        self.assertNotIn("X" * 500, serialized)
        detail = self.store.conversation_detail("a", first["items"][0]["conversation_id"])
        self.assertEqual(len(detail["messages"][0]["content"]), 1000)

    def test_crlf_partial_jsonl_is_a_single_item_error_and_import_continues(self) -> None:
        sessions = self.a / "sessions"
        sessions.mkdir()
        records = [
            {"type": "session_meta", "payload": {"id": "session-1"}},
            {"type": "user_message", "timestamp": "2026-07-30T01:00:00Z", "message": "private body"},
        ]
        path = sessions / "rollout.jsonl"
        path.write_bytes(
            ("\r\n".join(json.dumps(item) for item in records) + "\r\n{\"type\":").encode("utf-8")
        )
        preview = self.scanner.scan(self.scope_a, roots=["sessions"])
        self.assertEqual(preview["candidate_count"], 1)
        candidate = preview["candidates"][0]
        self.assertEqual(candidate["message_count"], 1)
        self.assertEqual(candidate["parse_error_count"], 1)
        self.assertNotIn("private body", json.dumps(preview))
        imported = self.scanner.import_candidates(
            self.store,
            self.scope_a,
            roots=["sessions"],
            candidate_ids=[candidate["candidate_id"]],
        )
        self.assertEqual(imported["inserted_count"], 1)
        detail = self.store.conversation_detail("a", candidate["conversation_id"])
        self.assertEqual(detail["messages"][0]["content"], "private body")

    def test_windows_encoding_invalid_encoding_and_size_limits(self) -> None:
        sessions = self.a / "sessions"
        sessions.mkdir()
        gb = sessions / "gb.jsonl"
        gb.write_bytes(
            json.dumps(
                {"type": "user_message", "message": "中文 Windows 编码"},
                ensure_ascii=False,
            ).encode("gb18030")
        )
        bad = sessions / "bad.jsonl"
        bad.write_bytes(b"\xff\xff\x81")
        huge = sessions / "huge.jsonl"
        huge.write_bytes(b"x" * 2048)
        result = self.scanner.scan(
            self.scope_a,
            roots=["sessions"],
            policy=ScanPolicy(max_file_bytes=1024, max_total_bytes=4096),
        )
        by_name = {item["relative_path"].split("/")[-1]: item for item in result["candidates"]}
        self.assertEqual(by_name["gb.jsonl"]["encoding"], "gb18030")
        self.assertEqual(by_name["gb.jsonl"]["message_count"], 1)
        self.assertEqual(by_name["bad.jsonl"]["fatal_error"], "invalid_encoding")
        self.assertEqual(by_name["huge.jsonl"]["fatal_error"], "file_too_large")

    def test_scan_rejects_absolute_parent_and_outside_paths_and_skips_symlink_escape(self) -> None:
        sessions = self.a / "sessions"
        sessions.mkdir()
        outside = self.root / "outside.jsonl"
        outside.write_text(json.dumps({"type": "user_message", "message": "outside"}), encoding="utf-8")
        with self.assertRaises(CodexSessionError):
            self.scanner.scan(self.scope_a, roots=[str(sessions.resolve())])
        with self.assertRaises(CodexSessionError):
            self.scanner.scan(self.scope_a, roots=["../"])
        from coding_tools_mcp.codex_sessions import parse_codex_session_file

        with self.assertRaises(CodexSessionError):
            parse_codex_session_file(self.scope_a, outside)
        link = sessions / "escape.jsonl"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("Host does not permit creating a test symlink.")
        result = self.scanner.scan(self.scope_a, roots=["sessions"])
        self.assertEqual(result["candidate_count"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows file-sharing behavior")
    def test_locked_windows_file_becomes_item_error_not_list_failure(self) -> None:
        sessions = self.a / "sessions"
        sessions.mkdir()
        path = sessions / "locked.jsonl"
        path.write_text(json.dumps({"type": "user_message", "message": "locked"}), encoding="utf-8")
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x40000000,
            0,
            None,
            3,
            0x80,
            None,
        )
        self.assertNotEqual(handle, -1)
        try:
            result = self.scanner.scan(self.scope_a, roots=["sessions"])
            self.assertEqual(result["candidate_count"], 1)
            self.assertIn(result["candidates"][0]["fatal_error"], {"stat_failed", "read_failed"})
        finally:
            kernel32.CloseHandle(handle)

    def test_file_count_depth_and_total_read_limits_are_enforced(self) -> None:
        sessions = self.a / "sessions"
        deep = sessions / "one" / "two"
        deep.mkdir(parents=True)
        for index in range(4):
            (sessions / f"{index}.jsonl").write_text(
                json.dumps({"type": "user_message", "message": str(index)}), encoding="utf-8"
            )
        (deep / "deep.jsonl").write_text(json.dumps({"type": "user_message", "message": "deep"}), encoding="utf-8")
        result = self.scanner.scan(
            self.scope_a,
            roots=["sessions"],
            policy=ScanPolicy(max_depth=0, max_files=2, max_total_bytes=1024, max_file_bytes=1024),
        )
        self.assertLessEqual(result["files_considered"], 2)
        self.assertTrue(any(item["code"] == "file_limit" for item in result["scan_errors"]))
        self.assertNotIn("deep.jsonl", json.dumps(result))

    def test_imported_message_and_session_ids_are_path_stable_and_do_not_collide(self) -> None:
        sessions = self.a / "sessions"
        sessions.mkdir()
        for name, text in (("one.jsonl", "one"), ("two.jsonl", "two")):
            (sessions / name).write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "same-source-session"}}),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "id": "same-source-message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": text}],
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
        preview = self.scanner.scan(self.scope_a, roots=["sessions"])
        ids = [item["candidate_id"] for item in preview["candidates"]]
        self.assertEqual(len(set(ids)), 2)
        imported = self.scanner.import_candidates(
            self.store, self.scope_a, roots=["sessions"], candidate_ids=ids
        )
        self.assertEqual(imported["inserted_count"], 2)
        sessions_payload = self.store.list_imported_sessions("a")
        self.assertEqual(sessions_payload["total"], 2)
        message_ids: set[str] = set()
        for item in preview["candidates"]:
            detail = self.store.conversation_detail("a", item["conversation_id"])
            message_ids.add(detail["messages"][0]["message_id"])
        self.assertEqual(len(message_ids), 2)

    def test_scanner_cache_key_contains_workspace_identity(self) -> None:
        for scope, root, body in ((self.scope_a, self.a, "A"), (self.scope_b, self.b, "B")):
            folder = root / "sessions"
            folder.mkdir()
            (folder / "same.jsonl").write_text(
                json.dumps({"type": "user_message", "message": body}), encoding="utf-8"
            )
            self.scanner.scan(scope, roots=["sessions"])
        keys = list(self.scanner._cache)  # contract-level cache-key inspection
        self.assertEqual({key[0] for key in keys}, {"a", "b"})

    def test_no_chat_or_transcript_content_is_sent_to_telemetry(self) -> None:
        import coding_tools_mcp.codex_sessions as codex_sessions
        import coding_tools_mcp.transcript as transcript

        sources = inspect.getsource(transcript) + inspect.getsource(codex_sessions)
        self.assertNotIn("telemetry", sources.lower())
        self.assertNotIn("SessionTelemetry", sources)


class ChatAdminServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a = self.root / "a"
        self.b = self.root / "b"
        self.a.mkdir()
        self.b.mkdir()
        catalog = WorkspaceCatalog(
            [
                WorkspaceEntry("a", "A", self.a, True, True),
                WorkspaceEntry("b", "B", self.b, True, False),
            ],
            "a",
        )
        settings = {**catalog.settings_payload(), "workspace": str(self.a)}
        self.settings_store = ServerSettingsStore(self.root / "settings.json")
        self.settings_store.write(settings)
        self.transcripts = TranscriptStore(self.root / "transcripts.sqlite3")
        gateway = self.root / "gateway.json"
        self.service = AdminService(
            settings_store=self.settings_store,
            active_settings=settings,
            fallback_workspace=self.a,
            gateway_path=gateway,
            active_gateway_revision=gateway_file_revision(gateway),
            secret_vault=SecretVault(self.root / "vault.json", "master-key"),
            transcript_store=self.transcripts,
            session_scanner=CodexSessionScanner(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_admin_summary_detail_and_idempotent_workspace_keyed_delete(self) -> None:
        self.service.dispatch(
            "POST",
            "/admin/api/chat/conversations/a/c/messages",
            {"messages": [{"message_id": "same", "role": "user", "content": "body-a"}]},
            {},
        )
        self.service.dispatch(
            "POST",
            "/admin/api/chat/conversations/b/c/messages",
            {"messages": [{"message_id": "same", "role": "user", "content": "body-b"}]},
            {},
        )
        summary = self.service.dispatch("GET", "/admin/api/chat/conversations", {}, {"page_size": "1"})
        self.assertEqual(summary["total"], 2)
        self.assertNotIn("body-a", json.dumps(summary))
        detail = self.service.dispatch("GET", "/admin/api/chat/conversations/a/c", {}, {})
        self.assertEqual(detail["messages"][0]["content"], "body-a")
        deleted = self.service.dispatch("DELETE", "/admin/api/chat/messages/a/same", {}, {})
        self.assertEqual(deleted["affected_count"], 1)
        repeated = self.service.dispatch("DELETE", "/admin/api/chat/messages/a/same", {}, {})
        self.assertEqual(repeated["affected_count"], 0)
        other = self.service.dispatch("GET", "/admin/api/chat/conversations/b/c", {}, {})
        self.assertEqual(other["messages"][0]["content"], "body-b")

    def test_admin_scan_rejects_unknown_workspace_and_path_escape(self) -> None:
        with self.assertRaises(AdminNotFoundError):
            self.service.codex_scan({"workspace_id": "missing", "roots": ["."]})
        with self.assertRaises(AdminServiceError):
            self.service.codex_scan({"workspace_id": "a", "roots": ["../"]})

    def test_admin_import_and_session_delete_use_workspace_and_stable_id(self) -> None:
        folder = self.a / "sessions"
        folder.mkdir()
        (folder / "one.jsonl").write_text(
            json.dumps({"type": "user_message", "message": "imported body"}), encoding="utf-8"
        )
        preview = self.service.codex_scan({"workspace_id": "a", "roots": ["sessions"]})
        candidate = preview["candidates"][0]
        imported = self.service.codex_import(
            {"workspace_id": "a", "roots": ["sessions"], "candidate_ids": [candidate["candidate_id"]]}
        )
        self.assertEqual(imported["inserted_count"], 1)
        sessions = self.service.codex_sessions({"workspace_id": "a"})
        self.assertEqual(sessions["total"], 1)
        deleted = self.service.dispatch(
            "DELETE",
            f"/admin/api/codex/sessions/a/{candidate['session_id']}",
            {},
            {},
        )
        self.assertEqual(deleted["deleted_session_count"], 1)
        self.assertEqual(deleted["deleted_conversation_count"], 1)
        self.assertEqual(deleted["deleted_message_count"], 1)
        self.assertEqual(deleted["affected_count"], 3)
        repeated = self.service.dispatch(
            "DELETE",
            f"/admin/api/codex/sessions/a/{candidate['session_id']}",
            {},
            {},
        )
        self.assertEqual(repeated["affected_count"], 0)


class ChatCliTests(unittest.TestCase):
    def test_cli_reads_gb18030_bytes_and_remains_workspace_scoped(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            db = root / "chat.sqlite3"
            payload = {
                "conversation_id": "conversation-1",
                "message_id": "message-1",
                "role": "assistant",
                "content": "中文 CLI 内容\r\n第二行",
            }

            class BytesStdin:
                def __init__(self, raw: bytes) -> None:
                    self.buffer = io.BytesIO(raw)

            output = io.StringIO()
            with patch.object(sys, "stdin", BytesStdin(json.dumps(payload, ensure_ascii=False).encode("gb18030"))), redirect_stdout(output):
                code = chat_cli.main(
                    [
                        "--db-path", str(db),
                        "--workspace-id", "workspace-a",
                        "--workspace-root", str(workspace),
                        "record-message", "--stdin-json",
                    ]
                )
            self.assertEqual(code, 0)
            recorded = json.loads(output.getvalue())
            self.assertEqual(recorded["inserted_count"], 1)
            detail = TranscriptStore(db).conversation_detail("workspace-a", "conversation-1")
            self.assertIn("第二行", detail["messages"][0]["content"])
            self.assertIsNone(TranscriptStore(db).conversation_detail("workspace-b", "conversation-1"))


if __name__ == "__main__":
    unittest.main()
