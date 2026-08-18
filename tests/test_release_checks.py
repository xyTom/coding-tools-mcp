from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.check_release_versions import validate_release


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
        (root / "packages" / "npm-launcher").mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nversion = "{project_version}"\n', encoding="utf-8"
        )
        (root / "coding_tools_mcp" / "__init__.py").write_text(
            f'__version__ = "{module_version}"\n', encoding="utf-8"
        )
        (root / "packages" / "npm-launcher" / "package.json").write_text(
            json.dumps({"version": npm_version}), encoding="utf-8"
        )
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    def test_release_metadata_accepts_matching_stable_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(root)
            self.assertEqual(validate_release(root, "v0.2.0"), ("0.2.0", "0.1.0"))

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

if __name__ == "__main__":
    unittest.main()
