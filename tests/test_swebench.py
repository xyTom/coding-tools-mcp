from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

os.environ.setdefault("CODING_TOOLS_MCP_TELEMETRY", "off")

from benchmarks.swebench import generate_reference_predictions as reference
from benchmarks.swebench import pinned, replay_mcp, run_smoke


class PinnedInputTests(unittest.TestCase):
    def test_fixture_revision_patch_and_source_are_immutable(self) -> None:
        pins = pinned.load_pins()
        rows = pinned.load_instances(pins)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len(pins["dataset"]["revision"]), 40)
        self.assertEqual(pins["swebench_version"], "4.1.0")
        row = pinned.replay_instance(pins)
        self.assertEqual(row["instance_id"], "sympy__sympy-12419")
        self.assertEqual(row["base_commit"], "479939f8c65c8c2908bbedc959549a257a7c0b0b")
        self.assertEqual(pinned.sha256((pinned.ROOT / pins["replay"]["source_fixture"]).read_bytes()),
                         pins["replay"]["source_sha256"])
        for row in rows:
            source, tag = pinned.docker_image(pins, row["instance_id"])
            self.assertIn("@sha256:", source)
            self.assertNotIn(":latest", tag)
            self.assertEqual(pins["docker"]["images"][row["instance_id"]]["platform"], "linux/amd64")

    def test_corrupt_fixture_is_rejected(self) -> None:
        pins = pinned.load_pins()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / pins["dataset"]["fixture"]
            fixture.parent.mkdir()
            fixture.write_text("[]")
            with patch.object(pinned, "ROOT", root), self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                pinned.load_instances(pins)

    def test_changed_reference_patch_hash_is_rejected(self) -> None:
        pins = pinned.load_pins()
        pins["replay"]["reference_patch_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "patch SHA-256"):
            pinned.replay_instance(pins)

    def test_reference_generation_is_offline_and_labeled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = reference.main(["--instance-id", "sympy__sympy-12419", "--baseline-output", str(root / "b.jsonl"),
                                     "--candidate-output", str(root / "c.jsonl"), "--metadata-output", str(root / "meta.json")])
            self.assertEqual(result, 0)
            metadata = json.loads((root / "meta.json").read_text())
            self.assertEqual(metadata["prediction_source"], "reference_patch")
            self.assertIn("not model-generated", metadata["warning"])
            self.assertEqual(metadata["pins"], pinned.load_pins())
            self.assertIn("harness_control", json.loads((root / "c.jsonl").read_text())["model_name_or_path"])

    def test_reference_generation_rejects_floating_dataset_or_unknown_instance(self) -> None:
        with self.assertRaises(ValueError):
            reference.fetch_reference_patches("other/dataset", "test", ["sympy__sympy-12419"])
        with self.assertRaises(ValueError):
            reference.fetch_reference_patches("princeton-nlp/SWE-bench_Lite", "test", ["unknown"])


class PredictionValidationTests(unittest.TestCase):
    def validate(self, rows: list[object], ids: set[str] | None = None) -> run_smoke.PredictionSet:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "predictions.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            return run_smoke.validate_predictions(path, ids if ids is not None else {"one"})

    def row(self, instance: str = "one", content: object = "diff") -> dict[str, object]:
        return {"instance_id": instance, "model_name_or_path": "model", "model_patch": content}

    def test_non_object_prediction_is_rejected(self) -> None:
        self.assertTrue(self.validate([[]]).errors)

    def test_non_string_patch_is_rejected(self) -> None:
        self.assertTrue(self.validate([self.row(content=123)]).errors)

    def test_duplicate_prediction_is_rejected(self) -> None:
        self.assertIn("duplicate", " ".join(self.validate([self.row(), self.row()]).errors))

    def test_any_empty_selected_patch_is_a_placeholder(self) -> None:
        result = self.validate([self.row(), self.row("two", "")], {"one", "two"})
        self.assertTrue(result.placeholder)

    def test_empty_selection_is_rejected(self) -> None:
        self.assertTrue(self.validate([self.row()], set()).errors)

    def test_unknown_subset_instance_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not in pinned subset"):
            run_smoke.selected_instances({"instances": [{"instance_id": "one"}]}, ["typo"])

    def test_missing_and_invalid_model_are_rejected(self) -> None:
        for model in ("", "..", ".", None):
            row = self.row()
            row["model_name_or_path"] = model
            with self.subTest(model=model):
                self.assertTrue(self.validate([row]).errors)


class HarnessTests(unittest.TestCase):
    def test_command_uses_verified_local_dataset_and_tag(self) -> None:
        command = run_smoke.evaluation_command(Path("predictions.jsonl"), "unique-run", 1, ["sympy__sympy-12419"])
        self.assertEqual(command[command.index("--dataset_name") + 1], str(pinned.dataset_path(pinned.load_pins())))
        self.assertEqual(command[command.index("--instance_image_tag") + 1], "coding-tools-pinned-v1")
        self.assertNotIn("latest", command)
        self.assertIn("--split", command)

    def test_wrong_harness_version_does_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_smoke, "package_version", return_value="99.0"), patch.object(run_smoke, "capture") as capture:
            available, detail, _, _ = run_smoke.check_swebench(Path(tmp), install=False)
            self.assertFalse(available)
            self.assertIn("4.1.0", detail)
            capture.assert_not_called()

    def test_install_requests_exact_harness_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_smoke, "package_version", side_effect=[None, "4.1.0"]), patch.object(run_smoke, "capture", return_value={"returncode": 0}) as capture:
            self.assertTrue(run_smoke.check_swebench(Path(tmp), install=True)[0])
            self.assertIn("swebench==4.1.0", capture.call_args_list[0].args[0])

    def test_images_are_pulled_by_digest_and_local_alias_is_verified(self) -> None:
        responses = [{"returncode": 0}, {"returncode": 0}, {"returncode": 0, "stdout": "sha256:abc\nsha256:abc\n"}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_smoke, "capture", side_effect=responses) as capture:
            ok, _ = run_smoke.prepare_images(["sympy__sympy-12419"], Path(tmp))
            self.assertTrue(ok)
            self.assertIn("@sha256:", capture.call_args_list[0].args[0][-1])
            self.assertEqual(capture.call_args_list[1].args[0][1], "tag")
            self.assertIn("inspect", capture.call_args_list[2].args[0])

    def test_image_pull_failure_stops_before_tag_or_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_smoke, "capture", return_value={"returncode": 1}) as capture:
            self.assertFalse(run_smoke.prepare_images(["sympy__sympy-12419"], Path(tmp))[0])
            self.assertEqual(capture.call_count, 1)

    def test_mismatched_local_image_alias_fails_closed(self) -> None:
        responses = [{"returncode": 0}, {"returncode": 0}, {"returncode": 0, "stdout": "sha256:abc\nsha256:def\n"}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_smoke, "capture", side_effect=responses):
            self.assertFalse(run_smoke.prepare_images(["sympy__sympy-12419"], Path(tmp))[0])

    def test_zero_resolution_cannot_pass(self) -> None:
        self.assertEqual(run_smoke.comparison_conclusion({"completed": 1, "resolved": 0}, {"completed": 1, "resolved": 0}, 1), "FAIL")

    def test_partial_reports_cannot_pass(self) -> None:
        self.assertEqual(run_smoke.comparison_conclusion({"completed": 1, "resolved": 1}, {"completed": 1, "resolved": 1}, 2), "INCONCLUSIVE")

    def test_complete_nonzero_reports_pass(self) -> None:
        self.assertEqual(run_smoke.comparison_conclusion({"completed": 1, "resolved": 1}, {"completed": 1, "resolved": 1}, 1), "PASS")

    def test_missing_docker_is_blocked_and_reruns_have_distinct_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_smoke, "check_docker", return_value=(False, "missing", {})), patch.object(run_smoke, "check_swebench", return_value=(True, "ok", None, {})), patch.object(run_smoke, "capture_environment", return_value={}):
            root = Path(tmp)
            prediction = root / "p.jsonl"
            reference.write_predictions(prediction, "test", {"sympy__sympy-12419": "nonempty"})
            args = ["--run-evaluation", "--require-evaluation-pass", "--instance-id", "sympy__sympy-12419",
                    "--baseline-predictions", str(prediction), "--candidate-predictions", str(prediction),
                    "--report-json", str(root / "report.json"), "--report-md", str(root / "report.md")]
            self.assertEqual(run_smoke.main(args), 1)
            first = json.loads((root / "report.json").read_text())
            self.assertEqual(first["conclusion"], "BLOCKED")
            self.assertFalse(first["candidate"]["run"]["ran"])
            old_raw = Path(first["raw_dir"])
            old_raw.mkdir(parents=True)
            (old_raw / "historical-success.json").write_text('{"resolved": true}')
            self.assertEqual(run_smoke.main(args), 1)
            second = json.loads((root / "report.json").read_text())
            self.assertNotEqual(first["run_ids"], second["run_ids"])
            self.assertNotEqual(first["raw_dir"], second["raw_dir"])
            self.assertTrue((old_raw / "historical-success.json").exists())
            self.assertFalse((Path(second["raw_dir"]) / "historical-success.json").exists())

    def test_early_failure_replaces_stale_pass_reports(self) -> None:
        for failure in ("instance", "pins", "workers"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                report, markdown = root / "report.json", root / "report.md"
                report.write_text('{"conclusion": "PASS", "baseline": {"resolved": 1}}')
                markdown.write_text("# Historical result\nPASS\n")
                args = ["--report-json", str(report), "--report-md", str(markdown)]
                if failure == "instance":
                    args.extend(["--instance-id", "unknown-typo"])
                elif failure == "workers":
                    args.extend(["--max-workers", "0"])
                with contextlib.ExitStack() as stack:
                    if failure == "pins":
                        stack.enter_context(patch.object(run_smoke, "load_pins", side_effect=ValueError("broken pin")))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    self.assertEqual(run_smoke.main(args), 1)
                current = json.loads(report.read_text())
                self.assertEqual(current["conclusion"], "ERROR")
                self.assertNotIn("resolved", current["baseline"])
                self.assertNotIn("PASS", markdown.read_text())
                self.assertIn("failed:", " ".join(current["limitations"]))

    def test_interruption_cannot_leave_a_prior_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report, markdown = root / "report.json", root / "report.md"
            report.write_text('{"conclusion": "PASS"}')
            with patch.object(run_smoke, "load_pins", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                run_smoke.main(["--report-json", str(report), "--report-md", str(markdown)])
            self.assertEqual(json.loads(report.read_text())["conclusion"], "INCONCLUSIVE")
            self.assertNotIn("PASS", markdown.read_text())


class WorkflowTests(unittest.TestCase):
    def test_advisory_workflow_runs_both_sources_at_pinned_sha(self) -> None:
        text = (pinned.ROOT.parents[1] / ".github/workflows/swebench-lite.yml").read_text()
        self.assertIn("source_ref:", text)
        self.assertIn("ref: ${{ inputs.source_ref || github.sha }}", text)
        self.assertIn("default: both", text)
        self.assertIn("overwrite: true", text)
        self.assertIn("--prediction-source mcp_reference_replay", text)
        self.assertIn("source=reference_patch", text)
        self.assertNotIn('ids="${{', text)
        self.assertNotIn('--max-workers "${{', text)

    def test_only_current_attempt_evidence_is_uploaded(self) -> None:
        workflow = yaml.safe_load((pinned.ROOT.parents[1] / ".github/workflows/swebench-lite.yml").read_text())
        job = workflow["jobs"]["swebench-lite"]
        self.assertNotIn("EVIDENCE_ROOT", job["env"])
        initialization = job["steps"][0]["run"]
        for required in ("mktemp -d", "$RUNNER_TEMP", "$GITHUB_RUN_ID", "$GITHUB_RUN_ATTEMPT", "$GITHUB_ENV", "attempt.json"):
            self.assertIn(required, initialization)
        uploads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["with"]["path"], "${{ env.EVIDENCE_ROOT }}")
        for step in job["steps"]:
            run = step.get("run", "")
            self.assertNotIn("reports/benchmark", run)
            if "replay_mcp.py" in run:
                self.assertIn('--output-dir "$EVIDENCE_ROOT/mcp-replay"', run)
            if "run_smoke.py" in run:
                self.assertIn('--report-json "$EVIDENCE_ROOT/', run)
                self.assertIn('--report-md "$EVIDENCE_ROOT/', run)

    def test_failed_workflow_validation_leaves_only_current_attempt_manifest(self) -> None:
        workflow = yaml.safe_load((pinned.ROOT.parents[1] / ".github/workflows/swebench-lite.yml").read_text())
        steps = workflow["jobs"]["swebench-lite"]["steps"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "reports/benchmark/historical.json"
            old.parent.mkdir(parents=True)
            old.write_text('{"conclusion": "PASS"}')
            env_file = root / "github-env"
            env = {**os.environ, "RUNNER_TEMP": str(root), "GITHUB_ENV": str(env_file), "SOURCE_REF": "invalid",
                   "PREDICTION_SOURCE": "mcp_reference_replay", "GITHUB_SHA": "a" * 40,
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}
            subprocess.run(["bash", "-e", "-c", steps[0]["run"]], cwd=root, env=env, check=True)
            evidence = Path(env_file.read_text().strip().removeprefix("EVIDENCE_ROOT="))
            validation = subprocess.run(["bash", "-e", "-c", steps[1]["run"]], cwd=root, env=env, check=False)
            self.assertNotEqual(validation.returncode, 0)
            self.assertEqual([path.name for path in evidence.iterdir()], ["attempt.json"])
            attempt = json.loads((evidence / "attempt.json").read_text())
            self.assertEqual(attempt["run_attempt"], "2")
            self.assertEqual(attempt["prediction_source"], "mcp_reference_replay")
            self.assertNotIn("conclusion", attempt)

    def test_pr_evidence_is_bounded_read_only_and_advisory(self) -> None:
        workflows = pinned.ROOT.parents[1] / ".github/workflows"
        workflow = yaml.safe_load((workflows / "swebench-pr.yml").read_text())
        events = workflow.get("on", workflow.get(True))
        self.assertEqual(set(events), {"pull_request"})
        self.assertIn("benchmarks/swebench/**", events["pull_request"]["paths"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertTrue(workflow["concurrency"]["cancel-in-progress"])
        job = workflow["jobs"]["pinned-mcp-evidence"]
        self.assertEqual(job["uses"], "./.github/workflows/swebench-lite.yml")
        self.assertNotIn("secrets", job)
        self.assertNotIn("environment", job)
        self.assertEqual(job["with"], {
            "source_ref": "${{ github.event.pull_request.head.sha }}",
            "prediction_source": "mcp_reference_replay", "instance_ids": "sympy__sympy-12419",
            "max_workers": "1", "install_swebench": True, "require_evaluation_pass": True,
            "blocking": False, "timeout_minutes": 30,
        })

    def test_timeout_defaults_remain_unchanged_and_invalid_limits_fail_before_install(self) -> None:
        workflow = yaml.safe_load((pinned.ROOT.parents[1] / ".github/workflows/swebench-lite.yml").read_text())
        events = workflow.get("on", workflow.get(True))
        for event in ("workflow_dispatch", "workflow_call"):
            self.assertEqual(events[event]["inputs"]["timeout_minutes"]["default"], 180)
        job = workflow["jobs"]["swebench-lite"]
        self.assertEqual(job["timeout-minutes"], "${{ inputs.timeout_minutes || 180 }}")
        validation = job["steps"][1]["run"]
        for value, accepted in (("30", True), ("180", True), ("0", False), ("181", False), ("1.5", False)):
            with self.subTest(value=value):
                env = {**os.environ, "SOURCE_REF": "a" * 40, "PREDICTION_SOURCE": "mcp_reference_replay",
                       "TIMEOUT_MINUTES": value}
                result = subprocess.run(["bash", "-e", "-c", validation], env=env, capture_output=True, check=False)
                self.assertEqual(result.returncode == 0, accepted)


class ReplayExecutionTests(unittest.TestCase):
    def test_marker_before_empty_final_poll_is_successful(self) -> None:
        initial = {"command_id": "command", "status": "running", "exit_code": None, "stdout": "syntax-ok\n"}
        poll = Mock(side_effect=[
            {"command_id": "command", "status": "running", "exit_code": None, "stdout": ""},
            {"command_id": "command", "status": "exited", "exit_code": 0, "stdout": ""},
        ])
        replay_mcp.verify_execution(poll, initial)
        self.assertEqual(poll.call_count, 2)

    def test_marker_split_across_responses_is_successful(self) -> None:
        initial = {"command_id": "command", "status": "queued", "exit_code": None, "stdout": "syn"}
        poll = Mock(side_effect=[
            {"command_id": "command", "status": "running", "exit_code": None, "stdout": "tax-"},
            {"command_id": "command", "status": "exited", "exit_code": 0, "stdout": "ok\n"},
        ])
        replay_mcp.verify_execution(poll, initial)

    def test_marker_does_not_override_failed_exit_and_diagnostics_are_preserved(self) -> None:
        initial = {"command_id": "command", "status": "running", "exit_code": None,
                   "stdout": "syntax-ok\n", "stderr": "early warning\n"}
        poll = Mock(return_value={"command_id": "command", "status": "exited", "exit_code": 1,
                                  "stdout": "", "stderr": "late failure\n"})
        with self.assertRaises(RuntimeError) as error:
            replay_mcp.verify_execution(poll, initial)
        for message in ("syntax-ok", "early warning", "late failure", "'exit_code': 1"):
            self.assertIn(message, str(error.exception))

    def test_successful_exit_without_marker_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "MCP exec verification failed"):
            replay_mcp.verify_execution(Mock(), {"status": "exited", "exit_code": 0, "stdout": ""})


class ReplayTests(unittest.TestCase):
    def seed_previous_success(self, root: Path) -> None:
        row = pinned.replay_instance(pinned.load_pins())
        for name in ("baseline_native.jsonl", "candidate_mcp.jsonl"):
            reference.write_predictions(root / name, "historical-source", {row["instance_id"]: row["patch"]})
        (root / "report.json").write_text(json.dumps({
            "conclusion": "PASS", "runtime_source_sha": "historical-source",
            "candidate_patch_sha256": "historical-patch", "official_harness": "PASS",
            "mcp_calls": [{"tool": "historical-call"}],
        }))
        (root / "notes.txt").write_text("unrelated user notes")
        (root / "historical-official.json").write_text('{"conclusion": "PASS"}')

    def fixture_checkout(self, destination: Path, source: str, commit: str) -> None:
        pins = pinned.load_pins()
        self.assertEqual(commit, pins["replay"]["base_commit"])
        file = destination / replay_mcp.PATH
        file.parent.mkdir(parents=True)
        file.write_bytes((pinned.ROOT / pins["replay"]["source_fixture"]).read_bytes())
        replay_mcp.run(["git", "init", "--quiet"], destination)
        replay_mcp.run(["git", "add", "."], destination)
        replay_mcp.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Fixture"], destination)

    def assert_unrelated_outputs_preserved(self, root: Path) -> None:
        self.assertEqual((root / "notes.txt").read_text(), "unrelated user notes")
        self.assertEqual(json.loads((root / "historical-official.json").read_text()), {"conclusion": "PASS"})

    def test_line_edit_uses_current_revision_and_unique_context(self) -> None:
        content = "header\n" + replay_mcp.OLD_ENTRY + "\nfooter\n"
        change = replay_mcp.line_edit(content, "revision")["changes"][0]
        self.assertEqual(change["revision"], "revision")
        self.assertEqual(change["edits"][0]["start_line"], 2)
        self.assertEqual(change["edits"][0]["end_line"], 6)
        with self.assertRaises(ValueError):
            replay_mcp.line_edit(content + content, "revision")

    def test_actual_http_mcp_replay_matches_native_patch_offline(self) -> None:
        pins = pinned.load_pins()
        row = pinned.replay_instance(pins)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, workspace = root / "native", root / "candidate"
            for checkout in (native, workspace):
                file = checkout / replay_mcp.PATH
                file.parent.mkdir(parents=True)
                file.write_bytes((pinned.ROOT / pins["replay"]["source_fixture"]).read_bytes())
                replay_mcp.run(["git", "init", "--quiet"], checkout)
                replay_mcp.run(["git", "add", "."], checkout)
                replay_mcp.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Fixture"], checkout)
            replay_mcp.run(["git", "apply", "-"], native, stdin=row["patch"])
            expected = (native / replay_mcp.PATH).read_text()
            self.assertEqual(pinned.sha256(expected.encode()), pins["replay"]["result_sha256"])
            calls: list[dict] = []
            actual = replay_mcp.replay(workspace, expected, root / "raw", calls)
            self.assertEqual(actual, replay_mcp.run(["git", "diff", "--unified=3"], native))
            self.assertEqual([call["tool"] for call in calls][:7],
                             ["read_file", "apply_patch", "read_file", "apply_changes", "read_file", "git_diff", "exec_command"])
            self.assertTrue((root / "raw/mcp-transcript.json").is_file())

    def test_failed_replay_removes_stale_prediction_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(replay_mcp, "replay_instance", side_effect=ValueError("broken fixture")):
            root = Path(tmp)
            for name in ("baseline_native.jsonl", "candidate_mcp.jsonl"):
                (root / name).write_text("stale")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(replay_mcp.main(["--output-dir", str(root)]), 1)
            self.assertFalse((root / "candidate_mcp.jsonl").exists())
            self.assertEqual(json.loads((root / "report.json").read_text())["conclusion"], "FAIL")

    def test_incomplete_report_is_persisted_before_prediction_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.seed_previous_success(root)
            unlink = Path.unlink

            def inspect_before_unlink(path: Path, missing_ok: bool = False) -> None:
                if path.name in {"baseline_native.jsonl", "candidate_mcp.jsonl"}:
                    current = json.loads((root / "report.json").read_text())
                    self.assertEqual(current["conclusion"], "INCONCLUSIVE")
                    self.assertNotIn("runtime_source_sha", current)
                    self.assertNotIn("candidate_patch_sha256", current)
                    self.assertEqual(current["official_harness"], "NOT_RUN")
                    self.assertEqual(current["mcp_calls"], [])
                unlink(path, missing_ok=missing_ok)

            with patch.object(Path, "unlink", inspect_before_unlink), patch.object(replay_mcp, "load_pins", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    replay_mcp.main(["--output-dir", str(root)])
            self.assert_unrelated_outputs_preserved(root)

    def test_failed_or_interrupted_rerun_cannot_reuse_prior_success(self) -> None:
        for stage in ("load_pins", "checkout", "replay", "write_predictions"):
            for error in (KeyboardInterrupt, RuntimeError):
                with self.subTest(stage=stage, error=error), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.seed_previous_success(root)
                    writes = 0

                    def replay_fixture(workspace: Path, expected: str, raw_dir: Path, calls: list[dict]) -> str:
                        calls.append({"tool": "current-call"})
                        if stage == "replay":
                            raise error("stopped replay")
                        (workspace / replay_mcp.PATH).write_text(expected)
                        return replay_mcp.run(["git", "diff", "--unified=3"], workspace)

                    def write_prediction(path: Path, model: str, patches: dict[str, str]) -> None:
                        nonlocal writes
                        writes += 1
                        reference.write_predictions(path, model, patches)
                        if stage == "write_predictions" and writes == 2:
                            raise error("stopped prediction write")

                    with contextlib.ExitStack() as stack:
                        stack.enter_context(patch.object(replay_mcp, "checkout", side_effect=self.fixture_checkout))
                        stack.enter_context(patch.object(replay_mcp, "replay", side_effect=replay_fixture))
                        stack.enter_context(patch.object(replay_mcp, "write_predictions", side_effect=write_prediction))
                        if stage in {"load_pins", "checkout"}:
                            stack.enter_context(patch.object(replay_mcp, stage, side_effect=error("stopped preflight")))
                        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                        if error is KeyboardInterrupt:
                            with self.assertRaises(KeyboardInterrupt):
                                replay_mcp.main(["--output-dir", str(root)])
                        else:
                            self.assertEqual(replay_mcp.main(["--output-dir", str(root)]), 1)
                    current = json.loads((root / "report.json").read_text())
                    self.assertEqual(current["conclusion"], "INCONCLUSIVE" if error is KeyboardInterrupt else "FAIL")
                    self.assertNotEqual(current.get("runtime_source_sha"), "historical-source")
                    self.assertNotIn("candidate_patch_sha256", current)
                    self.assertNotIn("candidate_predictions", current)
                    self.assertEqual(current["official_harness"], "NOT_RUN")
                    self.assertNotIn({"tool": "historical-call"}, current["mcp_calls"])
                    for name in ("baseline_native.jsonl", "candidate_mcp.jsonl"):
                        path = root / name
                        self.assertFalse(path.exists())
                        self.assertTrue(run_smoke.validate_predictions(path, {"sympy__sympy-12419"}).errors)
                    self.assert_unrelated_outputs_preserved(root)

    def test_successful_rerun_reports_only_current_matching_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.seed_previous_success(root)
            with patch.object(replay_mcp, "checkout", side_effect=self.fixture_checkout), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(replay_mcp.main(["--output-dir", str(root)]), 0)
            current = json.loads((root / "report.json").read_text())
            self.assertEqual(current["conclusion"], "PASS")
            self.assertEqual(current["runtime_source_sha"], replay_mcp.run(["git", "rev-parse", "HEAD"], replay_mcp.REPO_ROOT).strip())
            self.assertEqual(current["official_harness"], "NOT_RUN")
            self.assertTrue(current["native_diff_matches_mcp"])
            self.assertNotIn({"tool": "historical-call"}, current["mcp_calls"])
            predictions = []
            for field in ("baseline_predictions", "candidate_predictions"):
                path = Path(current[field])
                self.assertFalse(run_smoke.validate_predictions(path, {current["instance_id"]}).errors)
                prediction = json.loads(path.read_text())
                self.assertEqual(prediction["instance_id"], current["instance_id"])
                self.assertEqual(pinned.sha256(prediction["model_patch"].encode()), current["candidate_patch_sha256"])
                predictions.append(prediction["model_patch"])
            self.assertEqual(*predictions)
            self.assert_unrelated_outputs_preserved(root)

    def test_failed_report_publication_cannot_leave_predictions(self) -> None:
        for fail_on, after_replace in ((1, False), (1, True), (2, False), (2, True)):
            for error in (KeyboardInterrupt, OSError):
                with self.subTest(fail_on=fail_on, after_replace=after_replace, error=error), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.seed_previous_success(root)
                    replace = Path.replace
                    publications = 0

                    def publish(path: Path, target: Path) -> Path:
                        nonlocal publications
                        publications += 1
                        if publications == fail_on:
                            if after_replace:
                                replace(path, target)
                            raise error("stopped report publication")
                        return replace(path, target)

                    with contextlib.ExitStack() as stack:
                        stack.enter_context(patch.object(replay_mcp, "checkout", side_effect=self.fixture_checkout))
                        stack.enter_context(patch.object(Path, "replace", publish))
                        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                        if error is KeyboardInterrupt:
                            with self.assertRaises(KeyboardInterrupt):
                                replay_mcp.main(["--output-dir", str(root)])
                        else:
                            self.assertEqual(replay_mcp.main(["--output-dir", str(root)]), 1)
                    current = json.loads((root / "report.json").read_text())
                    self.assertEqual(current["conclusion"], "INCONCLUSIVE" if error is KeyboardInterrupt else "FAIL")
                    self.assertNotIn("candidate_predictions", current)
                    self.assertNotIn("candidate_patch_sha256", current)
                    if fail_on == 1:
                        self.assertNotIn("runtime_source_sha", current)
                    else:
                        self.assertEqual(current["runtime_source_sha"], replay_mcp.run(["git", "rev-parse", "HEAD"], replay_mcp.REPO_ROOT).strip())
                    for name in ("baseline_native.jsonl", "candidate_mcp.jsonl"):
                        self.assertFalse((root / name).exists())
                    self.assertFalse(list(root.glob(".replay-report-*")))
                    self.assert_unrelated_outputs_preserved(root)

    def test_unwritable_report_preserves_previous_complete_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.seed_previous_success(root)
            previous = {path: path.read_bytes() for path in root.iterdir()}
            with patch.object(Path, "replace", side_effect=OSError("report unavailable")):
                with self.assertRaisesRegex(OSError, "report unavailable"):
                    replay_mcp.main(["--output-dir", str(root)])
            self.assertEqual({path: path.read_bytes() for path in root.iterdir()}, previous)


if __name__ == "__main__":
    unittest.main()
