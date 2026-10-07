#!/usr/bin/env python3
"""Validate release tag, package versions, and release-note coverage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _canonical_requirement_name(requirement: str) -> str:
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    if not match:
        raise SystemExit(f"could not parse requirement name from {requirement!r}")
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def validate_release(root: Path, tag: str) -> tuple[str, str]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", project_version):
        raise SystemExit(f"Python version {project_version!r} is not stable major.minor.patch")

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
        expected_dev = {
            _canonical_requirement_name(requirement)
            for requirement in pyproject.get("project", {})
            .get("optional-dependencies", {})
            .get("dev", [])
        }
        locked_dev = {
            re.sub(r"[-_.]+", "-", str(item.get("name", ""))).lower()
            for item in locked_project.get("optional-dependencies", {}).get("dev", [])
        }
        if locked_dev != expected_dev:
            raise SystemExit(
                "uv.lock dev dependencies do not match pyproject.toml: "
                f"expected {sorted(expected_dev)!r}, got {sorted(locked_dev)!r}"
            )

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(project_version)} - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE):
        raise SystemExit(f"CHANGELOG.md has no dated {project_version} release heading")
    if re.search(r"^## Unreleased\s*$", changelog, re.MULTILINE):
        raise SystemExit("CHANGELOG.md still contains an Unreleased section")

    npm_package = json.loads((root / "packages" / "npm-launcher" / "package.json").read_text(encoding="utf-8"))
    npm_version = npm_package["version"]
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", npm_version):
        raise SystemExit(f"npm launcher version {npm_version!r} is not stable")

    return project_version, npm_version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--tag", required=True, help="Release tag, for example v0.2.0")
    parser.add_argument("--changed-from", help="Skip release-only checks when the Python version is unchanged")
    args = parser.parse_args()

    if args.changed_from:
        before = tomllib.loads(subprocess.check_output(
            ["git", "-C", str(args.root), "show", f"{args.changed_from}:pyproject.toml"], text=True,
        ))["project"]["version"]
        current = tomllib.loads((args.root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        if before == current:
            print("No Python version change; release-only metadata checks are deferred")
            return 0
        def numeric_version(value: str) -> tuple[int, ...]:
            if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
                raise SystemExit(f"version {value!r} is not stable major.minor.patch")
            return tuple(map(int, value.split(".")))
        if numeric_version(current) <= numeric_version(before):
            raise SystemExit("release version must increase")

    project_version, npm_version = validate_release(args.root, args.tag)

    print(
        f"Release metadata OK: Python {project_version} ({args.tag}), "
        f"npm launcher {npm_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
