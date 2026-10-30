#!/usr/bin/env python3
"""Render the final verification report used by the final-audit workflow."""

from __future__ import annotations

import argparse
from pathlib import Path


def run_url(repo: str, run_id: str) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--compliance-run-id", required=True)
    parser.add_argument("--real-workloads-run-id", required=True)
    parser.add_argument("--swebench-run-id", required=True)
    parser.add_argument("--final-audit-run-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/final.md"))
    args = parser.parse_args()

    output = f"""# Final Integration Report

Repository: `https://github.com/{args.repo}`

Branch: `{args.branch}`

Commit: `{args.commit}`

Tag: `{args.tag}`

## Summary

This report indexes the workflow runs checked by `final-audit` for the SHA above.
It does not read their artifacts or establish individual benchmark outcomes.
The runtime can be launched with:

```bash
uvx coding-tools-mcp --workspace .
```

Copy/paste MCP client snippets are documented in `README.md`,
`docs/quickstart.md`, and `docs/mcp-client-config.md` for Codex, Claude Code,
Cursor, and generic MCP clients.

## Final GitHub Actions Evidence

- compliance: `{args.compliance_run_id}` ({run_url(args.repo, args.compliance_run_id)})
- real-workloads: `{args.real_workloads_run_id}` ({run_url(args.repo, args.real_workloads_run_id)})
- swebench-lite: `{args.swebench_run_id}` ({run_url(args.repo, args.swebench_run_id)})
- final-audit: `{args.final_audit_run_id}` ({run_url(args.repo, args.final_audit_run_id)})

The `final-audit` workflow validates that the first three runs completed with
`success` and that each run's `headSha` equals `{args.commit}`.

## Local Gates

The release gate expects passing local or CI runs for:

- `make lint`
- `make typecheck`
- `make test`
- `make ci`
- `make compliance`
- `make benchmark-smoke`
- `make benchmark-real-workloads`

## Compliance

The expected compliance evidence is `reports/compliance/latest.*` in the linked
workflow artifact. Review it for the full `all` suite, the required MCP tool
surface, and any failures or skips; this renderer does not inspect that evidence.

## Dogfood And Benchmarks

Individual result details are `UNKNOWN` to this metadata-only report. Inspect
the compliance and real-workloads artifacts linked above for the actual dogfood,
latency, and workload outcomes and any skips or limitations.

Real workload coverage includes public Python, Node, Rust, Go, and monorepo
repositories, plus large-file read, large-output command, and long-running
command checks.

## SWE-bench

- Official evaluation status: `UNKNOWN` (report artifacts were not inspected)
- Baseline completed/resolved counts: `UNKNOWN`
- Candidate completed/resolved counts: `UNKNOWN`

A successful workflow alone does not prove official Docker evaluation ran or
passed: advisory stages can fail or be blocked while the workflow succeeds.
Inspect the linked run's `swebench-lite-evidence` artifact, including its
`attempt.json`, selected evaluation reports, predictions, and raw harness logs.
Current workflows upload only that attempt's generated evidence. Missing reports
are not a pass, and historical checked-in reports do not verify this commit.

Reference-patch controls and scripted MCP replays are not model-generated
SWE-bench leaderboard scores. A valid comparison requires complete fresh official
reports, a nonzero baseline, and a candidate resolved count at least as high.

## Remaining Items

- Release readiness cannot be concluded from this metadata-only report.
- Review the referenced artifacts and their limitations before making outcome or
  model-generated benchmark claims.
"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
