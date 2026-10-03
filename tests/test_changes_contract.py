"""The apply_changes / read_file contract a model actually has to follow.

Each class pins one way a well-behaved caller used to fail: a revision that
was refused or misapplied, a schema-following edit the handler rejected,
guidance that contradicted itself, or line endings an edit silently rewrote.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp.changes import apply_line_edits, normalize_revision, parse_changes
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.patching import content_revision
from coding_tools_mcp.server import Runtime


def apply_text(text: str, raw_edits: list[dict[str, Any]]) -> str:
    """Parse raw line edits and return the resulting text for an in-memory fixture."""
    change = parse_changes([{"action": "edit", "path": "f.txt", "revision": "r", "edits": raw_edits}])[0]
    return apply_line_edits(text, change.edits, "f.txt").content


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        """Create a trusted temporary workspace with sample text and telemetry disabled."""
        self._env = patch.dict(os.environ, {"CODING_TOOLS_MCP_TELEMETRY": "off"})
        self._env.start()
        self._tmp = TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        (self.workspace / "a.txt").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
        self.runtime = Runtime(self.workspace, permission_mode="trusted")

    def tearDown(self) -> None:
        """Close the runtime, remove the workspace, and restore the telemetry environment."""
        self.runtime.close()
        self._tmp.cleanup()
        self._env.stop()

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call through the fixture runtime and return its MCP result."""
        return self.runtime.call_tool(name, args)

    def text(self, result: dict[str, Any]) -> str:
        """Extract the final model-facing text block from a tool result."""
        return result["content"][-1]["text"]

    def revision(self, name: str = "a.txt") -> str:
        """Compute the revision of the fixture file without normalizing its line endings."""
        return content_revision((self.workspace / name).read_bytes().decode("utf-8"))

    def edit(self, revision: str, *edits: dict[str, Any], path: str = "a.txt", **extra: Any) -> dict[str, Any]:
        """Submit edits to one file with the supplied revision and optional call arguments."""
        return self.call(
            "apply_changes",
            {"changes": [{"action": "edit", "path": path, "revision": revision, "edits": list(edits)}], **extra},
        )


class GuidanceTests(unittest.TestCase):
    """Items 1, 3, 4, 5, 6: what the model is told."""

    def setUp(self) -> None:
        """Load the apply_changes description and schema fragments used by guidance checks."""
        self.description = server_module.TOOL_REGISTRY["apply_changes"].description
        self.schema = server_module.input_schemas()["apply_changes"]
        self.item = self.schema["properties"]["changes"]["items"]
        self.edit_item = self.item["properties"]["edits"]["items"]

    def test_revision_may_come_from_the_latest_write_result(self) -> None:
        """Verify revision guidance accepts the latest write result for the same file version."""
        revision_doc = self.item["properties"]["revision"]["description"]
        for text in (self.description, revision_doc):
            with self.subTest(text=text[:40]):
                self.assertIn("read_file", text)
                self.assertIn("latest apply_changes/apply_patch result", text)
                self.assertIn("same version of the file", text)

    def test_the_example_does_not_show_a_placeholder_that_looks_like_a_value(self) -> None:
        """Verify examples describe a real revision token instead of a copyable placeholder."""
        self.assertNotIn("<from read_file>", self.description)
        self.assertIn("64-hex", self.description)

    def test_several_edits_to_one_file_are_combined_everywhere(self) -> None:
        """Verify schema and tool guidance combine edits to one file into a single change."""
        changes_doc = self.schema["properties"]["changes"]["description"]
        self.assertNotIn("apply_patch to chain", changes_doc)
        self.assertIn("single edit change", changes_doc)
        self.assertIn("single edit change", self.description)

    def test_every_field_says_which_ops_use_it(self) -> None:
        """Verify field descriptions identify the operations that accept each field."""
        props = self.edit_item["properties"]
        self.assertIn("replace and delete", props["start_line"]["description"])
        self.assertIn("replace and delete", props["end_line"]["description"])
        self.assertIn("insert_after", props["line"]["description"])
        self.assertIn("shorthand", props["line"]["description"])
        self.assertIn("not accepted for delete", props["content"]["description"])
        self.assertIn("create and write", self.item["properties"]["content"]["description"])
        self.assertIn("move and copy", self.item["properties"]["destination"]["description"])

    def test_no_schema_combinators_are_introduced(self) -> None:
        """Verify the edit schema stays free of oneOf, anyOf, and allOf combinators."""
        rendered = repr(self.schema)
        for key in ("oneOf", "anyOf", "allOf"):
            self.assertNotIn(key, rendered)

    def test_line_numbers_is_documented_for_both_tools(self) -> None:
        """Verify optional line numbering is documented by both read and edit tools."""
        read_schema = server_module.input_schemas()["read_file"]["properties"]["line_numbers"]
        self.assertEqual(read_schema["type"], "boolean")
        self.assertIs(read_schema["default"], False)
        self.assertIn("line_numbers", server_module.TOOL_REGISTRY["read_file"].description)
        self.assertIn("line_numbers", self.description)


