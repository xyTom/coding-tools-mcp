from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

from scripts.check_dispatch_inputs import workflow_triggers


ROOT = Path(__file__).resolve().parents[1]
SOURCE_EXPRESSION = "${{ inputs.source_ref || github.event.pull_request.head.sha || github.sha }}"


def _load_workflow(name: str) -> dict:
    with (ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class SandboxReleaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.native = _load_workflow("cross-platform-sandbox.yml")
        self.release = _load_workflow("release.yml")

    def test_native_acceptance_is_reusable_and_pins_every_runner(self) -> None:
        inputs = workflow_triggers(self.native)["workflow_call"]["inputs"]
        self.assertEqual(inputs["source_ref"]["type"], "string")
        self.assertTrue(inputs["source_ref"]["required"])
        self.assertEqual(self.native["env"]["SANDBOX_SOURCE_SHA"], SOURCE_EXPRESSION)
        for name, job in self.native["jobs"].items():
            with self.subTest(job=name):
                self.assertNotIn("if", job, "release acceptance must run every platform")
                self.assertFalse(job.get("continue-on-error", False))
                steps = job["steps"]
                checkout = [
                    step for step in steps
                    if step.get("uses", "").startswith("actions/checkout@")
                ]
                self.assertTrue(checkout, "every runner must check out the source")
                for step in checkout:
                    self.assertEqual(step["with"]["ref"], SOURCE_EXPRESSION)
                    self.assertFalse(step["with"]["persist-credentials"])
                self.assertTrue(any(
                    "source-revision.txt" in step.get("with", {}).get("path", "")
                    and step.get("if") == "always()"
                    for step in steps
                ), "native evidence must preserve the tested commit SHA")
                for step in steps:
                    self.assertFalse(step.get("continue-on-error", False))
                    if step.get("uses", "").startswith("actions/upload-artifact@"):
                        self.assertTrue(
                            step["with"].get("overwrite"),
                            "failed native jobs must be able to replace evidence on retry",
                        )

    def test_release_build_and_publish_cannot_bypass_native_acceptance(self) -> None:
        jobs = self.release["jobs"]
        gate = jobs["native-sandbox"]
        self.assertEqual(gate["uses"], "./.github/workflows/cross-platform-sandbox.yml")
        # The tag-driven release uses github.sha. The version-PR release plan
        # selects an older, immutable source SHA for recovery publications.
        planned_release = "plan" in jobs
        source = (
            "${{ needs.plan.outputs.source_sha }}" if planned_release else "${{ github.sha }}"
        )
        self.assertEqual(gate["with"]["source_ref"], source)
        self.assertFalse(gate.get("continue-on-error", False))
        if planned_release:
            self.assertEqual(gate["needs"], "plan")
            self.assertEqual(gate["if"], "needs.plan.outputs.publish == 'true'")
        else:
            self.assertNotIn("if", gate)

        def ancestors(name: str) -> set[str]:
            dependencies = jobs[name].get("needs", [])
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            result = set(dependencies)
            for dependency in dependencies:
                result.update(ancestors(dependency))
            return result

        release_jobs = ["build", "publish-pypi", "publish-npm", "github-release"]
        if "pack-npm" in jobs:
            release_jobs.append("pack-npm")
        for name in release_jobs:
            with self.subTest(job=name):
                dependencies = ancestors(name)
                self.assertIn("native-sandbox", dependencies)
                for dependency in dependencies | {name}:
                    job = jobs[dependency]
                    self.assertFalse(job.get("continue-on-error", False))
                    condition = job.get("if", "")
                    self.assertNotIn("always()", condition)
                    self.assertNotIn("!cancelled()", condition)
        source_jobs = (
            ("plan", "build", "github-release") if planned_release
            else ("validate", "build", "pack-npm", "github-release")
        )
        for name in source_jobs:
            with self.subTest(source_job=name):
                source_checkouts = [
                    step for step in jobs[name]["steps"]
                    if step.get("uses", "").startswith("actions/checkout@")
                    and (not planned_release or step.get("with", {}).get("path") == "source")
                ]
                self.assertTrue(source_checkouts)
                for step in source_checkouts:
                    expected = "${{ steps.plan.outputs.source_sha }}" if name == "plan" else source
                    self.assertEqual(step["with"]["ref"], expected)

    def test_native_matrix_retains_every_supported_architecture_and_shell(self) -> None:
        jobs = self.native["jobs"]
        platforms = {
            item["platform"]: item["architecture"]
            for item in jobs["native-posix"]["strategy"]["matrix"]["include"]
        }
        self.assertEqual(platforms, {
            "Linux": "x86_64", "Linux-ARM64": "aarch64", "macOS": "arm64",
        })
        self.assertEqual(
            jobs["native-posix"]["env"]["CODING_TOOLS_SANDBOX_REQUIRE_NATIVE"], "1"
        )
        self.assertEqual(
            set(jobs["windows-shells"]["strategy"]["matrix"]["shell"]), {"cmd", "pwsh"}
        )

    def test_git_helper_security_runs_and_preserves_evidence_on_every_platform(self) -> None:
        for name, job in self.native["jobs"].items():
            with self.subTest(job=name):
                steps = job["steps"]
                acceptance = [
                    step for step in steps
                    if "tests.test_git_helper_security" in step.get("run", "")
                    and "tests.test_workspace_executor" in step.get("run", "")
                ]
                self.assertEqual(len(acceptance), 1)
                self.assertNotIn("if", acceptance[0])
                self.assertFalse(acceptance[0].get("continue-on-error", False))
                self.assertIn("git-helper-results.txt", acceptance[0]["run"])
                self.assertTrue(any(
                    "git-helper-results.txt" in step.get("with", {}).get("path", "")
                    and step.get("if") == "always()"
                    for step in steps
                ))

    def test_source_evidence_rejects_mismatched_and_mutable_refs(self) -> None:
        commands = [
            step["run"]
            for job in self.native["jobs"].values()
            for step in job["steps"]
            if step.get("name") == "Require and record the exact source commit"
        ]
        self.assertEqual(len(commands), len(self.native["jobs"]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Workflow test",
                "-c", "user.email=workflow-test@example.invalid", "commit",
                "--allow-empty", "--quiet", "-m", "source fixture",
            ], check=True)
            actual = subprocess.check_output([
                "git", "-C", str(root), "rev-parse", "HEAD",
            ], text=True).strip()
            for command in commands:
                arguments = shlex.split(command)
                self.assertEqual(arguments[:2], ["python", "-c"])
                arguments[0] = sys.executable
                for expected in (actual, "0" * 40, "main"):
                    with self.subTest(command=command, source=expected):
                        evidence = root / "source-revision.txt"
                        evidence.unlink(missing_ok=True)
                        result = subprocess.run(
                            arguments, cwd=root,
                            env={**os.environ, "SANDBOX_SOURCE_SHA": expected},
                            capture_output=True, text=True, check=False,
                        )
                        if expected == actual:
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(evidence.read_text(encoding="utf-8"), actual + "\n")
                        else:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
