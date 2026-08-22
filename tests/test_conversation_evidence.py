from __future__ import annotations

import json
import unittest

from coding_tools_mcp.conversation_evidence import (
    MAX_HANDOFF_BYTES,
    continuation_feedback,
    evidence_entry,
    handoff_brief,
)


class ConversationEvidenceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
