from __future__ import annotations

import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from urllib.parse import parse_qs, urlparse

from scripts.check_final_audit import select_successful_run, workflow_runs_url
from scripts.check_release_versions import validate_release


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def _write_release_tree(
        self,
        root: Path,
        *,
        project_version: str = "0.2.0",
        module_version: str = "0.2.0",
        npm_version: str = "0.1.0",
        changelog: str = "# Changelog\n\n## 0.2.0 - 2026-07-24\n",
    ) -> None:
        (root / "coding_tools_mcp").mkdir(parents=True)
        (root / "npm" / "coding-tools-mcp").mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nversion = "{project_version}"\n', encoding="utf-8"
        )
        (root / "coding_tools_mcp" / "__init__.py").write_text(
            f'__version__ = "{module_version}"\n', encoding="utf-8"
        )
        (root / "npm" / "coding-tools-mcp" / "package.json").write_text(
            json.dumps({"version": npm_version}), encoding="utf-8"
        )
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    def test_release_metadata_accepts_matching_stable_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(root)
            self.assertEqual(validate_release(root, "v0.2.0"), ("0.2.0", "0.1.0"))

    def test_release_metadata_accepts_matching_development_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(
                root,
                project_version="0.3.0.dev0",
                module_version="0.3.0.dev0",
                npm_version="0.3.0-dev.0",
                changelog="# Changelog\n\n## 0.3.0.dev0 - 2026-07-31\n",
            )
            self.assertEqual(
                validate_release(root, "v0.3.0.dev0"),
                ("0.3.0.dev0", "0.3.0-dev.0"),
            )

    def test_release_metadata_rejects_mismatched_development_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(
                root,
                project_version="0.3.0.dev0",
                module_version="0.3.0.dev0",
                npm_version="0.3.0-dev.1",
                changelog="# Changelog\n\n## 0.3.0.dev0 - 2026-07-31\n",
            )
            with self.assertRaisesRegex(SystemExit, "requires npm launcher"):
                validate_release(root, "v0.3.0.dev0")

    def test_release_metadata_rejects_unreleased_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(
                root,
                changelog="# Changelog\n\n## Unreleased\n\n## 0.2.0 - 2026-07-24\n",
            )
            with self.assertRaisesRegex(SystemExit, "Unreleased"):
                validate_release(root, "v0.2.0")

    def test_release_metadata_rejects_prerelease_npm_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(root, npm_version="0.1.0-beta.1")
            with self.assertRaisesRegex(SystemExit, "not stable"):
                validate_release(root, "v0.2.0")

    def test_current_integration_tree_is_versioned_development_candidate(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_init = (ROOT / "coding_tools_mcp" / "__init__.py").read_text(encoding="utf-8")
        npm_package = json.loads(
            (ROOT / "npm" / "coding-tools-mcp" / "package.json").read_text(encoding="utf-8")
        )
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertEqual(pyproject["project"]["version"], "0.3.0.dev0")
        self.assertIn('__version__ = "0.3.0.dev0"', package_init)
        self.assertEqual(npm_package["version"], "0.3.0-dev.0")
        self.assertIn("## 0.3.0.dev0 - 2026-07-31", changelog)
        self.assertNotIn("## Unreleased", changelog)
        self.assertEqual(
            validate_release(ROOT, "v0.3.0.dev0"),
            ("0.3.0.dev0", "0.3.0-dev.0"),
        )


class FinalAuditTests(unittest.TestCase):
    def test_workflow_runs_url_filters_by_release_sha(self) -> None:
        url = workflow_runs_url(
            "https://api.github.com", "xyTom/coding-tools-mcp", "final-audit.yml", "abc123"
        )
        parsed = urlparse(url)
        self.assertEqual(
            parsed.path,
            "/repos/xyTom/coding-tools-mcp/actions/workflows/final-audit.yml/runs",
        )
        self.assertEqual(
            parse_qs(parsed.query),
            {"status": ["success"], "head_sha": ["abc123"], "per_page": ["100"]},
        )

    def test_selects_latest_successful_run_for_release_sha(self) -> None:
        runs = [
            {"id": 1, "head_sha": "abc", "status": "completed", "conclusion": "success"},
            {"id": 3, "head_sha": "abc", "status": "completed", "conclusion": "success"},
            {"id": 4, "head_sha": "def", "status": "completed", "conclusion": "success"},
        ]
        selected = select_successful_run(runs, "abc")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["id"], 3)

    def test_rejects_failed_or_incomplete_runs(self) -> None:
        runs = [
            {"id": 1, "head_sha": "abc", "status": "completed", "conclusion": "failure"},
            {"id": 2, "head_sha": "abc", "status": "in_progress", "conclusion": None},
        ]
        self.assertIsNone(select_successful_run(runs, "abc"))


if __name__ == "__main__":
    unittest.main()