class RevisionChainingTests(RuntimeCase):
    """Items 1 and 2: read once, edit twice."""

    def test_the_revision_a_write_returns_chains_with_the_new_line_numbers(self) -> None:
        """Verify a write's revision can authorize a subsequent edit using updated line numbers."""
        r0 = self.call("read_file", {"path": "a.txt"})["structuredContent"]["revision"]
        first = self.edit(r0, {"op": "replace", "start_line": 2, "content": "X"})
        self.assertFalse(first["isError"])
        r1 = first["structuredContent"]["affected_files"][0]["revision"]
        self.assertIn(f"a.txt: revision={r1}", self.text(first))
        second = self.edit(r1, {"op": "replace", "start_line": 3, "content": "Y"})
        self.assertFalse(second["isError"], second)
        self.assertEqual((self.workspace / "a.txt").read_text(), "l1\nX\nY\nl4\n")

    def test_a_mismatch_never_hands_back_the_current_revision(self) -> None:
        """Verify revision mismatches preserve content and direct callers to reread the file."""
        r0 = self.revision()
        self.assertFalse(self.edit(r0, {"op": "replace", "start_line": 2, "content": "X"})["isError"])
        current = self.revision()
        stale = self.edit(r0, {"op": "replace", "start_line": 2, "content": "Z"})
        self.assertTrue(stale["isError"])
        error = stale["structuredContent"]["error"]
        self.assertEqual(error["code"], "REVISION_MISMATCH")
        self.assertNotIn(current, self.text(stale))
        self.assertNotIn(current, repr(error))
        self.assertIn("read_file", error["message"])
        self.assertIn("line numbers", error["message"])
        self.assertEqual(error["details"]["next_action"]["tool"], "read_file")
        self.assertEqual((self.workspace / "a.txt").read_text(), "l1\nX\nl3\nl4\n")

    def test_revision_required_still_names_the_current_revision(self) -> None:
        """Verify a missing revision error still identifies the current file revision."""
        result = self.call(
            "apply_changes",
            {"changes": [{"action": "edit", "path": "a.txt", "edits": [{"op": "delete", "start_line": 1}]}]},
        )
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "REVISION_REQUIRED")
        self.assertEqual(error["details"]["current_revision"], self.revision())


