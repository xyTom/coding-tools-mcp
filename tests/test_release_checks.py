from __future__ import annotations

import json
import io
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
import zipfile

from scripts.check_release_versions import validate_distributions, validate_release


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

    def test_release_metadata_rejects_a_stale_checked_in_uv_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(root)
            (root / "uv.lock").write_text(
                """version = 1

[[package]]
name = "coding-tools-mcp"
version = "0.1.0"
source = { editable = "." }
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "uv.lock project version"):
                validate_release(root, "v0.2.0")

    def test_release_metadata_rejects_uv_lock_dev_dependency_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(root)
            (root / "pyproject.toml").write_text(
                """[project]
version = "0.2.0"

[project.optional-dependencies]
dev = ["mcp>=2.0", "PyYAML>=6.0"]
""",
                encoding="utf-8",
            )
            (root / "uv.lock").write_text(
                """version = 1

[[package]]
name = "coding-tools-mcp"
version = "0.2.0"
source = { editable = "." }

[package.optional-dependencies]
dev = [{ name = "mcp" }]
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "uv.lock dev dependencies"):
                validate_release(root, "v0.2.0")

    def test_release_metadata_normalizes_uv_lock_dev_dependency_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(root)
            (root / "pyproject.toml").write_text(
                """[project]
version = "0.2.0"

[project.optional-dependencies]
dev = ["typing_extensions>=4.0", "PyYAML>=6.0"]
""",
                encoding="utf-8",
            )
            (root / "uv.lock").write_text(
                """version = 1

[[package]]
name = "coding-tools-mcp"
version = "0.2.0"
source = { editable = "." }

[package.optional-dependencies]
dev = [{ name = "typing-extensions" }, { name = "pyyaml" }]
""",
                encoding="utf-8",
            )
            self.assertEqual(validate_release(root, "v0.2.0"), ("0.2.0", "0.1.0"))

    def test_release_metadata_rejects_removed_extra_in_uv_lock(self) -> None:
        """A removed desktop extra must fail even when dev dependencies still match."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_release_tree(root)
            (root / "uv.lock").write_text(
                '''version = 1
[[package]]
name = "coding-tools-mcp"
version = "0.2.0"
source = { editable = "." }
[package.optional-dependencies]
desktop = [{ name = "pyside6" }]
''',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "optional dependency groups"):
                validate_release(root, "v0.2.0")


class DistributionMetadataTests(unittest.TestCase):
    def _write_distributions(
        self, root: Path, *, wheel_extra: str = "", entry_extra: str = "",
        wheel_desktop: bool = False, sdist_desktop: bool = False,
    ) -> Path:
        """Create archive fixtures with independently readable package metadata."""
        manifest = '''[project]
name = "coding-tools-mcp"
version = "0.5.0"
dependencies = ["PyJWT>=2.8"]
[project.scripts]
coding-tools-mcp = "coding_tools_mcp.server:main"
'''
        (root / "pyproject.toml").write_text(manifest, encoding="utf-8")
        directory = root / "dist"
        directory.mkdir()
        metadata = "Metadata-Version: 2.4\nName: coding-tools-mcp\nVersion: 0.5.0\nRequires-Dist: PyJWT>=2.8\n"
        sources = {
            "coding_tools_mcp/__init__.py": b'__version__ = "0.5.0"\n',
            "coding_tools_mcp/__main__.py": b"",
            "coding_tools_mcp/server.py": b"",
        }
        with zipfile.ZipFile(directory / "coding_tools_mcp-0.5.0-py3-none-any.whl", "w") as wheel:
            for name, body in sources.items():
                wheel.writestr(name, body)
            wheel.writestr("coding_tools_mcp-0.5.0.dist-info/METADATA", metadata + wheel_extra)
            wheel.writestr(
                "coding_tools_mcp-0.5.0.dist-info/entry_points.txt",
                "[console_scripts]\ncoding-tools-mcp = coding_tools_mcp.server:main\n" + entry_extra,
            )
            if wheel_desktop:
                wheel.writestr("mcp_desktop_client/app.py", "")
        with tarfile.open(directory / "coding_tools_mcp-0.5.0.tar.gz", "w:gz") as sdist:
            files = {**sources, "pyproject.toml": manifest.encode(), "PKG-INFO": metadata.encode()}
            if sdist_desktop:
                files["apps/desktop-client/mcp_desktop_client/app.py"] = b""
            for name, body in files.items():
                member = tarfile.TarInfo("coding_tools_mcp-0.5.0/" + name)
                member.size = len(body)
                sdist.addfile(member, io.BytesIO(body))
        return directory

    def test_core_only_wheel_and_sdist_are_accepted_without_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self._write_distributions(root)
            self.assertEqual(validate_distributions(root, directory), "0.5.0")

    def test_removed_desktop_extra_or_dependency_is_rejected(self) -> None:
        for metadata, message in (
            ("Provides-Extra: desktop\n", "extras"),
            ("Requires-Dist: PySide6>=6.8\n", "dependencies"),
        ):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = self._write_distributions(root, wheel_extra=metadata)
                with self.assertRaisesRegex(SystemExit, message):
                    validate_distributions(root, directory)

    def test_removed_desktop_console_script_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self._write_distributions(
                root, entry_extra="coding-tools-mcp-desktop = mcp_desktop_client.app:main\n"
            )
            with self.assertRaisesRegex(SystemExit, "console scripts"):
                validate_distributions(root, directory)

    def test_desktop_code_is_rejected_in_either_distribution(self) -> None:
        for field in ("wheel_desktop", "sdist_desktop"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = self._write_distributions(root, **{field: True})
                with self.assertRaisesRegex(SystemExit, "desktop application files"):
                    validate_distributions(root, directory)


class RepositoryHygieneTests(unittest.TestCase):
    def test_cloudflare_local_secret_files_are_ignored_after_infra_move(self) -> None:
        root = Path(__file__).resolve().parents[1]
        paths = [
            "infra/cloudflare/sandbox-control/.dev.vars",
            "infra/cloudflare/sandbox-control/.dev.vars.local",
            "infra/cloudflare/sandbox-control/.env",
            "infra/cloudflare/sandbox-control/.env.local",
        ]
        for path in paths:
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", "--quiet", "--no-index", path],
                    cwd=root,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, f"{path} must be ignored")

if __name__ == "__main__":
    unittest.main()
