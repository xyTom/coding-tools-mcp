#!/usr/bin/env python3
"""Preflight and optionally run the SWE-bench Lite smoke regression."""

from __future__ import annotations

import argparse
import importlib.util
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.swebench.pinned import dataset_path, docker_image, load_pins  # noqa: E402


@dataclass
class PredictionSet:
    path: Path
    count: int
    instance_ids: list[str]
    model_names: list[str]
    placeholder: bool
    errors: list[str]


def load_subset(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def selected_instances(subset: dict[str, Any], requested: list[str]) -> list[dict[str, Any]]:
    instances = [item for item in subset.get("instances", []) if isinstance(item, dict)]
    if not requested:
        return instances
    wanted = set(requested)
    missing = wanted - {item.get("instance_id") for item in instances}
    if missing:
        raise ValueError(f"instance IDs are not in pinned subset: {', '.join(sorted(missing))}")
    return [item for item in instances if item.get("instance_id") in wanted]


def validate_predictions(path: Path, expected_ids: set[str]) -> PredictionSet:
    errors: list[str] = []
    ids: list[str] = []
    patches: list[str] = []
    model_names: set[str] = set()
    if not path.exists():
        return PredictionSet(path, 0, [], [], True, [f"{path} does not exist"])
    if not expected_ids:
        errors.append("no benchmark instances selected")
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_no}: invalid JSON: {exc}")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {line_no}: prediction must be an object")
            continue
        for key in ("instance_id", "model_name_or_path", "model_patch"):
            if not isinstance(row.get(key), str):
                errors.append(f"line {line_no}: {key} must be a string")
        instance_id = row.get("instance_id")
        if isinstance(instance_id, str) and instance_id in expected_ids:
            if instance_id in ids:
                errors.append(f"line {line_no}: duplicate prediction for {instance_id}")
            ids.append(instance_id)
            patch = row.get("model_patch")
            patches.append(patch if isinstance(patch, str) else "")
            model_name = row.get("model_name_or_path")
            if isinstance(model_name, str) and model_name and model_name not in {".", ".."}:
                model_names.add(model_name)
            else:
                errors.append(f"line {line_no}: invalid model_name_or_path")
    missing = sorted(expected_ids - set(ids))
    if missing:
        errors.append(f"missing predictions for: {', '.join(missing)}")
    if not model_names:
        errors.append("predictions must name a model or replay source")
    return PredictionSet(path, len(ids), ids, sorted(model_names), not patches or any(not patch.strip() for patch in patches), errors)


