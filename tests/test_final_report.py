from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.render_final_report import main


class FinalReportTests(unittest.TestCase):
    def test_metadata_only_audit_does_not_invent_benchmark_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new/report.md"
            args = ["render_final_report", "--repo", "owner/repo", "--branch", "main",
                    "--commit", "a" * 40, "--tag", "v0.5.1", "--compliance-run-id", "101",
                    "--real-workloads-run-id", "102", "--swebench-run-id", "103",
                    "--final-audit-run-id", "104", "--output", str(output)]
            with patch("sys.argv", args):
                self.assertEqual(main(), 0)
            text = output.read_text()
            self.assertIn("Official evaluation status: `UNKNOWN`", text)
            self.assertIn("Baseline completed/resolved counts: `UNKNOWN`", text)
            self.assertIn("Candidate completed/resolved counts: `UNKNOWN`", text)
            self.assertIn("advisory stages can fail or be blocked", text)
            self.assertIn("https://github.com/owner/repo/actions/runs/103", text)
            self.assertIn("swebench-lite-evidence", text)
            self.assertIn("attempt.json", text)
            for unsupported in ("`PASS`", "`1 / 1`", "No release-blocking items remain",
                                "workflow ran the official Docker", "under `reports/benchmark/`"):
                self.assertNotIn(unsupported, text)


if __name__ == "__main__":
    unittest.main()
