#!/usr/bin/env python3
"""Validate release tag, package versions, and release-note coverage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="Release tag, for example v0.2.0")
    args = parser.parse_args()

    project_version, npm_version = validate_release(ROOT, args.tag)

    print(
        f"Release metadata OK: Python {project_version} ({args.tag}), "
        f"npm launcher {npm_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
