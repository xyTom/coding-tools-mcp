#!/usr/bin/env python3
"""Finalize a verified release without changing existing tags or releases."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from typing import Any
import urllib.error
import urllib.request

from scripts.release_plan import stable_version, SEALED_VERSION


def api(repository: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/{path}",
        data=None if payload is None else json.dumps(payload).encode(), headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and payload is None:
            return None
        raise


def inspect(repository: str, tag: str, source: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("source must be a full commit SHA")
    if not tag.startswith("v") or stable_version(tag[1:]) <= SEALED_VERSION:
        raise ValueError("v0.5.0 and earlier are sealed")
    ref = api(repository, f"git/ref/tags/{tag}")
    if ref is not None:
        obj = ref["object"]
        for _ in range(8):
            if obj["type"] != "tag":
                break
            obj = api(repository, f"git/tags/{obj['sha']}")["object"]
        if obj["type"] != "commit" or obj["sha"] != source:
            raise ValueError(f"{tag} already points to different content; never move release tags")
    release = api(repository, f"releases/tags/{tag}")
    if release is not None:
        if ref is None or release["draft"] or release["prerelease"]:
            raise ValueError("existing release has inconsistent/draft/prerelease state; manual review required")
    # A tag alone is a partial release, not evidence of registry success.
    return {"tag_exists": ref is not None, "release_exists": release is not None}


def notes(changelog: Path, version: str) -> str:
    text = changelog.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}\n", text)
    if match is None:
        raise ValueError(f"no dated changelog section for {version}")
    tail = text[match.start():]
    end = re.search(r"(?m)^## ", tail[len(match.group()):])
    return tail if end is None else tail[:len(match.group()) + end.start()]


def workflow_tree(repository: str, commit: str) -> str | None:
    root = api(repository, f"git/trees/{commit}")
    github = next((entry["sha"] for entry in root["tree"] if entry["path"] == ".github"), None)
    if github is None:
        return None
    entries = api(repository, f"git/trees/{github}")["tree"]
    return next((entry["sha"] for entry in entries if entry["path"] == "workflows"), None)


def require_workflow_token_compatibility(repository: str, source: str) -> None:
    """GITHUB_TOKEN cannot be granted Workflows:write for historical targets."""
    default = api(repository, "")["default_branch"]
    head = api(repository, f"git/ref/heads/{default}")["object"]["sha"]
    if head == source:
        return
    if workflow_tree(repository, source) != workflow_tree(repository, head):
        raise ValueError(
            "Packages are verified but GitHub finalization needs a maintainer: "
            "the original source workflows differ from the current default branch. "
            "GITHUB_TOKEN cannot receive Workflows:write. Follow the release recovery "
            "runbook; do not change the target SHA, move tags, or create credentials automatically."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--changelog", type=Path)
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--github-token", action="store_true", help="Fail clearly on historical-workflow permission limits")
    args = parser.parse_args()
    state = inspect(args.repository, args.tag, args.source)
    print(json.dumps(state, sort_keys=True))
    if not args.finalize:
        return 0
    if not args.changelog:
        raise ValueError("finalization requires a changelog")
    body = notes(args.changelog, args.tag[1:])
    if args.github_token and not state["release_exists"]:
        require_workflow_token_compatibility(args.repository, args.source)
    if not state["tag_exists"]:
        api(args.repository, "git/refs", {"ref": f"refs/tags/{args.tag}", "sha": args.source})
    if not state["release_exists"]:
        api(args.repository, "releases", {
            "tag_name": args.tag, "target_commitish": args.source, "name": args.tag,
            "body": body, "draft": False, "prerelease": False, "make_latest": "legacy",
        })
    # Re-read authoritative state even after successful HTTP writes.
    result = inspect(args.repository, args.tag, args.source)
    if not result["release_exists"]:
        raise ValueError("GitHub release was not confirmed")
    print(f"Verified GitHub release {args.tag} at {args.source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
