#!/usr/bin/env python3
"""Validate release tag, package versions, and release-note coverage."""

from __future__ import annotations

import argparse
import configparser
from email.message import Message
from email.parser import BytesParser
import json
from pathlib import Path
import re
import tarfile
import tomllib
from typing import Any
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def _canonical_requirement_name(requirement: str) -> str:
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    if not match:
        raise SystemExit(f"could not parse requirement name from {requirement!r}")
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def validate_release(root: Path, tag: str) -> tuple[str, str]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]

    package_init = (root / "coding_tools_mcp" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', package_init, re.MULTILINE)
    if not match:
        raise SystemExit("coding_tools_mcp.__version__ was not found")
    module_version = match.group(1)

    expected_tag = f"v{project_version}"
    if tag != expected_tag:
        raise SystemExit(f"release tag {tag!r} does not match {expected_tag!r}")
    if module_version != project_version:
        raise SystemExit(
            f"pyproject version {project_version!r} does not match module version {module_version!r}"
        )

    lock_path = root / "uv.lock"
    if lock_path.exists():
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        project_packages = [
            package
            for package in lock.get("package", [])
            if package.get("name") == "coding-tools-mcp"
            and package.get("source", {}).get("editable") == "."
        ]
        if len(project_packages) != 1:
            raise SystemExit("uv.lock must contain exactly one editable coding-tools-mcp package")
        locked_project = project_packages[0]
        if locked_project.get("version") != project_version:
            raise SystemExit(
                f"uv.lock project version {locked_project.get('version')!r} "
                f"does not match pyproject version {project_version!r}"
            )
        expected_groups = pyproject.get("project", {}).get("optional-dependencies", {})
        locked_groups = locked_project.get("optional-dependencies", {})
        if set(locked_groups) != set(expected_groups):
            raise SystemExit(
                "uv.lock optional dependency groups do not match pyproject.toml: "
                f"expected {sorted(expected_groups)!r}, got {sorted(locked_groups)!r}"
            )
        for group, requirements in expected_groups.items():
            expected_dependencies = {_canonical_requirement_name(item) for item in requirements}
            locked_dependencies = {
                re.sub(r"[-_.]+", "-", str(item.get("name", ""))).lower()
                for item in locked_groups[group]
            }
            if locked_dependencies != expected_dependencies:
                raise SystemExit(
                    f"uv.lock {group} dependencies do not match pyproject.toml: "
                    f"expected {sorted(expected_dependencies)!r}, got {sorted(locked_dependencies)!r}"
                )
        declared_extras = locked_project.get("metadata", {}).get("provides-extras")
        if declared_extras is not None and set(declared_extras) != set(expected_groups):
            raise SystemExit("uv.lock provides-extras do not match pyproject.toml")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(project_version)} - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE):
        raise SystemExit(f"CHANGELOG.md has no dated {project_version} release heading")
    if re.search(r"^## Unreleased\s*$", changelog, re.MULTILINE):
        raise SystemExit("CHANGELOG.md still contains an Unreleased section")

    npm_package = json.loads((root / "packages" / "npm-launcher" / "package.json").read_text(encoding="utf-8"))
    npm_version = npm_package["version"]
    if re.search(r"(?:^|[-.])(alpha|beta|rc|dev|next)(?:[-.]|$)", npm_version, re.IGNORECASE):
        raise SystemExit(f"npm launcher version {npm_version!r} is not stable")

    return project_version, npm_version


def validate_distributions(root: Path, directory: Path) -> str:
    """Check built wheel/sdist boundaries without importing the source checkout."""

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit("expected exactly one wheel and one source distribution")
    required = {"coding_tools_mcp/__init__.py", "coding_tools_mcp/__main__.py", "coding_tools_mcp/server.py"}
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        if missing := required - names:
            raise SystemExit(f"wheel is missing core modules: {sorted(missing)}")
        _reject_desktop_paths(names, "wheel")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_names) != 1:
            raise SystemExit("wheel must have exactly one METADATA and entry_points.txt")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        _validate_package_metadata(metadata, project, "wheel")
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(entry_names[0]).decode("utf-8"))
        actual_scripts = dict(entries["console_scripts"]) if entries.has_section("console_scripts") else {}
        if actual_scripts != project.get("scripts", {}):
            raise SystemExit("wheel console scripts do not match pyproject.toml")
    with tarfile.open(sdists[0], "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers() if member.isfile()}
        roots = {name.split("/", 1)[0] for name in members}
        if len(roots) != 1:
            raise SystemExit("source distribution must have exactly one root directory")
        prefix = roots.pop() + "/"
        names = {name.removeprefix(prefix) for name in members}
        if missing := (required | {"pyproject.toml", "PKG-INFO"}) - names:
            raise SystemExit(f"source distribution is missing: {sorted(missing)}")
        _reject_desktop_paths(names, "source distribution")
        for filename in ("PKG-INFO", "pyproject.toml"):
            handle = archive.extractfile(members[prefix + filename])
            assert handle is not None
            with handle:
                content = handle.read()
            if filename == "PKG-INFO":
                _validate_package_metadata(BytesParser().parsebytes(content), project, "source distribution")
            elif tomllib.loads(content.decode("utf-8"))["project"] != project:
                raise SystemExit("source distribution project metadata differs from the checkout")
    return str(project["version"])


def _reject_desktop_paths(names: set[str], distribution: str) -> None:
    unexpected = sorted(
        name for name in names
        if name.startswith(("mcp_desktop_client/", "apps/desktop-client/"))
    )
    if unexpected:
        raise SystemExit(f"{distribution} still contains desktop application files: {unexpected}")


def _validate_package_metadata(metadata: Message, project: dict[str, Any], distribution: str) -> None:
    if metadata.get("Name") != project["name"] or metadata.get("Version") != project["version"]:
        raise SystemExit(f"{distribution} name/version does not match pyproject.toml")
    extras = set(metadata.get_all("Provides-Extra", []))
    if extras != set(project.get("optional-dependencies", {})):
        raise SystemExit(f"{distribution} extras do not match pyproject.toml")
    expected_requirements = {
        _canonical_requirement_name(item)
        for item in [
            *project.get("dependencies", []),
            *(item for group in project.get("optional-dependencies", {}).values() for item in group),
        ]
    }
    actual_requirements = {
        _canonical_requirement_name(item) for item in metadata.get_all("Requires-Dist", [])
    }
    if actual_requirements != expected_requirements:
        raise SystemExit(f"{distribution} dependencies do not match pyproject.toml")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Release tag, for example v0.2.0")
    parser.add_argument("--dist-dir", type=Path, help="Validate built wheel and sdist contents in this directory")
    args = parser.parse_args()
    if args.tag is None and args.dist_dir is None:
        parser.error("at least one of --tag or --dist-dir is required")
    if args.tag is not None:
        project_version, npm_version = validate_release(ROOT, args.tag)
        print(f"Release metadata OK: Python {project_version} ({args.tag}), npm launcher {npm_version}")
    if args.dist_dir is not None:
        project_version = validate_distributions(ROOT, args.dist_dir)
        print(f"Distribution contents OK: Python {project_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