class RevisionFormatTests(RuntimeCase):
    """Item 3: a malformed token is a bad argument, not a stale file."""

    def test_harmless_spellings_are_normalized(self) -> None:
        """Verify revision parsing accepts case, algorithm prefixes, and surrounding whitespace."""
        good = self.revision()
        for spelling in (good.upper(), f"sha256:{good}", f"SHA256:{good.upper()}", f" {good} "):
            with self.subTest(spelling=spelling[:12]):
                self.assertEqual(normalize_revision(spelling, "changes[0]"), good)
        result = self.edit(f"sha256:{good.upper()}", {"op": "replace", "start_line": 1, "content": "L1"})
        self.assertFalse(result["isError"], result)
        self.assertEqual((self.workspace / "a.txt").read_text(), "L1\nl2\nl3\nl4\n")

    def test_malformed_tokens_are_invalid_arguments(self) -> None:
        """Verify malformed revisions are non-retryable argument errors and leave content intact."""
        good = self.revision()
        for token in ("<from read_file>", "<64-hex revision from read_file>", good[:12], good + "0", "z" * 64):
            with self.subTest(token=token[:20]):
                result = self.edit(token, {"op": "replace", "start_line": 1, "content": "L1"})
                error = result["structuredContent"]["error"]
                self.assertEqual(error["code"], "INVALID_ARGUMENT")
                self.assertIs(error["retryable"], False)
                self.assertIn(
                    "revision must be the 64-character hex value read_file or the last write returned",
                    error["message"],
                )
                self.assertNotIn("changed since", error["message"])
        self.assertEqual((self.workspace / "a.txt").read_text(), "l1\nl2\nl3\nl4\n")

    def test_a_malformed_revision_on_a_missing_path_is_also_an_invalid_argument(self) -> None:
        """Verify malformed revisions are rejected even when the write target does not exist."""
        result = self.call(
            "apply_changes",
            {"changes": [{"action": "write", "path": "new.txt", "revision": "abc", "content": "x"}]},
        )
        self.assertEqual(result["structuredContent"]["error"]["code"], "INVALID_ARGUMENT")

    def test_a_dry_run_labels_its_revision_as_not_written(self) -> None:
        """Verify dry-run edits label hypothetical revisions separately from committed revisions."""
        result = self.edit(self.revision(), {"op": "replace", "start_line": 1, "content": "L1"}, dry_run=True)
        self.assertFalse(result["isError"])
        text = self.text(result)
        would_be = result["structuredContent"]["affected_files"][0]["revision"]
        self.assertIn(f"would_be_revision={would_be}", text)
        self.assertNotIn(" revision=", text)
        applied = self.edit(self.revision(), {"op": "replace", "start_line": 1, "content": "L1"})
        self.assertIn(" revision=", self.text(applied))
        self.assertNotIn("would_be_revision", self.text(applied))

    def test_patch_dry_run_also_labels_its_revision_as_not_written(self) -> None:
        """Verify patch dry runs label hypothetical revisions and leave the file untouched."""
        result = self.call("apply_patch", {
            "patch": "*** Begin Patch\n*** Update File: a.txt\n@@\n-l1\n+L1\n*** End Patch\n",
            "dry_run": True,
        })
        self.assertFalse(result["isError"], result)
        self.assertIn("would_be_revision=", self.text(result))
        self.assertNotIn(" revision=", self.text(result))
        self.assertEqual((self.workspace / "a.txt").read_text(), "l1\nl2\nl3\nl4\n")


