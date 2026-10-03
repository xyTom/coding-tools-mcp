"""Regression tests for apply_patch locating and already-applied detection.

Locating follows Codex's apply_patch: a forward-only cursor, `@@` anchors
found in turn, and the first match after an anchor. The already-applied
cases were reproduced against v0.5.0, which reported success for patches
that wrote nothing. Every test asserts the file bytes as well as the result,
because the failures being guarded against were silent.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.patching import PatchHunk, apply_update_hunks_detailed, parse_patch
from coding_tools_mcp.server import Runtime


def envelope(body: str) -> str:
    """Wrap a patch body in the required Begin Patch and End Patch markers."""
    return "*** Begin Patch\n" + body + "*** End Patch"


TWO_CLASSES = (
    "class A:\n"
    "    def run(self):\n"
    "        return 1\n"
    "\n"
    "class B:\n"
    "    def run(self):\n"
    "        return 1\n"
)

DUPLICATE_BODY = (
    "def greet(name):\n"
    '    print("hello")\n'
    "    return None\n"
    "\n"
    "\n"
    "def farewell(name):\n"
    '    print("hello")\n'
    "    return None\n"
)


class RuntimePatchCase(unittest.TestCase):
    def setUp(self) -> None:
        """Create a temporary trusted runtime with telemetry off and register fixture cleanup."""
        env = patch.dict(os.environ, {"CODING_TOOLS_MCP_TELEMETRY": "off"})
        env.start()
        self.addCleanup(env.stop)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = Runtime(self.root, permission_mode="trusted")
        self.addCleanup(self.runtime.close)

    def write(self, name: str, body: str) -> None:
        """Write a UTF-8 fixture file in the test workspace."""
        (self.root / name).write_text(body, encoding="utf-8")

    def read(self, name: str) -> str:
        """Read a UTF-8 fixture file from the test workspace."""
        return (self.root / name).read_text(encoding="utf-8")

    def patch(self, body: str) -> dict[str, Any]:
        """Apply an enveloped patch body and return its structured result."""
        result = self.runtime.call_tool("apply_patch", {"patch": envelope(body)})
        return result["structuredContent"]

    def assert_error(self, result: dict[str, Any], code: str) -> None:
        """Assert that a structured patch result failed with the expected error code."""
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["error"]["code"], code, result)


class AlreadyAppliedEvidenceTests(RuntimePatchCase):
    INI = "[server]\ntimeout = 10\n\n[client]\ntimeout = 30\n"

    def test_blank_context_line_is_not_evidence_of_an_applied_edit(self) -> None:
        """Verify a blank context line cannot justify treating a mismatched edit as already applied."""
        # The stray blank context line before `*** End Patch` used to locate
        # `timeout = 30` in the [client] section and report success.
        self.write("app.ini", self.INI)
        result = self.patch(
            "*** Update File: app.ini\n@@ [server]\n-timeout = 20\n+timeout = 30\n \n"
        )
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(self.read("app.ini"), self.INI)

    def test_punctuation_context_is_not_evidence(self) -> None:
        """Verify punctuation-only context cannot establish that a replacement already landed."""
        original = "def a():\n    x = 1\n}\n"
        self.write("a.js", original)
        result = self.patch("*** Update File: a.js\n@@\n-    y = 0\n+    x = 1\n }\n")
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(self.read("a.js"), original)

    def test_a_false_already_applied_hunk_fails_the_whole_patch(self) -> None:
        """Verify an unsupported already-applied claim prevents every hunk from being committed."""
        # Hunk 0 applies; hunk 1's only located evidence is a lone added line
        # far below its anchor. Nothing may be written, not even hunk 0.
        original = "[server]\nname = a\ntimeout = 10\n\n[client]\ntimeout = 30\n"
        self.write("app.ini", original)
        result = self.patch(
            "*** Update File: app.ini\n"
            "@@ [server]\n"
            "-name = a\n"
            "+name = b\n"
            "@@ [server]\n"
            "-timeout = 20\n"
            "+timeout = 30\n"
            " \n"
        )
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(self.read("app.ini"), original)

    def test_already_applied_hunk_with_real_context_is_skipped_and_not_counted(self) -> None:
        """Verify real unchanged context permits skipping an applied hunk with zero line counts."""
        original = "def run():\n    value = 2\n"
        self.write("app.py", original)
        result = self.patch("*** Update File: app.py\n@@\n def run():\n-    value = 1\n+    value = 2\n")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["already_applied"])
        self.assertEqual((result["additions"], result["removals"]), (0, 0))
        self.assertEqual(self.read("app.py"), original)

    def test_mixed_applied_and_skipped_hunks_count_only_the_applied_one(self) -> None:
        """Verify skipped hunks contribute no line counts when other hunks still apply."""
        self.write("app.py", "def a():\n    x = 1\n\ndef b():\n    y = 2\n")
        result = self.patch(
            "*** Update File: app.py\n"
            "@@\n def a():\n-    x = 1\n+    x = 5\n"
            "@@\n def b():\n-    y = 1\n+    y = 2\n"
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["already_applied"])
        self.assertEqual((result["additions"], result["removals"]), (1, 1))
        self.assertEqual(self.read("app.py"), "def a():\n    x = 5\n\ndef b():\n    y = 2\n")

    def test_pure_deletion_already_applied_needs_real_context(self) -> None:
        """Verify recognizing an already-applied deletion requires substantive remaining context."""
        self.write("app.py", "def f():\n    return x\n")
        result = self.patch("*** Update File: app.py\n@@\n def f():\n-    x = 1\n     return x\n")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["already_applied"])
        self.assertEqual(self.read("app.py"), "def f():\n    return x\n")

        self.write("b.py", "a = 1\n}\n")
        result = self.patch("*** Update File: b.py\n@@\n-gone = 1\n }\n")
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(self.read("b.py"), "a = 1\n}\n")


class ReplaySafetyTests(RuntimePatchCase):
    def test_addition_next_to_context_reapplies_like_codex(self) -> None:
        """Verify resending an addition beside matching context inserts the added text again."""
        # Codex has no replay detection: the context still matches, so a
        # resend applies again. Only idempotency_key makes it replay-safe.
        self.write("i.py", "import os\n\nprint(1)\n")
        body = "*** Update File: i.py\n@@\n import os\n+import sys\n"
        first = self.patch(body)
        self.assertTrue(first["ok"], first)
        self.assertEqual((first["additions"], first["removals"]), (1, 0))
        second = self.patch(body)
        self.assertTrue(second["ok"], second)
        self.assertFalse(second["already_applied"])
        self.assertEqual(self.read("i.py"), "import os\nimport sys\nimport sys\n\nprint(1)\n")

    def test_idempotency_key_makes_a_resend_safe(self) -> None:
        """Verify an idempotency key prevents a repeated patch from inserting duplicate content."""
        self.write("i.py", "import os\n")
        args = {"patch": envelope("*** Update File: i.py\n@@\n import os\n+import sys\n"), "idempotency_key": "k1"}
        self.runtime.call_tool("apply_patch", args)
        replay = self.runtime.call_tool("apply_patch", args)["structuredContent"]
        self.assertTrue(replay.get("idempotent_replay"), replay)
        self.assertEqual(self.read("i.py"), "import os\nimport sys\n")

    def test_context_free_pure_addition_is_not_replay_safe(self) -> None:
        """Verify context-free additions repeat when no idempotency key protects the call."""
        self.write("i.py", "import os\n")
        body = "*** Update File: i.py\n@@\n+import sys\n"
        self.patch(body)
        self.patch(body)
        self.assertEqual(self.read("i.py"), "import os\nimport sys\nimport sys\n")


class CodexLocatingTests(RuntimePatchCase):
    def test_anchor_takes_the_first_match_after_it(self) -> None:
        """Verify matching starts after the anchor even when a later body repeats its text."""
        # Codex: the anchor `def run(self):` is found in A, the cursor moves
        # past it, and the body's first match after that is B's.
        self.write("m.py", TWO_CLASSES)
        result = self.patch(
            "*** Update File: m.py\n@@     def run(self):\n     def run(self):\n-        return 1\n+        return 2\n"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("m.py"), TWO_CLASSES[: TWO_CLASSES.rindex("1")] + "2\n")

    def test_anchor_selects_an_earlier_identical_body(self) -> None:
        """Verify an anchor disambiguates the earlier of two identical function bodies."""
        # v0.5.0 reported AMBIGUOUS here and its hint said to add the @@ the
        # patch already had.
        self.write("app.py", DUPLICATE_BODY)
        result = self.patch('*** Update File: app.py\n@@ def greet(name):\n-    print("hello")\n+    print("hi")\n')
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("app.py"), DUPLICATE_BODY.replace('print("hello")', 'print("hi")', 1))

    def test_anchor_selects_a_later_identical_body(self) -> None:
        """Verify a later anchor selects the later identical function body."""
        self.write("app.py", DUPLICATE_BODY)
        result = self.patch('*** Update File: app.py\n@@ def farewell(name):\n-    print("hello")\n+    print("bye")\n')
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.read("app.py"),
            DUPLICATE_BODY[: DUPLICATE_BODY.rindex('print("hello")')] + 'print("bye")\n    return None\n',
        )

    def test_partial_anchor_is_not_found(self) -> None:
        """Verify partial-line anchors fail without modifying the file."""
        # Codex matches the anchor as a whole line (whitespace-tolerant), not
        # as a prefix.
        self.write("app.py", DUPLICATE_BODY)
        result = self.patch('*** Update File: app.py\n@@ def greet\n-    print("hello")\n+    print("hi")\n')
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(self.read("app.py"), DUPLICATE_BODY)

    def test_unanchored_hunk_matching_twice_after_the_cursor_is_ambiguous(self) -> None:
        """Verify multiple unanchored matches after the cursor are rejected as ambiguous."""
        self.write("m.py", TWO_CLASSES)
        result = self.patch("*** Update File: m.py\n@@\n-        return 1\n+        return 2\n")
        self.assert_error(result, "PATCH_CONTEXT_AMBIGUOUS")
        self.assertEqual(self.read("m.py"), TWO_CLASSES)

    def test_hunks_are_located_in_file_order(self) -> None:
        """Verify a later hunk cannot search backward before the preceding hunk."""
        original = "def a():\n    x = 1\n\ndef b():\n    y = 1\n"
        self.write("f.py", original)
        result = self.patch(
            "*** Update File: f.py\n@@\n def b():\n-    y = 1\n+    y = 2\n@@\n-    x = 1\n+    x = 10\n"
        )
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(self.read("f.py"), original)

    def test_nested_anchors_all_have_to_be_found(self) -> None:
        """Verify every nested anchor must exist before any file mutation can occur."""
        # v0.5.0 kept only the last @@ line, so this applied.
        self.write("n.py", TWO_CLASSES)
        result = self.patch(
            "*** Update File: n.py\n@@ class DoesNotExist:\n@@ class B:\n-        return 1\n+        return 2\n"
        )
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(result["error"]["details"]["scope"], "class DoesNotExist:")
        self.assertEqual(self.read("n.py"), TWO_CLASSES)

    def test_nested_anchors_narrow_in_sequence(self) -> None:
        """Verify successive anchors advance the search into the intended nested scope."""
        self.write("n.py", TWO_CLASSES)
        result = self.patch(
            "*** Update File: n.py\n@@ class B:\n@@     def run(self):\n-        return 1\n+        return 2\n"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("n.py"), TWO_CLASSES[: TWO_CLASSES.rindex("1")] + "2\n")

    def test_nested_anchor_missing_after_the_outer_one_fails(self) -> None:
        """Verify nested anchors cannot match text before their outer anchor."""
        self.write("n.py", TWO_CLASSES)
        result = self.patch(
            "*** Update File: n.py\n@@ class B:\n@@ class A:\n-        return 1\n+        return 2\n"
        )
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(self.read("n.py"), TWO_CLASSES)

    def test_end_of_file_hunk_must_match_at_eof(self) -> None:
        """Verify an EOF-marked hunk fails when its old text occurs only before the file end."""
        original = "beta\nalpha\n"
        self.write("t.txt", original)
        result = self.patch("*** Update File: t.txt\n@@\n-beta\n+omega\n*** End of File\n")
        self.assert_error(result, "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(self.read("t.txt"), original)

    def test_end_of_file_picks_the_tail_copy(self) -> None:
        """Verify an EOF anchor selects the final copy of otherwise repeated text."""
        self.write("t.txt", "x\nmid\nx\n")
        result = self.patch("*** Update File: t.txt\n@@\n-x\n+y\n*** End of File\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("t.txt"), "x\nmid\ny\n")

    def test_duplicate_update_blocks_for_one_file_are_rejected(self) -> None:
        """Verify duplicate primary update paths reject the entire patch without writes."""
        # Codex primary-path contract; see migration-0.5.md.
        self.write("d.py", "one\ntwo\n")
        result = self.patch("*** Update File: d.py\n@@\n-one\n+ONE\n*** Update File: d.py\n@@\n-two\n+TWO\n")
        self.assert_error(result, "PATCH_FAILED")
        self.assertEqual(self.read("d.py"), "one\ntwo\n")


class ParserAndLocatorUnitTests(unittest.TestCase):
    def test_consecutive_headers_are_kept_as_nested_scopes(self) -> None:
        """Verify consecutive anchor headers form nested scopes that reset for the next hunk."""
        operations = parse_patch(envelope("*** Update File: a.py\n@@ class B:\n@@ def run(self):\n-x\n+y\n@@ other\n-p\n+q\n"))
        first, second = operations[0].hunks
        self.assertEqual(first.outer_scopes, ("class B:",))
        self.assertEqual(first.scope, "def run(self):")
        self.assertEqual(second.outer_scopes, ())
        self.assertEqual(second.scope, "other")

    def test_first_match_after_an_anchor_wins(self) -> None:
        """Verify an anchored hunk selects the first matching body after its anchor."""
        outcome = apply_update_hunks_detailed(
            "x = 1\nanchor\nx = 1\nx = 1\n", [PatchHunk(["-x = 1", "+x = 2"], "anchor")]
        )
        self.assertEqual(outcome.content, "x = 1\nanchor\nx = 2\nx = 1\n")

    def test_unanchored_ambiguity_after_the_cursor_is_reported(self) -> None:
        """Verify an unanchored duplicate body raises PATCH_CONTEXT_AMBIGUOUS."""
        with self.assertRaises(ToolFailure) as raised:
            apply_update_hunks_detailed("y\nx = 1\nx = 1\n", [["-x = 1", "+x = 2"]])
        self.assertEqual(raised.exception.code, "PATCH_CONTEXT_AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
