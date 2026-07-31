#!/usr/bin/env python3
"""Require a successful final-audit workflow run for a release commit."""

from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def select_successful_run(workflow_runs: list[dict[str, object]], sha: str) -> dict[str, object] | None:
    matches = [
        run
        for run in workflow_runs
        if run.get("head_sha") == sha
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    ]
    if not matches:
        return None
    return max(matches, key=lambda run: int(run.get("id", 0)))


def workflow_runs_url(api_url: str, repo: str, workflow: str, sha: str) -> str:
    owner, name = repo.split("/", 1)
    base = (
        f"{api_url.rstrip('/')}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        f"/actions/workflows/{quote(workflow, safe='')}/runs"
    )
    query = urlencode({"status": "success", "head_sha": sha, "per_page": 100})
    return f"{base}?{query}"


def fetch_runs(api_url: str, repo: str, workflow: str, sha: str, token: str) -> list[dict[str, object]]:
    request = Request(
        workflow_runs_url(api_url, repo, workflow, sha),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "coding-tools-mcp-release-audit",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub Actions API failed with {error.code}: {detail}") from error
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise SystemExit("GitHub Actions API returned an invalid workflow_runs payload")
    return runs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="GitHub repository as owner/name")
    parser.add_argument("--sha", required=True, help="Release commit SHA")
    parser.add_argument("--workflow", default="final-audit.yml")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required to verify final-audit evidence")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    run = select_successful_run(
        fetch_runs(api_url, args.repo, args.workflow, args.sha, token), args.sha
    )
    if run is None:
        raise SystemExit(
            f"no successful {args.workflow} run was found for release commit {args.sha}"
        )

    print(f"Final audit OK: {run.get('html_url', '')} ({args.sha})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
