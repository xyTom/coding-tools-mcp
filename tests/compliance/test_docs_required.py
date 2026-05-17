from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class RequiredDocsTests(unittest.TestCase):
    def test_required_operator_docs_exist(self) -> None:
        required_paths = [
            "README.md",
            "SECURITY.md",
            "COMPLIANCE.md",
            "BENCHMARK.md",
            "docs/quickstart.md",
            "docs/mcp-client-config.md",
            "docs/tools-and-schemas.md",
            "docs/ci-and-tests.md",
            "docs/dogfood.md",
            "docs/swe-bench.md",
            "docs/limitations.md",
            "docs/troubleshooting.md",
            "docs/competitive-analysis.md",
            "docs/profile-v0.1.md",
        ]
        missing = [path for path in required_paths if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_required_evidence_artifacts_exist(self) -> None:
        required_paths = [
            "reports/compliance/latest.json",
            "reports/compliance/latest.md",
            "reports/dogfood/codex-on-mcp.json",
            "reports/dogfood/codex-on-mcp.md",
            "docs/dogfood/codex-on-mcp-transcript.json",
            "reports/benchmark/swebench-regression.json",
            "reports/benchmark/swebench-regression.md",
            "reports/benchmark/swebench-official-attempt.json",
            "reports/benchmark/swebench-official-attempt.md",
        ]
        missing = [path for path in required_paths if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_docs_contain_required_operational_topics(self) -> None:
        expectations = {
            "README.md": ["Quickstart", "Safety Boundary", "Dogfood", "SWE-bench"],
            "SECURITY.md": ["Linux Landlock", "Environment Scrubbing", "Session Lifecycle"],
            "COMPLIANCE.md": ["make compliance", "required_tools", "not_measured"],
            "BENCHMARK.md": ["make dogfood-smoke", "PREFLIGHT_ONLY", "swebench-official-attempt"],
            "docs/ci-and-tests.md": ["make ci", "workflow", "swebench-lite"],
            "docs/dogfood.md": ["MCP-Only Rule", "view_image", "Direct filesystem/shell bypass"],
            "docs/swe-bench.md": ["Official attempt report", "BLOCKED", "sympy__sympy-12419"],
            "docs/troubleshooting.md": ["SANDBOX_UNAVAILABLE", "MCP-Protocol-Version"],
            "docs/competitive-analysis.md": ["Claude Code", "Aider", "OpenHands", "Cline"],
        }
        for rel_path, needles in expectations.items():
            text = (ROOT / rel_path).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(path=rel_path, needle=needle):
                    self.assertIn(needle, text)

    def test_ci_workflows_include_required_gates(self) -> None:
        compliance = (ROOT / ".github/workflows/compliance.yml").read_text(encoding="utf-8")
        for needle in (
            "make lint",
            "make typecheck",
            "make test",
            "make test-protocol",
            "make test-integration",
            "make dogfood-smoke",
            "make benchmark-smoke",
            "make compliance",
            "actions/upload-artifact",
        ):
            with self.subTest(workflow="compliance", needle=needle):
                self.assertIn(needle, compliance)

        swebench = (ROOT / ".github/workflows/swebench-lite.yml").read_text(encoding="utf-8")
        for needle in ("workflow_dispatch", "--install-swebench", "--run-evaluation", "reports/benchmark/**"):
            with self.subTest(workflow="swebench-lite", needle=needle):
                self.assertIn(needle, swebench)
