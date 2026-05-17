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
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ROOT = Path(__file__).resolve().parents[1]


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
    return [item for item in instances if item.get("instance_id") in wanted]


def validate_predictions(path: Path, expected_ids: set[str]) -> PredictionSet:
    errors: list[str] = []
    ids: list[str] = []
    patches: list[str] = []
    model_names: set[str] = set()
    if not path.exists():
        return PredictionSet(path, 0, [], [], True, [f"{path} does not exist"])
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_no}: invalid JSON: {exc}")
            continue
        for key in ("instance_id", "model_name_or_path", "model_patch"):
            if key not in row:
                errors.append(f"line {line_no}: missing {key}")
        instance_id = row.get("instance_id")
        if isinstance(instance_id, str) and instance_id in expected_ids:
            ids.append(instance_id)
            patch = row.get("model_patch")
            patches.append(patch if isinstance(patch, str) else "")
            model_name = row.get("model_name_or_path")
            if isinstance(model_name, str) and model_name:
                model_names.add(model_name)
    missing = sorted(expected_ids - set(ids))
    if missing:
        errors.append(f"missing predictions for: {', '.join(missing)}")
    return PredictionSet(path, len(ids), ids, sorted(model_names), all(not patch.strip() for patch in patches), errors)


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
    if install and importlib.util.find_spec("swebench") is None:
        install_result = capture([sys.executable, "-m", "pip", "install", "swebench"], raw_dir, "pip-install-swebench", timeout=900)
    help_result = capture([sys.executable, "-m", "swebench.harness.run_evaluation", "--help"], raw_dir, "swebench-help", timeout=120)
    if help_result["returncode"] != 0:
        if importlib.util.find_spec("swebench") is None:
            return False, "Python package swebench is not installed or not importable", install_result, help_result
        return False, "swebench harness help/import failed", install_result, help_result
    return True, "swebench harness help succeeded", install_result, help_result


def evaluation_command(predictions: Path, run_id: str, max_workers: int, instance_ids: list[str]) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        "princeton-nlp/SWE-bench_Lite",
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
    for source, destination in (
        (Path("logs/run_evaluation") / run_id, raw_dir / f"{label}-logs-run_evaluation"),
        (Path("evaluation_results"), raw_dir / f"{label}-evaluation_results"),
    ):
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
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--run-evaluation", action="store_true")
    parser.add_argument("--install-swebench", action="store_true")
    parser.add_argument("--allow-placeholder-evaluation", action="store_true")
    parser.add_argument(
        "--require-evaluation-pass",
        action="store_true",
        help="Exit nonzero unless official evaluation runs and candidate resolved count is >= baseline.",
    )
    args = parser.parse_args(argv)

    raw_dir = args.raw_dir
    if raw_dir is None:
        raw_dir = args.report_json.parent / args.report_json.stem / "raw"

    subset = load_subset(args.subset)
    instances = selected_instances(subset, args.instance_id)
    expected_ids = {str(item["instance_id"]) for item in instances}
    baseline = validate_predictions(args.baseline_predictions, expected_ids)
    candidate = validate_predictions(args.candidate_predictions, expected_ids)
    docker_ok, docker_detail, docker_run = check_docker(raw_dir)
    swebench_ok, swebench_detail, install_run, help_run = check_swebench(raw_dir, install=args.install_swebench)
    environment = capture_environment(raw_dir)
    baseline_command = evaluation_command(
        args.baseline_predictions,
        "codex_tool_runtime_native_smoke",
        args.max_workers,
        sorted(expected_ids),
    )
    candidate_command = evaluation_command(
        args.candidate_predictions,
        "codex_tool_runtime_mcp_smoke",
        args.max_workers,
        sorted(expected_ids),
    )

    limitations: list[str] = []
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
        limitations.append("Prediction files are schema-valid placeholders, not model-generated patches.")
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

    baseline_run = maybe_run(baseline_command, can_run, raw_dir, "baseline-evaluation")
    candidate_run = maybe_run(candidate_command, can_run, raw_dir, "candidate-evaluation")
    baseline_artifacts = collect_harness_artifacts(raw_dir, "codex_tool_runtime_native_smoke", "baseline") if can_run else []
    candidate_artifacts = collect_harness_artifacts(raw_dir, "codex_tool_runtime_mcp_smoke", "candidate") if can_run else []
    baseline_counts = parse_resolved_count("codex_tool_runtime_native_smoke", baseline.model_names, expected_ids) if can_run else {}
    candidate_counts = parse_resolved_count("codex_tool_runtime_mcp_smoke", candidate.model_names, expected_ids) if can_run else {}
    if can_run and baseline_run["returncode"] == 0 and candidate_run["returncode"] == 0:
        baseline_resolved = baseline_counts.get("resolved")
        candidate_resolved = candidate_counts.get("resolved")
        if isinstance(baseline_resolved, int) and isinstance(candidate_resolved, int):
            conclusion = "PASS" if candidate_resolved >= baseline_resolved else "FAIL"
        else:
            conclusion = "INCONCLUSIVE"
            limitations.append("Harness ran, but resolved counts could not be parsed from report.json files.")
    elif can_run:
        conclusion = "FAIL"
    elif args.run_evaluation:
        conclusion = "BLOCKED"
    else:
        conclusion = "PREFLIGHT_ONLY"

    report = {
        "conclusion": conclusion,
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