class ReadFileLineNumberTests(RuntimeCase):
    """Item 4."""

    def test_continuation_respects_the_requested_range(self) -> None:
        """Keep bounded reads inside their original end_line or max_lines range."""
        for bounds in ({"end_line": 2}, {"max_lines": 2}):
            with self.subTest(bounds=bounds):
                first = self.call("read_file", {"path": "a.txt", "max_bytes": 3, **bounds})
                action = first["structuredContent"]["next_action"]
                second = self.call(action["tool"], action["arguments"])["structuredContent"]
                self.assertEqual(second["content"], "l2\n")
                self.assertFalse(second["truncated"])
                self.assertIsNone(second["next_start_line"])
                self.assertNotIn("next_action", second)

    def test_partial_last_requested_line_has_no_out_of_range_continuation(self) -> None:
        """An oversized bounded line asks for a larger budget, not a later line."""
        result = self.call("read_file", {"path": "a.txt", "max_bytes": 1, "end_line": 1})
        payload = result["structuredContent"]
        self.assertTrue(payload["truncated"])
        self.assertIsNone(payload["next_start_line"])
        self.assertNotIn("next_action", payload)

    def test_default_output_is_unchanged(self) -> None:
        """Verify ordinary reads retain unnumbered text and omit the line_numbers field."""
        result = self.call("read_file", {"path": "a.txt"})
        self.assertTrue(self.text(result).endswith("]\nl1\nl2\nl3\nl4\n"))
        self.assertNotIn("line_numbers", result["structuredContent"])

    def test_line_numbers_prefix_text_only(self) -> None:
        """Verify line numbering changes rendered text while preserving structured content."""
        result = self.call("read_file", {"path": "a.txt", "line_numbers": True})
        self.assertTrue(self.text(result).endswith("]\n1\tl1\n2\tl2\n3\tl3\n4\tl4\n"))
        self.assertEqual(result["structuredContent"]["content"], "l1\nl2\nl3\nl4\n")

    def test_numbers_follow_the_requested_range_and_line_endings(self) -> None:
        """Verify numbered reads preserve original line endings and the requested starting index."""
        (self.workspace / "m.txt").write_bytes(b"a\r\nb\rc\nd")
        result = self.call("read_file", {"path": "m.txt", "start_line": 2, "line_numbers": True})
        self.assertTrue(self.text(result).endswith("]\n2\tb\r3\tc\n4\td"))
        self.assertEqual(result["structuredContent"]["content"], "b\rc\nd")

    def test_the_continuation_keeps_line_numbers(self) -> None:
        """Verify truncated reads carry the line-numbering option into their continuation."""
        (self.workspace / "big.txt").write_text("".join(f"line {n}\n" for n in range(50)), encoding="utf-8")
        result = self.call("read_file", {"path": "big.txt", "max_bytes": 40, "line_numbers": True})
        payload = result["structuredContent"]
        self.assertTrue(payload["truncated"])
        self.assertIs(payload["next_action"]["arguments"]["line_numbers"], True)

    def test_byte_limited_pages_follow_physical_lines_without_overlap(self) -> None:
        for endings in (("\r",) * 4, ("\r\n",) * 4, ("\n",) * 4, ("\r\n", "\r", "\n", "")):
            with self.subTest(endings=endings):
                lines = [f"line{n}{ending}" for n, ending in enumerate(endings, 1)]
                (self.workspace / "paged.txt").write_bytes("".join(lines).encode())
                args: dict[str, Any] = {"path": "paged.txt", "max_bytes": 14, "line_numbers": True}
                seen: list[str] = []
                while True:
                    result = self.call("read_file", args)
                    self.assertFalse(result["isError"], result)
                    payload = result["structuredContent"]
                    start, end = payload["start_line"], payload["end_line"]
                    expected = lines[start - 1:end]
                    self.assertEqual(payload["content"], "".join(expected))
                    self.assertEqual(payload["output_lines"], len(expected))
                    self.assertLessEqual(payload["bytes_read"], 14)
                    self.assertTrue(self.text(result).endswith("".join(
                        f"{number}\t{line}" for number, line in enumerate(expected, start)
                    )))
                    seen.extend(expected)
                    if "next_action" not in payload:
                        break
                    args = payload["next_action"]["arguments"]
                    self.assertEqual(args["start_line"], end + 1)
                self.assertEqual(seen, lines)


class LenientEditFieldTests(unittest.TestCase):
    """Item 5: unambiguous schema-following spellings are accepted."""

    def test_line_is_shorthand_for_a_one_line_replace_or_delete(self) -> None:
        """Verify the line alias selects a single replacement or deletion unless a range is given."""
        self.assertEqual(apply_text("a\nb\nc\n", [{"op": "replace", "line": 2, "content": "B"}]), "a\nB\nc\n")
        self.assertEqual(apply_text("a\nb\nc\n", [{"op": "delete", "line": 2}]), "a\nc\n")
        self.assertEqual(
            apply_text("a\nb\nc\n", [{"op": "delete", "line": 2, "start_line": 2, "end_line": 3}]), "a\n"
        )

    def test_start_line_stands_in_for_line_on_insertions(self) -> None:
        """Verify insertions accept start_line when it identifies an unambiguous anchor."""
        self.assertEqual(apply_text("a\nb\n", [{"op": "insert_after", "start_line": 1, "content": "x"}]), "a\nx\nb\n")
        self.assertEqual(
            apply_text("a\nb\n", [{"op": "insert_before", "start_line": 1, "end_line": 1, "content": "x"}]),
            "x\na\nb\n",
        )
        self.assertEqual(
            apply_text("a\nb\n", [{"op": "insert_before", "line": 2, "start_line": 2, "content": "x"}]),
            "a\nx\nb\n",
        )

    def test_contradictory_combinations_stay_invalid(self) -> None:
        """Verify conflicting aliases and missing or forbidden edit fields remain invalid."""
        for raw in (
            {"op": "insert_after", "line": 1, "start_line": 2, "content": "x"},
            {"op": "insert_before", "line": 1, "end_line": 3, "content": "x"},
            {"op": "replace", "line": 1, "start_line": 2, "content": "x"},
            {"op": "replace", "line": 1, "end_line": 3, "content": "x"},
            {"op": "replace", "content": "x"},
            {"op": "insert_after", "content": "x"},
            {"op": "delete", "start_line": 1, "content": "x"},
            {"op": "replace", "start_line": 1},
        ):
            with self.subTest(raw=raw), self.assertRaises(ToolFailure) as raised:
                parse_changes([{"action": "edit", "path": "f", "revision": "r", "edits": [raw]}])
            self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
            self.assertIn("edits[0]", raised.exception.message)


