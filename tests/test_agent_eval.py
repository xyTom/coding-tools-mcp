from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# `unittest discover -s tests` imports this module top-level, without running
# tests/__init__.py, and these tests start real servers: keep them silent.
os.environ.setdefault("CODING_TOOLS_MCP_TELEMETRY", "off")

from benchmarks.agent_eval.harness import ArmConfig, TaskRun, run_task
from benchmarks.agent_eval.run_eval import main, parse_arm
from benchmarks.agent_eval.scoring import RELEASE_GATES, render_markdown, score_runs
from benchmarks.agent_eval.tasks import Task, TaskError, load_tasks

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER = REPO_ROOT / "benchmarks/agent_eval/manifests/starter.json"

# A "no-op agent" leaves the workspace alone, so the task stays red; a
# "scripted agent" writes the answer. Between them they exercise both sides of
# every metric without needing a model.
NOOP_AGENT = f"{sys.executable} -c pass"


class ScriptedAgent:
    """Write one throwaway agent program and return the command that runs it."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._count = 0

    def command(self, script: str) -> str:
        self._count += 1
        path = self._directory / f"agent_{self._count}.py"
        path.write_text(script, encoding="utf-8")
        return f"{sys.executable} {path}"


class ManifestTests(unittest.TestCase):
    def test_the_shipped_starter_manifest_is_valid(self) -> None:
        tasks = load_tasks(STARTER)
        self.assertTrue(tasks)
        for task in tasks:
            with self.subTest(task=task.id):
                self.assertTrue(Path(task.fixture).is_dir())
                self.assertTrue(task.verify)

    def test_a_task_without_a_source_is_refused(self) -> None:
        with self.assertRaises(TaskError):
            Task.from_mapping({"id": "x", "prompt": "p", "verify": "true"}, source="<test>")

    def test_unknown_fields_are_refused_rather_than_ignored(self) -> None:
        with self.assertRaises(TaskError) as raised:
            Task.from_mapping(
                {"id": "x", "prompt": "p", "verify": "true", "setup": "true", "verfy": "true"},
                source="<test>",
            )
        self.assertIn("verfy", str(raised.exception))

    def test_duplicate_ids_are_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            entry = {"id": "same", "prompt": "p", "verify": "true", "setup": "true"}
            path.write_text(json.dumps({"tasks": [entry, entry]}), encoding="utf-8")
            with self.assertRaises(TaskError):
                load_tasks(path)


VERIFY_DONE_EXISTS = (
    f"{sys.executable} -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('done').exists() else 1)\""
)
VERIFY_NOTHING_BROKE = (
    f"{sys.executable} -c \"import pathlib,sys; sys.exit(1 if pathlib.Path('broke').exists() else 0)\""
)


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.workdir = Path(self._tmp.name)
        self.agents = ScriptedAgent(self.workdir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def task(self, **overrides: object) -> Task:
        base: dict[str, object] = {
            "id": "demo",
            "prompt": "make it pass",
            "verify": VERIFY_DONE_EXISTS,
            "setup": f"{sys.executable} -c pass",
            "timeout_s": 60,
            "max_rounds": 2,
        }
        base.update(overrides)
        return Task.from_mapping(base, source="<test>")

    def test_an_agent_that_fixes_the_task_is_scored_as_a_first_attempt_success(self) -> None:
        command = self.agents.command("import pathlib; pathlib.Path('done').write_text('x')")
        run = run_task(self.task(), ArmConfig("native", command), self.workdir)
        self.assertTrue(run.solved)
        self.assertEqual(run.rounds_to_green, 1)
        self.assertTrue(run.first_attempt_success)
        self.assertFalse(run.regression_introduced)

    def test_an_agent_that_never_fixes_it_burns_the_round_budget(self) -> None:
        run = run_task(self.task(), ArmConfig("native", NOOP_AGENT), self.workdir)
        self.assertFalse(run.solved)
        self.assertIsNone(run.rounds_to_green)
        self.assertEqual(len(run.rounds), 2)

    def test_a_second_round_fix_is_not_a_first_attempt_success(self) -> None:
        # The agent only writes the answer once it sees its own marker from
        # the first round, so round one fails and round two succeeds.
        command = self.agents.command(
            "import pathlib\n"
            "seen = pathlib.Path('.attempted')\n"
            "if seen.exists():\n"
            "    pathlib.Path('done').write_text('x')\n"
            "else:\n"
            "    seen.write_text('x')\n"
        )
        run = run_task(self.task(), ArmConfig("native", command), self.workdir)
        self.assertTrue(run.solved)
        self.assertEqual(run.rounds_to_green, 2)
        self.assertFalse(run.first_attempt_success)

    def test_breaking_the_regression_check_is_not_a_solve(self) -> None:
        command = self.agents.command(
            "import pathlib\n"
            "pathlib.Path('done').write_text('x')\n"
            "pathlib.Path('broke').write_text('x')\n"
        )
        run = run_task(
            self.task(regression_verify=VERIFY_NOTHING_BROKE, max_rounds=1),
            ArmConfig("native", command),
            self.workdir,
        )
        self.assertFalse(run.solved)
        self.assertTrue(run.regression_introduced)

    def test_a_task_that_is_already_green_is_reported_invalid(self) -> None:
        run = run_task(self.task(verify=f"{sys.executable} -c pass"), ArmConfig("native", NOOP_AGENT), self.workdir)
        self.assertTrue(run.started_green)
        self.assertFalse(run.solved)
        self.assertIn("already passed", run.error)

    def test_the_mcp_arm_hands_the_agent_a_live_server(self) -> None:
        # The agent edits through the server rather than the filesystem, so
        # this fails if the URL is missing or the server never came up.
        command = self.agents.command(
            "import os, sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from benchmarks.mcp_http import McpHttpClient\n"
            "client = McpHttpClient(os.environ['CODING_TOOLS_MCP_URL'])\n"
            "client.initialize()\n"
            "result = client.call_tool('apply_changes', {'changes': [\n"
            "    {'action': 'create', 'path': 'done', 'content': 'x\\n'}]})\n"
            "assert result.get('isError') is False, result\n"
        )
        run = run_task(self.task(max_rounds=1), ArmConfig("mcp", command, serve_mcp=True), self.workdir)
        self.assertTrue(run.solved, run.rounds)


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.workdir = Path(self._tmp.name)
        self.agents = ScriptedAgent(self.workdir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def task(self) -> Task:
        return Task.from_mapping(
            {
                "id": "demo",
                "prompt": "p",
                "verify": VERIFY_DONE_EXISTS,
                "setup": f"{sys.executable} -c pass",
                "timeout_s": 60,
                "max_rounds": 1,
            },
            source="<test>",
        )

    def runs(self) -> list[TaskRun]:
        command = self.agents.command("import pathlib; pathlib.Path('done').write_text('x')")
        return [
            run_task(self.task(), ArmConfig("mcp", command), self.workdir),
            run_task(self.task(), ArmConfig("native", NOOP_AGENT), self.workdir),
        ]

    def test_each_arm_is_scored_separately(self) -> None:
        report = score_runs(self.runs())
        self.assertEqual(report.arms["mcp"].to_dict()["final_pass_rate"], 1.0)
        self.assertEqual(report.arms["native"].to_dict()["final_pass_rate"], 0.0)

    def test_a_small_sample_never_passes_the_release_gate(self) -> None:
        report = score_runs(self.runs())
        self.assertFalse(report.meets_release_gates)
        self.assertTrue(
            any(str(RELEASE_GATES["minimum_tasks"]) in failure for failure in report.gate_failures),
            report.gate_failures,
        )
        self.assertIn("Release gates: not met.", render_markdown(report))

    def test_the_gate_applies_to_the_named_arm(self) -> None:
        report = score_runs(self.runs(), gated_arm="absent")
        self.assertEqual(report.gate_failures, ["no runs recorded for the gated arm"])


class CliTests(unittest.TestCase):
    def test_validate_only_checks_the_shipped_manifest(self) -> None:
        self.assertEqual(main(["--manifest", str(STARTER), "--validate-only"]), 0)

    def test_an_arm_needs_a_command(self) -> None:
        with self.assertRaises(Exception):
            parse_arm("native")

    def test_the_mcp_suffix_asks_for_a_server(self) -> None:
        arm = parse_arm("mcp:mcp=run-agent")
        self.assertTrue(arm.serve_mcp)
        self.assertEqual(arm.agent_command, "run-agent")
        self.assertFalse(parse_arm("native=run-agent").serve_mcp)


if __name__ == "__main__":
    unittest.main()
