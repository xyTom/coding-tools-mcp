from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp.server import Runtime, _matches_globstar_segments, matches_any_glob


class GlobstarMatchingTests(unittest.TestCase):
    """`**/` matches zero or more directories, as in fd, rg and gitignore."""

    def test_many_globstar_segments_do_not_expand_exponentially(self) -> None:
        for count in (3, 6, 10, 24):
            with self.subTest(count=count):
                pattern = "/".join(f"**/dir{i}" for i in range(count)) + "/file.py"
                path = "/".join(f"dir{i}" for i in range(count)) + "/file.py"
                self.assertTrue(_matches_globstar_segments(path, pattern))
                self.assertFalse(_matches_globstar_segments(path + ".txt", pattern))

    def test_globstar_matches_zero_or_more_directories(self) -> None:
        cases = [
            ("src/m.py", "src/**/*.py", True),
            ("src/deep/er/d.py", "src/**/*.py", True),
            ("root.py", "**/*.py", True),
            ("src/m.py", "**/*.py", True),
            ("a/x.py", "**/a/**/*.py", True),
            ("src/file.py", "**/src/**", True),
            ("src/deep/file.py", "**/src/**", True),
            ("prefix/src/deep/file.py", "**/src/**", True),
            ("other/deep/file.py", "**/src/**", False),
            ("src", "**/src/**", False),
            ("nested/README.md", "**/README.md/**", False),
            ("src/m.txt", "src/**/*.py", False),
            ("other/m.py", "src/**/*.py", False),
            ("x.py", "**/a/**/*.py", False),
        ]
        for rel, pattern, expected in cases:
            with self.subTest(rel=rel, pattern=pattern):
                self.assertIs(matches_any_glob(rel, [pattern]), expected)

    def test_python_fallback_list_and_search_include_direct_children(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "deep").mkdir(parents=True)
            for rel in ("root.py", "src/m.py", "src/deep/d.py"):
                (root / rel).write_text("def f():\n    pass\n", encoding="utf-8")
            runtime = Runtime(root)
            runtime.workspace.git_path = None
            try:
                # No fd/rg: exercise the Python walker these patterns go through.
                with patch.object(server_module, "cached_which", return_value=None):
                    listed = runtime.list_files({"glob": "src/**/*.py"})
                    everything = runtime.list_files({"glob": "**/*.py"})
                    found = runtime.search_text({"query": "def f", "include_globs": ["src/**/*.py"]})
            finally:
                runtime.close()
        self.assertEqual([f["path"] for f in listed["files"]], ["src/deep/d.py", "src/m.py"])
        self.assertEqual(
            [f["path"] for f in everything["files"]], ["root.py", "src/deep/d.py", "src/m.py"]
        )
        self.assertEqual(found["total_matches"], 2)

    def test_python_fallback_excludes_descendants_but_keeps_named_regular_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("src", "nested/README.md", "nested/src/a.py", "nested/src/deep/b.py"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def f():\n    pass\n", encoding="utf-8")
            runtime = Runtime(root)
            runtime.workspace.git_path = None
            exclusions = ["**/src/**", "**/README.md/**"]
            try:
                with patch.object(server_module, "cached_which", return_value=None):
                    listed = runtime.list_files({"exclude_patterns": exclusions})
                    found = runtime.search_text({"query": "def f", "exclude_globs": exclusions})
            finally:
                runtime.close()
        expected = ["nested/README.md", "src"]
        self.assertEqual([item["path"] for item in listed["files"]], expected)
        self.assertEqual(sorted(item["path"] for item in found["matches"]), expected)
        self.assertEqual(found["total_matches"], len(expected))

    @unittest.skipUnless(shutil.which("rg"), "ripgrep not installed")
    def test_trailing_globstar_agrees_with_ripgrep(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = ["src", "nested/README.md", "nested/src/a.py", "nested/src/deep/b.py"]
            for rel in files:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def f():\n    pass\n", encoding="utf-8")
            for pattern in ("**/src/**", "**/README.md/**"):
                with self.subTest(pattern=pattern):
                    result = subprocess.run(
                        ["rg", "--no-config", "--files", "--hidden", "--no-ignore", "--glob", pattern],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        check=False,
                    )
                    self.assertIn(result.returncode, (0, 1), result.stderr)
                    expected = sorted(Path(path).as_posix() for path in result.stdout.splitlines())
                    actual = sorted(rel for rel in files if matches_any_glob(rel, [pattern]))
                    self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