class DuplicatePathTests(RuntimeCase):
    """Item 6."""

    def test_two_changes_naming_one_path_say_so(self) -> None:
        """Verify duplicate-path errors identify both changes and explain how to combine them."""
        rev = self.revision()
        result = self.call(
            "apply_changes",
            {
                "changes": [
                    {"action": "edit", "path": "a.txt", "revision": rev, "edits": [{"op": "delete", "line": 1}]},
                    {"action": "edit", "path": "a.txt", "revision": rev, "edits": [{"op": "delete", "line": 3}]},
                ]
            },
        )
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "INVALID_ARGUMENT")
        self.assertIn("changes[0] and changes[1] both name a.txt", error["message"])
        self.assertIn("single edit change", error["message"])


class CreateTests(RuntimeCase):
    """Item 7."""

    def test_create_with_identical_content_is_an_idempotent_success(self) -> None:
        """Verify identical creates succeed without changing timestamps or counting edits."""
        before = (self.workspace / "a.txt").stat().st_mtime_ns
        result = self.call("apply_changes", {"changes": [{"action": "create", "path": "a.txt", "content": "l1\nl2\nl3\nl4\n"}]})
        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]
        self.assertIs(payload["already_applied"], True)
        self.assertEqual(payload["affected_files"][0]["operation"], "unchanged")
        self.assertEqual((payload["additions"], payload["removals"]), (0, 0))
        self.assertEqual((self.workspace / "a.txt").stat().st_mtime_ns, before)

    def test_create_with_different_content_suggests_write_with_the_revision(self) -> None:
        """Verify conflicting creates preserve content and recommend a revision-checked write."""
        result = self.call("apply_changes", {"changes": [{"action": "create", "path": "a.txt", "content": "other\n"}]})
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "PATCH_FAILED")
        self.assertIn("different content", error["message"])
        self.assertIn('"write"', error["message"])
        self.assertIn("revision", error["message"])
        self.assertEqual((self.workspace / "a.txt").read_text(), "l1\nl2\nl3\nl4\n")

    def test_create_or_write_on_a_directory_does_not_suggest_write(self) -> None:
        """Verify directory targets receive file-path guidance for both create and write."""
        (self.workspace / "pkg").mkdir()
        for action in ("create", "write"):
            with self.subTest(action=action):
                result = self.call("apply_changes", {"changes": [{"action": action, "path": "pkg", "content": "x"}]})
                error = result["structuredContent"]["error"]
                self.assertEqual(error["code"], "PATCH_FAILED")
                self.assertIn("is a directory", error["message"])
                self.assertNotIn('Use action "write"', error["message"])
                self.assertNotIn("Cannot patch a directory", error["message"])