def capture(command: list[str], raw_dir: Path, name: str, *, timeout: int = 120) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        output = {
            "ran": True,
            "returncode": result.returncode,
            "stdout": result.stdout[-12000:],
            "stderr": result.stderr[-12000:],
            "command": command,
        }
    except Exception as exc:  # pragma: no cover - environment dependent
        output = {"ran": False, "returncode": None, "stdout": "", "stderr": repr(exc), "command": command}
    (raw_dir / f"{name}.json").write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (raw_dir / f"{name}.stdout.txt").write_text(str(output["stdout"]), encoding="utf-8")
    (raw_dir / f"{name}.stderr.txt").write_text(str(output["stderr"]), encoding="utf-8")
    return output


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def capture_environment(raw_dir: Path) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    output = {
        "cwd": str(Path.cwd()),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "packages": {
            "swebench": package_version("swebench"),
            "docker": package_version("docker"),
            "datasets": package_version("datasets"),
        },
        "executables": {
            "docker": shutil.which("docker"),
            "git": shutil.which("git"),
        },
        "github": {
            "actions": os.environ.get("GITHUB_ACTIONS"),
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "sha": os.environ.get("GITHUB_SHA"),
            "ref": os.environ.get("GITHUB_REF"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "runner_os": os.environ.get("RUNNER_OS"),
        },
    }
    (raw_dir / "environment.json").write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def check_docker(raw_dir: Path) -> tuple[bool, str, dict[str, Any]]:
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker executable not found", {"ran": False, "returncode": None, "stdout": "", "stderr": ""}
    result = capture([docker, "version"], raw_dir, "docker-version", timeout=20)
    if result["returncode"] != 0:
        detail = (str(result["stderr"]) or str(result["stdout"])).strip()
        return False, f"docker daemon unavailable: {detail[:500]}", result
    return True, "docker version succeeded", result


def check_swebench(raw_dir: Path, *, install: bool) -> tuple[bool, str, dict[str, Any] | None, dict[str, Any]]:
    install_result: dict[str, Any] | None = None
    required = load_pins()["swebench_version"]
    if install and package_version("swebench") != required:
        install_result = capture([sys.executable, "-m", "pip", "install", f"swebench=={required}"], raw_dir, "pip-install-swebench", timeout=900)
    if package_version("swebench") != required:
        detail = f"swebench=={required} required; installed={package_version('swebench')}"
        return False, detail, install_result, {"ran": False, "returncode": None, "stdout": "", "stderr": detail}
    help_result = capture([sys.executable, "-m", "swebench.harness.run_evaluation", "--help"], raw_dir, "swebench-help", timeout=120)
    if help_result["returncode"] != 0:
        if importlib.util.find_spec("swebench") is None:
            return False, "Python package swebench is not installed or not importable", install_result, help_result
        return False, "swebench harness help/import failed", install_result, help_result
    return True, "swebench harness help succeeded", install_result, help_result


def evaluation_command(predictions: Path, run_id: str, max_workers: int, instance_ids: list[str]) -> list[str]:
    pins = load_pins()
    command = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        str(dataset_path(pins)),
        "--split",
        pins["dataset"]["split"],
        "--namespace",
        pins["docker"]["namespace"],
        "--instance_image_tag",
        pins["docker"]["instance_image_tag"],
        "--cache_level",
        "instance",
        "--predictions_path",
        str(predictions),
        "--max_workers",
        str(max_workers),
        "--run_id",
        run_id,
    ]
    if instance_ids:
        command.append("--instance_ids")
        command.extend(instance_ids)
    return command


def prepare_images(instance_ids: list[str], raw_dir: Path) -> tuple[bool, list[dict[str, Any]]]:
    """Pull immutable digests, then alias locally for the harness's tag-only API."""
    pins = load_pins()
    evidence: list[dict[str, Any]] = []
    for instance_id in instance_ids:
        source, target = docker_image(pins, instance_id)
        commands = (["docker", "pull", "--platform", "linux/amd64", source],
                    ["docker", "tag", source, target],
                    ["docker", "image", "inspect", "--format", "{{.Id}}", source, target])
        for index, command in enumerate(commands):
            result = capture(command, raw_dir, f"image-{instance_id}-{index}", timeout=1200)
            evidence.append(result)
            if result["returncode"] != 0:
                return False, evidence
            if index == 2:
                ids = str(result["stdout"]).splitlines()
                if len(ids) != 2 or ids[0] != ids[1] or not ids[0].startswith("sha256:"):
                    return False, evidence
    return True, evidence


def comparison_conclusion(baseline: dict[str, Any], candidate: dict[str, Any], expected: int) -> str:
    if expected < 1 or baseline.get("completed") != expected or candidate.get("completed") != expected:
        return "INCONCLUSIVE"
    native, mcp = baseline.get("resolved"), candidate.get("resolved")
    if not isinstance(native, int) or not isinstance(mcp, int):
        return "INCONCLUSIVE"
    # Two empty/failed controls must never become a green advisory result.
    return "PASS" if native > 0 and mcp >= native else "FAIL"


def maybe_run(command: list[str], enabled: bool, raw_dir: Path, name: str) -> dict[str, Any]:
    if not enabled:
        return {"ran": False, "returncode": None, "stdout": "", "stderr": "", "command": command}
    return capture(command, raw_dir, name, timeout=7200)


def safe_name(value: str) -> str:
    return value.replace("/", "__")


def copy_if_exists(source: Path, destination: Path) -> str | None:
    if not source.exists():
        return None
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return str(destination)


