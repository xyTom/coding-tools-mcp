from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.compliance.mcp_client import REQUIRED_TOOLS


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports" / "compliance"
JSON_REPORT = REPORT_DIR / "latest.json"
MD_REPORT = REPORT_DIR / "latest.md"
PROFILE = "codex-tool-runtime-mcp-v0.1"

SUITES = {
    "mcp-contract": ["tests.compliance.test_mcp_contract"],
    "tool-golden": ["tests.compliance.test_tool_golden"],
    "security": ["tests.compliance.test_security"],
    "e2e": ["tests.compliance.test_e2e"],
    "codex-compat": ["tests.compliance.test_codex_compat"],
    "dogfood": ["tests.compliance.test_dogfood"],
    "compliance-report": ["tests.compliance.test_compliance_report"],
    "docs-required": ["tests.compliance.test_docs_required"],
    "schema-drift": ["tests.compliance.test_schema_drift"],
}
SUITES["all"] = [module for name in SUITES for module in SUITES[name]]


@dataclass
class FailureRecord:
    test: str
    kind: str
    message: str
    traceback: str


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.records: list[FailureRecord] = []
        self.test_ids: list[str] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self.test_ids.append(test.id())
        super().startTest(test)

    def addFailure(self, test: unittest.case.TestCase, err: tuple[type[BaseException], BaseException, Any]) -> None:
        super().addFailure(test, err)
        self.records.append(make_record(test, "failure", err))

    def addError(self, test: unittest.case.TestCase, err: tuple[type[BaseException], BaseException, Any]) -> None:
        super().addError(test, err)
        self.records.append(make_record(test, "error", err))


class RecordingRunner(unittest.TextTestRunner):
    resultclass = RecordingResult


def make_record(
    test: unittest.case.TestCase,
    kind: str,
    err: tuple[type[BaseException], BaseException, Any],
) -> FailureRecord:
    exc_type, exc, tb = err
    return FailureRecord(
        test=test.id(),
        kind=kind,
        message=f"{exc_type.__name__}: {exc}",
        traceback="".join(traceback.format_exception(exc_type, exc, tb)),
    )


def load_suite(suite_name: str) -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for module in SUITES[suite_name]:
        suite.addTests(loader.loadTestsFromName(module))
    return suite


def git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except Exception:
        return "unknown"
    return completed.stdout.strip()


def write_reports(
    *,
    suite_name: str,
    result: RecordingResult | None,
    elapsed_seconds: float,
    write_only: bool = False,
) -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    failures = [] if result is None else [record.__dict__ for record in result.records]
    passed = bool(result and result.wasSuccessful())
    report: dict[str, Any] = {
        "profile": PROFILE,
        "commit": git_commit(),
        "suite": suite_name,
        "passed": passed,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "required_tools": required_tool_statuses(
            suite_name=suite_name,
            result=result,
            passed=passed,
        ),
        "security": category_status(result, "tests.compliance.test_security"),
        "e2e": category_status(result, "tests.compliance.test_e2e"),
        "codex_dogfood": category_status(result, "tests.compliance.test_dogfood"),
        "tests_run": 0 if result is None else result.testsRun,
        "failures": failures,
        "skipped": [] if result is None else [test.id() for test, _reason in result.skipped],
        "write_only": write_only,
    }
    JSON_REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    MD_REPORT.write_text(markdown_report(report), encoding="utf-8")
    return report


def required_tool_statuses(
    *,
    suite_name: str,
    result: RecordingResult | None,
    passed: bool,
) -> dict[str, str]:
    if result is None:
        status = "not_run"
    elif suite_name != "all":
        status = "not_measured"
    elif passed:
        status = "passed"
    else:
        status = "failed"
    return {tool: status for tool in REQUIRED_TOOLS}


def category_status(result: RecordingResult | None, module_prefix: str) -> str:
    if result is None:
        return "not_run"
    matching_runs = [test_id for test_id in result.test_ids if test_id.startswith(module_prefix)]
    if not matching_runs:
        return "not_run"
    matching_failures = [record for record in result.records if record.test.startswith(module_prefix)]
    if matching_failures:
        return "failed"
    return "passed"


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Compliance Report",
        "",
        f"- profile: `{report['profile']}`",
        f"- commit: `{report['commit']}`",
        f"- suite: `{report['suite']}`",
        f"- passed: `{str(report['passed']).lower()}`",
        f"- tests_run: `{report['tests_run']}`",
        f"- elapsed_seconds: `{report['elapsed_seconds']}`",
        "",
        "## Required Tools",
        "",
    ]
    for tool, status in report["required_tools"].items():
        lines.append(f"- `{tool}`: {status}")
    lines.extend(["", "## Failures", ""])
    if not report["failures"]:
        lines.append("No failures recorded.")
    else:
        for failure in report["failures"]:
            lines.extend(
                [
                    f"### {failure['test']}",
                    "",
                    f"- kind: `{failure['kind']}`",
                    f"- message: `{failure['message'][:500]}`",
                    "",
                    "```text",
                    failure["traceback"][-4000:],
                    "```",
                    "",
                ]
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Codex Tool Runtime MCP compliance tests.")
    parser.add_argument("--suite", choices=sorted(SUITES), default="all")
    parser.add_argument("--report", action="store_true", help="write reports/compliance/latest.{json,md}")
    parser.add_argument("--write-report-only", action="store_true", help="write a not-run report skeleton")
    args = parser.parse_args(argv)

    if args.write_report_only:
        write_reports(suite_name=args.suite, result=None, elapsed_seconds=0, write_only=True)
        return 0

    start = time.time()
    suite = load_suite(args.suite)
    runner = RecordingRunner(verbosity=2)
    result = runner.run(suite)
    elapsed = time.time() - start
    if args.report:
        write_reports(suite_name=args.suite, result=result, elapsed_seconds=elapsed)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