class LineEndingPreservationTests(unittest.TestCase):
    """Item 8: untouched lines keep their own terminator byte-for-byte."""

    def test_mixed_endings_survive_an_edit(self) -> None:
        """Verify replacements retain each line's original LF, CRLF, or CR terminator."""
        self.assertEqual(
            apply_text("a\r\nb\nc\rd\r\n", [{"op": "replace", "start_line": 1, "content": "A"}]),
            "A\r\nb\nc\rd\r\n",
        )
        self.assertEqual(
            apply_text("a\r\nb\nc\rd\r\n", [{"op": "replace", "start_line": 3, "content": "C"}]),
            "a\r\nb\nC\rd\r\n",
        )

    def test_bare_cr_survives_an_edit(self) -> None:
        """Verify editing a CR-delimited file preserves its bare carriage returns."""
        self.assertEqual(apply_text("a\rb\rc\r", [{"op": "replace", "start_line": 2, "content": "B"}]), "a\rB\rc\r")

    def test_new_lines_borrow_the_ending_of_their_neighbour(self) -> None:
        """Verify inserted and replacement lines inherit the applicable neighboring terminator."""
        # Replacement lines take the replaced line's terminator.
        self.assertEqual(
            apply_text("a\nb\r\nc\n", [{"op": "replace", "start_line": 2, "content": "x\ny"}]),
            "a\nx\r\ny\r\nc\n",
        )
        # An insertion takes the terminator of the line it is inserted before.
        self.assertEqual(
            apply_text("a\nb\r\nc\n", [{"op": "insert_before", "line": 2, "content": "x"}]),
            "a\nx\r\nb\r\nc\n",
        )
        # Appending takes the last line's terminator.
        self.assertEqual(
            apply_text("a\nb\r", [{"op": "insert_after", "line": 2, "content": "x"}]),
            "a\nb\rx\r",
        )

    def test_a_missing_final_newline_falls_back_to_the_dominant_ending(self) -> None:
        """Verify edits preserve a missing final newline and use the dominant internal ending."""
        self.assertEqual(
            apply_text("a\r\nb\r\nc", [{"op": "insert_after", "line": 3, "content": "d"}]),
            "a\r\nb\r\nc\r\nd",
        )
        self.assertEqual(
            apply_text("a\r\nb\r\nc", [{"op": "replace", "start_line": 3, "content": "x\ny"}]),
            "a\r\nb\r\nx\r\ny",
        )
        self.assertEqual(apply_text("a\nb", [{"op": "delete", "start_line": 2}]), "a")

    def test_bom_and_empty_files(self) -> None:
        """Verify edits preserve a BOM and insert sensible line endings into an empty file."""
        self.assertEqual(
            apply_text("﻿a\rb\r", [{"op": "replace", "start_line": 1, "content": "A"}]), "﻿A\rb\r"
        )
        self.assertEqual(apply_text("", [{"op": "insert_after", "line": 0, "content": "a\nb"}]), "a\nb\n")

    def test_runtime_writes_the_preserved_bytes(self) -> None:
        """Verify runtime commits preserve mixed line-ending bytes and return matching revisions."""
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODING_TOOLS_MCP_TELEMETRY": "off"}):
            path = Path(tmp) / "m.txt"
            path.write_bytes(b"a\r\nb\nc\rd\r\n")
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            try:
                revision = runtime.read_file({"path": "m.txt"})["revision"]
                result = runtime.call_tool(
                    "apply_changes",
                    {
                        "changes": [
                            {
                                "action": "edit",
                                "path": "m.txt",
                                "revision": revision,
                                "edits": [{"op": "replace", "start_line": 1, "content": "A"}],
                            }
                        ]
                    },
                )
                self.assertFalse(result["isError"], result)
                self.assertEqual(path.read_bytes(), b"A\r\nb\nc\rd\r\n")
                self.assertEqual(
                    result["structuredContent"]["affected_files"][0]["revision"],
                    runtime.read_file({"path": "m.txt"})["revision"],
                )
            finally:
                runtime.close()


class NoOpCountTests(RuntimeCase):
    """Item 9."""

    def test_a_no_op_replace_reports_zero_changes(self) -> None:
        """Verify identical replacements report already_applied with zero additions or removals."""
        result = self.edit(self.revision(), {"op": "replace", "start_line": 2, "content": "l2"})
        payload = result["structuredContent"]
        self.assertEqual((payload["additions"], payload["removals"]), (0, 0))
        self.assertIn("(+0 -0)", self.text(result))
        self.assertIs(payload["already_applied"], True)

    def test_counts_name_only_lines_that_differ(self) -> None:
        """Verify partially changed replacements count only the differing lines."""
        result = self.edit(self.revision(), {"op": "replace", "start_line": 1, "end_line": 3, "content": "l1\nX\nl3"})
        payload = result["structuredContent"]
        self.assertEqual((payload["additions"], payload["removals"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
