#!/usr/bin/env python3
"""Aggregate repeated deterministic dogfood reports for before/after evidence."""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


METRICS = (
    "completion_rate",
    "total_elapsed_ms",
    "tool_call_count",
    "argument_bytes",
    "result_bytes",
    "first_patch_success_rate",
    "session_poll_count",
    "tool_latency_p50_ms",
    "tool_latency_p95_ms",
)


def load_reports(paths: list[Path]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("metrics"), dict):
            raise ValueError(f"Not a dogfood report: {path}")
        reports.append(payload)
    return reports


def version_of(report: dict[str, Any]) -> str:
    initialize = report.get("initialize")
    if not isinstance(initialize, dict):
        return "unknown"
    server_info = initialize.get("serverInfo")
    if not isinstance(server_info, dict):
        return "unknown"
    return str(server_info.get("version") or "unknown")


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "runs": len(reports),
        "versions": sorted({version_of(report) for report in reports}),
        "pass_rate": round(
            sum(report.get("conclusion") == "PASS" for report in reports) / len(reports),
            3,
        ),
        "metrics": {},
    }
    for name in METRICS:
        samples = [float(report["metrics"][name]) for report in reports]
        summary["metrics"][name] = {
            "median": round(statistics.median(samples), 3),
            "min": round(min(samples), 3),
            "max": round(max(samples), 3),
        }
    return summary


def percent_change(before: float, after: float) -> float | None:
    if before == 0:
        return None
    return round((after - before) / before * 100, 3)


def comparison(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in METRICS:
        before = float(baseline["metrics"][name]["median"])
        after = float(candidate["metrics"][name]["median"])
        rows.append(
            {
                "metric": name,
                "baseline_median": before,
                "candidate_median": after,
                "change_percent": percent_change(before, after),
            }
        )
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    candidate = report["candidate"]
    lines = [
        "# Deterministic Dogfood Before/After",
        "",
        f"- Baseline versions: `{', '.join(baseline['versions'])}`",
        f"- Candidate versions: `{', '.join(candidate['versions'])}`",
        f"- Repetitions: `{baseline['runs']}` baseline / `{candidate['runs']}` candidate",
        f"- Pass rate: `{baseline['pass_rate']}` baseline / `{candidate['pass_rate']}` candidate",
        "",
        "| metric | baseline median | candidate median | change |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in report["comparison"]:
        change = "n/a" if row["change_percent"] is None else f"{row['change_percent']}%"
        lines.append(
            f"| `{row['metric']}` | {row['baseline_median']} | "
            f"{row['candidate_median']} | {change} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation Boundary",
            "",
            "This uses the same deterministic MCP-only runner, fixture, task order, and machine.",
            "It measures runtime/tool-contract regression, not model quality and not Codex, OpenCode,",
            "or Devspace end-to-end performance. Timing samples are local and should be treated as",
            "directional; completion, call counts, polling, and serialized byte counts are deterministic.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, nargs="+", required=True)
    parser.add_argument("--candidate", type=Path, nargs="+", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()

    baseline_reports = load_reports(args.baseline)
    candidate_reports = load_reports(args.candidate)
    baseline = summarize(baseline_reports)
    candidate = summarize(candidate_reports)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": "same deterministic MCP-only runner, fixture, task order, and machine",
        "baseline": baseline,
        "candidate": candidate,
        "comparison": comparison(baseline, candidate),
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output_md.write_text(render_markdown(report), encoding="utf-8")
    return 0 if baseline["pass_rate"] == 1.0 and candidate["pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
