#!/usr/bin/env python3
"""Select the immutable version-introducing main commit for release/recovery.

This script only reads Git. First publication and recovery intentionally use the
same first-parent version boundary, even when a push includes later commits.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SEALED_VERSION = (0, 5, 0)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def stable_version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError(f"expected a stable major.minor.patch version, got {value!r}")
    return tuple(map(int, value.split(".")))  # type: ignore[return-value]


def version_at(root: Path, sha: str) -> str:
    if not git(root, "ls-tree", "--name-only", sha, "--", "pyproject.toml"):
        return ""  # Initial repository history may predate Python packaging.
    return str(tomllib.loads(git(root, "show", f"{sha}:pyproject.toml"))["project"]["version"])


def select_release(root: Path, head: str, before: str = "", requested: str = "") -> dict[str, str]:
    """Fail closed on ambiguous histories, non-main revisions, and sealed releases."""
    head = git(root, "rev-parse", "--verify", f"{head}^{{commit}}")
    history = git(root, "rev-list", "--first-parent", head).splitlines()
    versions: dict[str, str] = {}

    def at(sha: str) -> str:
        if sha not in versions:
            versions[sha] = version_at(root, sha)
        return versions[sha]

    current_version = at(head)
    if requested:
        version = requested.removeprefix("v")
        if stable_version(version) <= SEALED_VERSION:
            raise ValueError("v0.5.0 and all earlier releases are sealed")
    else:
        if not re.fullmatch(r"[0-9a-f]{40}", before) or before == "0" * 40:
            raise ValueError("automatic release requires a non-initial main push")
        if before not in history:
            raise ValueError("before is not a first-parent ancestor; force pushes are not release inputs")
        pushed_versions = {at(sha) for sha in history[:history.index(before)]}
        if at(before) == current_version:
            if pushed_versions - {current_version}:
                raise ValueError("multiple version changes in one push; recover each version explicitly")
            return {"publish": "false"}
        version = current_version
        if stable_version(version) <= SEALED_VERSION:
            raise ValueError("v0.5.0 and all earlier releases are sealed")
        if stable_version(at(before)) >= stable_version(version):
            raise ValueError("release version must increase")
        # Do not silently drop an intermediate version from a batched push.
        changed_versions = pushed_versions - {at(before)}
        if changed_versions != {version}:
            raise ValueError("multiple version changes in one push; recover each version explicitly")

    # Follow the complete first-parent history so a later change/revert cannot
    # silently choose a different commit for an already partially published version.
    boundaries: list[str] = []
    for index, sha in enumerate(history):
        value = at(sha)
        if value != version:
            continue
        parent = history[index + 1] if index + 1 < len(history) else None
        if parent is None or at(parent) != version:
            boundaries.append(sha)
    if len(boundaries) != 1:
        raise ValueError(f"version {version} has {len(boundaries)} first-parent boundaries; refusing ambiguity")
    source = boundaries[0]
    previous = git(root, "rev-parse", f"{source}^1")
    if stable_version(at(previous)) >= stable_version(version):
        raise ValueError("selected version boundary does not increase the version")
    npm = json.loads(git(root, "show", f"{source}:packages/npm-launcher/package.json"))["version"]
    stable_version(npm)
    return {
        "publish": "true", "source_sha": source, "controller_sha": head, "version": version,
        "tag": f"v{version}", "npm_version": npm,
        "source_date_epoch": git(root, "show", "-s", "--format=%ct", source),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--before", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = select_release(args.root, args.head, args.before, args.version)
    print(json.dumps(result, sort_keys=True))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
