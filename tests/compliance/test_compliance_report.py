from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.compliance import runner
from tests.compliance.mcp_client import REQUIRED_TOOLS


class FakeTest:
    def __init__(self, test_id: str) -> None:
        self.test_id = test_id

    def id(self) -> str:
        return self.test_id


class FakeResult:
    def __init__(self, *, successful: bool) -> None:
        self._successful = successful
        self.testsRun = 4
        self.test_ids = [
            "tests.compliance.test_security.SecurityComplianceTests.test_escape",
            "tests.compliance.test_e2e.DeterministicE2ETests.test_loop",
            "tests.compliance.test_dogfood.DogfoodMCPOnlyTests.test_agent",
            "tests.compliance.test_mcp_contract.MCPContractTests.test_tools",
        ]
        self.records = [
            runner.FailureRecord(
                test="tests.compliance.test_security.SecurityComplianceTests.test_escape",
                kind="failure",
                message="AssertionError: denied path leaked",
                traceback="Traceback tail for security failure",
            )
        ]
        self.skipped = [(FakeTest("tests.compliance.test_e2e.DeterministicE2ETests.test_optional"), "optional")]

    def wasSuccessful(self) -> bool:
        return self._successful


class ComplianceReportTests(unittest.TestCase):
    def test_write_reports_records_categories_required_tools_and_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-mcp-report-") as tmp:
            report_dir = Path(tmp)
            json_report = report_dir / "latest.json"
            md_report = report_dir / "latest.md"
            result = FakeResult(successful=False)

            with (
                patch.object(runner, "REPORT_DIR", report_dir),
                patch.object(runner, "JSON_REPORT", json_report),
                patch.object(runner, "MD_REPORT", md_report),
                patch.object(runner, "git_commit", return_value="abc123"),
            ):
                report = runner.write_reports(suite_name="all", result=result, elapsed_seconds=1.2345)

            self.assertEqual(json.loads(json_report.read_text(encoding="utf-8")), report)
            self.assertEqual(report["commit"], "abc123")
            self.assertEqual(report["suite"], "all")
            self.assertIs(report["passed"], False)
            self.assertEqual(report["tests_run"], 4)
            self.assertEqual(report["security"], "failed")
            self.assertEqual(report["e2e"], "passed")
            self.assertEqual(report["codex_dogfood"], "passed")
            self.assertTrue(all(report["required_tools"][tool] == "failed" for tool in REQUIRED_TOOLS))
            self.assertEqual(
                report["skipped"],
                ["tests.compliance.test_e2e.DeterministicE2ETests.test_optional"],
            )

            markdown = md_report.read_text(encoding="utf-8")
            self.assertIn("tests.compliance.test_security.SecurityComplianceTests.test_escape", markdown)
            self.assertIn("AssertionError: denied path leaked", markdown)
            self.assertIn("Traceback tail for security failure", markdown)

    def test_write_report_only_marks_not_run_without_repo_report_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-mcp-report-") as tmp:
            report_dir = Path(tmp)
            json_report = report_dir / "latest.json"
            md_report = report_dir / "latest.md"

            with (
                patch.object(runner, "REPORT_DIR", report_dir),
                patch.object(runner, "JSON_REPORT", json_report),
                patch.object(runner, "MD_REPORT", md_report),
                patch.object(runner, "git_commit", return_value="abc123"),
            ):
                report = runner.write_reports(
                    suite_name="mcp-contract",
                    result=None,
                    elapsed_seconds=0,
                    write_only=True,
                )

            self.assertTrue(json_report.exists())
            self.assertTrue(md_report.exists())
            self.assertEqual(report["tests_run"], 0)
            self.assertIs(report["write_only"], True)
            self.assertEqual(report["failures"], [])
            self.assertEqual(report["skipped"], [])
            self.assertTrue(all(report["required_tools"][tool] == "not_run" for tool in REQUIRED_TOOLS))
            self.assertIn("No failures recorded.", md_report.read_text(encoding="utf-8"))

    def test_partial_suite_does_not_claim_required_tool_coverage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-mcp-report-") as tmp:
            report_dir = Path(tmp)
            json_report = report_dir / "latest.json"
            md_report = report_dir / "latest.md"

            with (
                patch.object(runner, "REPORT_DIR", report_dir),
                patch.object(runner, "JSON_REPORT", json_report),
                patch.object(runner, "MD_REPORT", md_report),
                patch.object(runner, "git_commit", return_value="abc123"),
            ):
                report = runner.write_reports(
                    suite_name="security",
                    result=FakeResult(successful=True),
                    elapsed_seconds=1.0,
                )

            self.assertTrue(all(report["required_tools"][tool] == "not_measured" for tool in REQUIRED_TOOLS))