def collect_harness_artifacts(raw_dir: Path, run_id: str, label: str) -> list[str]:
    copied: list[str] = []
    sources = [(Path("logs/run_evaluation") / run_id, raw_dir / f"{label}-logs-run_evaluation")]
    # Official 4.1.0 summary names end in the run ID. Never copy a shared
    # evaluation_results directory that may contain stale unrelated runs.
    sources.extend((source, raw_dir / f"{label}-{source.name}") for source in Path.cwd().glob(f"*.{run_id}.json"))
    for source, destination in sources:
        copied_path = copy_if_exists(source, destination)
        if copied_path is not None:
            copied.append(copied_path)
    return copied


def parse_resolved_count(run_id: str, model_names: list[str], expected_ids: set[str]) -> dict[str, Any]:
    report_paths: list[str] = []
    seen: dict[str, bool] = {}
    for model_name in model_names:
        report_root = Path("logs/run_evaluation") / run_id / safe_name(model_name)
        for report_path in sorted(report_root.glob("*/report.json")):
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            report_paths.append(str(report_path))
            if not isinstance(report, dict):
                continue
            for instance_id in expected_ids:
                row = report.get(instance_id)
                if isinstance(row, dict) and isinstance(row.get("resolved"), bool):
                    seen[instance_id] = row["resolved"]

    resolved = sum(1 for value in seen.values() if value)
    return {
        "resolved": resolved if seen else None,
        "completed": len(seen),
        "expected": len(expected_ids),
        "report_paths": report_paths,
        "resolved_ids": sorted(instance_id for instance_id, value in seen.items() if value),
        "unresolved_ids": sorted(instance_id for instance_id, value in seen.items() if not value),
        "missing_report_ids": sorted(expected_ids - set(seen)),
    }


def write_reports(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# SWE-bench Smoke Regression Report",
        "",
        f"- Conclusion: **{report['conclusion']}**",
        f"- Prediction source: `{report.get('prediction_source', 'checked_in')}` (advisory only)",
        f"- Dataset: `{report['dataset_name']}` split `{report['split']}`",
        f"- Smoke subset: `{report['subset_path']}`",
        f"- Raw log directory: `{report['raw_dir']}`",
        f"- Baseline predictions: `{report['baseline']['path']}`",
        f"- Candidate predictions: `{report['candidate']['path']}`",
        f"- Baseline resolved: `{report['baseline'].get('resolved')}`",
        f"- Candidate resolved: `{report['candidate'].get('resolved')}`",
        f"- Baseline completed: `{report['baseline'].get('completed')}` / `{report['baseline'].get('expected')}`",
        f"- Candidate completed: `{report['candidate'].get('completed')}` / `{report['candidate'].get('expected')}`",
        "",
        "## Preflight",
        "",
    ]
    for item in report.get("preflight", []):
        lines.append(f"- {item}")
    lines.extend(["", "## Instances", ""])
    for instance in report.get("instances", []):
        lines.append(f"- `{instance['instance_id']}` ({instance.get('project', 'unknown')})")
    lines.extend(["", "## Evaluation Commands", ""])
    lines.append("```bash")
    lines.append(" ".join(report["baseline"]["command"]))
    lines.append(" ".join(report["candidate"]["command"]))
    lines.append("```")
    lines.extend(["", "## Harness Reports", ""])
    for label in ("baseline", "candidate"):
        lines.append(f"### {label.title()}")
        for path in report[label].get("report_paths", []):
            lines.append(f"- `{path}`")
        for path in report[label].get("artifacts", []):
            lines.append(f"- `{path}`")
        if not report[label].get("report_paths") and not report[label].get("artifacts"):
            lines.append("- No harness report artifacts captured.")
    lines.extend(["", "## Limitations", ""])
    for item in report.get("limitations", []):
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", type=Path, default=BENCHMARK_ROOT / "swebench/subsets/smoke-lite-10.json")
    parser.add_argument(
        "--baseline-predictions",
        type=Path,
        default=BENCHMARK_ROOT / "swebench/predictions/baseline_native.jsonl",
    )
    parser.add_argument(
        "--candidate-predictions",
        type=Path,
        default=BENCHMARK_ROOT / "swebench/predictions/candidate_mcp.jsonl",
    )
    parser.add_argument("--report-json", type=Path, default=Path("reports/benchmark/swebench-regression.json"))
    parser.add_argument("--report-md", type=Path, default=Path("reports/benchmark/swebench-regression.md"))
    parser.add_argument("--raw-dir", type=Path, help="Parent directory for this attempt's unique raw-log subdirectory")
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--prediction-source", choices=("reference_patch", "mcp_reference_replay", "checked_in"), default="checked_in")
    parser.add_argument("--run-evaluation", action="store_true")
    parser.add_argument("--install-swebench", action="store_true")
    parser.add_argument("--allow-placeholder-evaluation", action="store_true")
    parser.add_argument(
        "--require-evaluation-pass",
        action="store_true",
        help="Exit nonzero unless official evaluation runs and candidate resolved count is >= baseline.",
    )
    args = parser.parse_args(argv)

    raw_parent = args.raw_dir or args.report_json.parent / args.report_json.stem / "raw"
    # Preserve previous diagnostics, but never mix them into this run's evidence.
    args.raw_dir = raw_parent / uuid.uuid4().hex
    # Replace any old PASS before pin/subset reads or other fallible preflight.
    # An interrupted run must also leave a current, explicitly incomplete report.
    incomplete: dict[str, Any] = {
        "conclusion": "INCONCLUSIVE", "prediction_source": args.prediction_source,
        "advisory_only": True, "dataset_name": "not validated", "split": "not validated",
        "subset_path": str(args.subset), "raw_dir": str(args.raw_dir), "instances": [],
        "preflight": [], "limitations": ["This attempt has not completed preflight or evaluation."],
        "baseline": {"path": str(args.baseline_predictions), "command": []},
        "candidate": {"path": str(args.candidate_predictions), "command": []},
    }
    write_reports(incomplete, args.report_json, args.report_md)
    try:
        return run_attempt(args)
    except Exception as exc:
        incomplete["conclusion"] = "ERROR"
        incomplete["limitations"] = [f"Preflight or evaluation failed: {type(exc).__name__}: {exc}"]
        write_reports(incomplete, args.report_json, args.report_md)
        print(incomplete["limitations"][0], file=sys.stderr)
        return 1


