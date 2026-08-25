from __future__ import annotations

import json
import unittest
import tempfile
import base64
import shutil
import subprocess
from pathlib import Path

from coding_tools_mcp.conversation_evidence import (
    MAX_HANDOFF_BYTES,
    continuation_feedback,
    evidence_entry,
    handoff_brief,
)
from coding_tools_mcp.conversation_recorder import EVIDENCE_HARD_CAP, ConversationEvidenceRecorder
from coding_tools_mcp.conversation_continuity import ConversationBindingStore, ConversationContinuityService
from coding_tools_mcp.server import AuthorizationContext, Runtime, WorkspaceBinding
from coding_tools_mcp.transcript import TranscriptStore


class ConversationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TranscriptStore(Path(self.tmp.name) / "transcripts.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()
    def test_evidence_is_bounded_and_rejects_unsafe_paths(self):
        self.assertIsNone(evidence_entry("not-a-kind", "x"))
        self.assertIsNone(evidence_entry("changed_path", "../../outside"))
        entry = evidence_entry(
            "changed_path",
            "src\\very\\long\\" + "a" * 1000,
            {"operation": "apply_patch", "secret": "do-not-store"},
        )
        assert entry is not None
        self.assertNotIn("secret", entry["metadata"])
        self.assertLessEqual(len(entry["content"]), 512)

    def test_continuation_is_deterministic_and_rule_based(self):
        detail = {
            "contexts": [
                {"kind": "task_instruction", "content": "fix failing test"},
                {"kind": "changed_path", "content": "src/module.py"},
                {"kind": "validation", "content": "failed"},
                {"kind": "failure", "content": "AssertionError"},
            ]
        }
        executions = [{"session_id": "agent-1", "status": "running", "last_turn_id": "turn-1"}]
        first = continuation_feedback(detail, executions)
        second = continuation_feedback(detail, executions)
        self.assertEqual(first, second)
        self.assertEqual(first["validation"]["status"], "failed")
        self.assertIn("resolve_failure", first["suggested_actions"])
        self.assertEqual(first["changes"]["paths"], ["src/module.py"])

    def test_handoff_is_bounded_for_oversized_history(self):
        detail = {
            "conversation_id": "conversation-1",
            "contexts": [
                {"kind": "task_instruction", "content": "x" * 20000},
                *[
                    {"kind": "explored_path", "content": f"src/generated-{index}.py"}
                    for index in range(1000)
                ],
            ],
        }
        brief = handoff_brief(detail)
        encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_HANDOFF_BYTES)
        self.assertLessEqual(len(brief["instruction"]), 512)
        self.assertLessEqual(len(brief["explored_paths"]), 50)

    def test_recorder_dedupes_paths_and_projects_latest_attempt(self):
        recorder = ConversationEvidenceRecorder(self.store)
        recorder.record("ws", "conversation", "task_instruction", "old task")
        for index in range(120):
            recorder.record("ws", "conversation", "explored_path", f"src/old-{index}.py")
        recorder.record("ws", "conversation", "task_instruction", "new task")
        for _index in range(10):
            recorder.record("ws", "conversation", "changed_path", "src/new.py")
        contexts = self.store.list_recent_context("ws", "conversation", limit=1000)
        result = continuation_feedback({"conversation_id": "conversation", "contexts": contexts})
        self.assertEqual(result["previous_instruction"], "new task")
        self.assertEqual(result["changes"]["paths"], ["src/new.py"])
        self.assertTrue(all(item["metadata"]["attempt_id"] == result["attempt"]["attempt_id"] for item in contexts if item["kind"] == "changed_path"))

    def test_retention_hard_cap_preserves_pending_approval(self):
        recorder = ConversationEvidenceRecorder(self.store)
        recorder.record(
            "ws",
            "conversation",
            "approval_state",
            "pending",
            {"approval_id": "approval-live", "status": "pending"},
        )
        for index in range(130):
            recorder.record("ws", "conversation", "validation", "failed", {"recipe": f"r-{index}"})
            recorder.record("ws", "conversation", "failure", f"failure-{index}")
            recorder.record("ws", "conversation", "checkpoint", f"checkpoint-{index}")
            recorder.record("ws", "conversation", "guidance", f"guidance-{index}")
        count = int(self.store.conversation_detail("ws", "conversation", context_page_size=1)["contexts_total"])
        self.assertLessEqual(count, EVIDENCE_HARD_CAP)
        contexts = self.store.list_recent_context("ws", "conversation", limit=EVIDENCE_HARD_CAP)
        approvals = [item for item in contexts if item.get("kind") == "approval_state"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["metadata"]["approval_id"], "approval-live")

    def test_terminal_job_and_processed_approval_are_not_active_or_pending(self):
        contexts = [
            {"kind": "task_instruction", "content": "task", "metadata": {"attempt_id": "attempt-1"}, "created_at": 3},
            {"kind": "job_state", "content": "completed", "metadata": {"job_id": "job-1", "status": "completed"}, "created_at": 2},
            {"kind": "approval_state", "content": "accept", "metadata": {"approval_id": "approval-1", "status": "accept"}, "created_at": 1},
        ]
        result = continuation_feedback({"contexts": contexts})
        self.assertEqual(result["jobs"]["active"], 0)
        self.assertEqual(result["approvals"]["pending"], 0)

    def test_latest_attempt_survives_more_than_five_hundred_later_events_and_reopen(self):
        recorder = ConversationEvidenceRecorder(self.store)
        recorder.record("ws", "conversation", "task_instruction", "durable task")
        for index in range(520):
            recorder.record("ws", "conversation", "guidance", f"note-{index}")
        expected = recorder.current_attempt("ws", "conversation")
        self.assertIsNotNone(expected)
        reopened = TranscriptStore(self.store.path)
        self.assertEqual(reopened.latest_instruction_attempt("ws", "conversation"), expected)

    def test_failure_before_a_later_passing_validation_is_not_unresolved(self):
        contexts = [
            {"kind": "task_instruction", "content": "task", "metadata": {"attempt_id": "attempt"}, "updated_at": 1},
            {"kind": "failure", "content": "old failure", "metadata": {"attempt_id": "attempt"}, "updated_at": 2},
            {"kind": "validation", "content": "passed", "metadata": {"attempt_id": "attempt"}, "updated_at": 3},
        ]
        result = continuation_feedback({"contexts": contexts})
        self.assertEqual(result["validation"]["unresolved_failure_count"], 0)
        later_failure = [*contexts, {"kind": "failure", "content": "new failure", "metadata": {"attempt_id": "attempt"}, "updated_at": 4}]
        result = continuation_feedback({"contexts": later_failure})
        self.assertEqual(result["validation"]["unresolved_failure_count"], 1)

    def test_pruning_caps_more_than_five_hundred_protected_states(self):
        recorder = ConversationEvidenceRecorder(self.store)
        for index in range(501):
            recorder.record(
                "ws",
                "conversation",
                "approval_state",
                "pending",
                {"approval_id": f"approval-{index}", "session_id": f"session-{index}", "status": "pending"},
            )
        detail = self.store.conversation_detail("ws", "conversation", context_page_size=1)
        assert detail is not None
        self.assertEqual(detail["contexts_total"], EVIDENCE_HARD_CAP)
        projection = continuation_feedback({"contexts": self.store.list_recent_context("ws", "conversation", limit=500)})
        self.assertEqual(projection["approvals"]["pending"], EVIDENCE_HARD_CAP)
        self.assertTrue(projection["approvals"]["truncated"])

    def test_state_updates_are_ordered_by_the_latest_transition(self):
        recorder = ConversationEvidenceRecorder(self.store)
        recorder.record("ws", "conversation", "task_instruction", "task")
        recorder.record("ws", "conversation", "job_state", "queued", {"job_id": "first", "status": "queued"})
        recorder.record("ws", "conversation", "job_state", "queued", {"job_id": "second", "status": "queued"})
        recorder.record("ws", "conversation", "job_state", "running", {"job_id": "first", "status": "running"})
        contexts = self.store.list_recent_context("ws", "conversation", limit=10)
        self.assertEqual(contexts[0]["metadata"]["job_id"], "first")
        result = continuation_feedback({"contexts": contexts})
        self.assertEqual(result["jobs"]["items"][0]["id"], "first")

    def test_runtime_dispatch_records_apply_patch_paths_and_never_records_patch_body(self):
        root = Path(self.tmp.name) / "workspace"
        root.mkdir()
        runtime = self._runtime(root)
        patch = """*** Begin Patch
*** Add File: first.txt
+first private file content
*** Add File: second.txt
+second private file content
*** End Patch
"""
        result = runtime.call_tool("apply_patch", {"patch": patch})
        self.assertFalse(result["isError"], result)
        contexts = self.store.list_recent_context("ws", str(runtime.current_conversation_id), limit=50)
        changed = [item["content"] for item in contexts if item["kind"] == "changed_path"]
        self.assertEqual(set(changed), {"first.txt", "second.txt"})
        durable = json.dumps(contexts, ensure_ascii=False)
        self.assertNotIn("private file content", durable)
        self.assertNotIn("runtime-session-id", durable)

    def test_runtime_dispatch_records_move_and_ignores_dry_run_mutations(self):
        root = Path(self.tmp.name) / "workspace"
        root.mkdir()
        runtime = self._runtime(root)
        runtime.call_tool("apply_patch", {"patch": """*** Begin Patch
*** Add File: old.txt
+old
*** End Patch
"""})
        runtime.call_tool("apply_patch", {"patch": """*** Begin Patch
*** Update File: old.txt
*** Move to: new.txt
*** End Patch
"""})
        runtime.call_tool("apply_patch", {"patch": """*** Begin Patch
*** Add File: dry.txt
+dry run only
*** End Patch
""", "dry_run": True})
        contexts = self.store.list_recent_context("ws", str(runtime.current_conversation_id), limit=50)
        changed = [item["content"] for item in contexts if item["kind"] == "changed_path"]
        self.assertIn("old.txt", changed)
        self.assertIn("new.txt", changed)
        self.assertNotIn("dry.txt", changed)

    def test_runtime_dispatch_records_view_image_and_git_checkpoints(self):
        root = Path(self.tmp.name) / "workspace"
        root.mkdir()
        (root / "image.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JFCkAAAAASUVORK5CYII="
        ))
        git = shutil.which("git")
        if git is None:
            self.fail("git is required for Conversation evidence acceptance")
        for command in (
            [git, "init"],
            [git, "config", "user.email", "tests@example.invalid"],
            [git, "config", "user.name", "Conversation tests"],
        ):
            subprocess.run(command, cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run([git, "add", "tracked.txt"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([git, "commit", "-m", "initial"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runtime = self._runtime(root)
        self.assertFalse(runtime.call_tool("view_image", {"path": "image.png"})["isError"])
        self.assertFalse(runtime.call_tool("git_status", {})["isError"])
        self.assertFalse(runtime.call_tool("git_diff", {})["isError"])
        self.assertFalse(runtime.call_tool("git_log", {})["isError"])
        self.assertFalse(runtime.call_tool("git_show", {})["isError"])
        self.assertFalse(runtime.call_tool("git_blame", {"path": "tracked.txt"})["isError"])
        contexts = self.store.list_recent_context("ws", str(runtime.current_conversation_id), limit=50)
        explored = [item["content"] for item in contexts if item["kind"] == "explored_path"]
        self.assertIn("image.png", explored)
        self.assertIn("tracked.txt", explored)
        checkpoints = [item["content"] for item in contexts if item["kind"] == "checkpoint"]
        self.assertEqual(
            set(checkpoints),
            {"git:git_status", "git:git_diff", "git:git_log", "git:git_show"},
        )

    def _runtime(self, root: Path) -> Runtime:
        runtime = Runtime(
            root,
            permission_mode="dangerous",
            transport="http",
            workspace_binding=WorkspaceBinding("ws", root, "bearer"),
            authorization_context=AuthorizationContext("bearer"),
        )
        runtime.http_session_id = "runtime-session-id"
        runtime.conversation_continuity = ConversationContinuityService(
            ConversationBindingStore(Path(self.tmp.name) / "bindings.sqlite3"),
            self.store,
        )
        return runtime


if __name__ == "__main__":
    unittest.main()