def run_attempt(args: argparse.Namespace) -> int:
    raw_dir = args.raw_dir
    pins = load_pins()
    dataset_path(pins)  # Fail closed on fixture corruption even for preflight.
    if args.max_workers < 1:
        raise ValueError("--max-workers must be positive")
    subset = load_subset(args.subset)
    instances = selected_instances(subset, args.instance_id)
    expected_ids = {str(item["instance_id"]) for item in instances}
    baseline = validate_predictions(args.baseline_predictions, expected_ids)
    candidate = validate_predictions(args.candidate_predictions, expected_ids)
    docker_ok, docker_detail, docker_run = check_docker(raw_dir)
    swebench_ok, swebench_detail, install_run, help_run = check_swebench(raw_dir, install=args.install_swebench)
    environment = capture_environment(raw_dir)
    suffix = uuid.uuid4().hex
    baseline_id, candidate_id = f"native_{suffix}", f"mcp_{suffix}"
    baseline_command = evaluation_command(
        args.baseline_predictions,
        baseline_id,
        args.max_workers,
        sorted(expected_ids),
    )
    candidate_command = evaluation_command(
        args.candidate_predictions,
        candidate_id,
        args.max_workers,
        sorted(expected_ids),
    )

    limitations: list[str] = []
    if args.prediction_source != "checked_in":
        limitations.append("Reference-derived smoke evidence only; not a model-generated benchmark comparison.")
    preflight = [
        f"docker: {'ok' if docker_ok else 'missing'} - {docker_detail}",
        f"swebench package: {'ok' if swebench_ok else 'missing'} - {swebench_detail}",
        f"baseline predictions: {baseline.count} rows, placeholder={baseline.placeholder}",
        f"candidate predictions: {candidate.count} rows, placeholder={candidate.placeholder}",
    ]
    for prediction_set in (baseline, candidate):
        for error in prediction_set.errors:
            limitations.append(f"{prediction_set.path}: {error}")
    if baseline.placeholder or candidate.placeholder:
        limitations.append("Selected predictions include empty patches and are not eligible for a valid benchmark comparison.")
    if not docker_ok:
        limitations.append("Official SWE-bench evaluation requires a working Docker daemon.")
    if not swebench_ok:
        limitations.append("Official SWE-bench evaluation requires an importable swebench harness.")

    can_run = (
        args.run_evaluation
        and docker_ok
        and swebench_ok
        and not baseline.errors
        and not candidate.errors
        and (args.allow_placeholder_evaluation or (not baseline.placeholder and not candidate.placeholder))
    )
    if args.run_evaluation and not can_run:
        limitations.append("Evaluation was requested but preflight/resource checks prevent a valid comparison.")

    images_ok, image_runs = prepare_images(sorted(expected_ids), raw_dir) if can_run else (False, [])
    if can_run and not images_ok:
        limitations.append("Pinned Docker image pull/tag verification failed; evaluation was not run.")
        can_run = False
    baseline_run = maybe_run(baseline_command, can_run, raw_dir, "baseline-evaluation")
    candidate_run = maybe_run(candidate_command, can_run, raw_dir, "candidate-evaluation")
    baseline_artifacts = collect_harness_artifacts(raw_dir, baseline_id, "baseline") if can_run else []
    candidate_artifacts = collect_harness_artifacts(raw_dir, candidate_id, "candidate") if can_run else []
    baseline_counts = parse_resolved_count(baseline_id, baseline.model_names, expected_ids) if can_run else {}
    candidate_counts = parse_resolved_count(candidate_id, candidate.model_names, expected_ids) if can_run else {}
    if can_run and baseline_run["returncode"] == 0 and candidate_run["returncode"] == 0:
        conclusion = comparison_conclusion(baseline_counts, candidate_counts, len(expected_ids))
        if conclusion == "INCONCLUSIVE":
            limitations.append("Harness reports are incomplete; every selected instance requires a fresh report.")
    elif can_run:
        conclusion = "FAIL"
    elif args.run_evaluation:
        conclusion = "BLOCKED"
    else:
        conclusion = "PREFLIGHT_ONLY"

    report = {
        "conclusion": conclusion,
        "prediction_source": args.prediction_source,
        "advisory_only": True,
        "pins": pins,
        "image_preparation": image_runs,
        "run_ids": {"baseline": baseline_id, "candidate": candidate_id},
        "dataset_name": subset.get("dataset_name"),
        "split": subset.get("split"),
        "subset_path": str(args.subset),
        "raw_dir": str(raw_dir),
        "instances": instances,
        "preflight": preflight,
        "limitations": limitations,
        "environment": environment,
        "docker": docker_run,
        "swebench_install": install_run,
        "swebench_help": help_run,
        "baseline": {
            "path": str(args.baseline_predictions),
            "count": baseline.count,
            "model_names": baseline.model_names,
            "placeholder": baseline.placeholder,
            "errors": baseline.errors,
            "resolved": baseline_counts.get("resolved"),
            "completed": baseline_counts.get("completed"),
            "expected": baseline_counts.get("expected"),
            "report_paths": baseline_counts.get("report_paths", []),
            "resolved_ids": baseline_counts.get("resolved_ids", []),
            "unresolved_ids": baseline_counts.get("unresolved_ids", []),
            "missing_report_ids": baseline_counts.get("missing_report_ids", []),
            "artifacts": baseline_artifacts,
            "command": baseline_command,
            "run": baseline_run,
        },
        "candidate": {
            "path": str(args.candidate_predictions),
            "count": candidate.count,
            "model_names": candidate.model_names,
            "placeholder": candidate.placeholder,
            "errors": candidate.errors,
            "resolved": candidate_counts.get("resolved"),
            "completed": candidate_counts.get("completed"),
            "expected": candidate_counts.get("expected"),
            "report_paths": candidate_counts.get("report_paths", []),
            "resolved_ids": candidate_counts.get("resolved_ids", []),
            "unresolved_ids": candidate_counts.get("unresolved_ids", []),
            "missing_report_ids": candidate_counts.get("missing_report_ids", []),
            "artifacts": candidate_artifacts,
            "command": candidate_command,
            "run": candidate_run,
        },
    }
    write_reports(report, args.report_json, args.report_md)
    if args.require_evaluation_pass and conclusion != "PASS":
        return 1
    return 0 if conclusion != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
