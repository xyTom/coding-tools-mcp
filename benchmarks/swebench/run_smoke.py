#!/usr/bin/env python3
"""Preflight and optionally run the SWE-bench Lite smoke regression."""

from __future__ import annotations

import argparse
import importlib.util
import json
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
    if not path.exists():
        return PredictionSet(path, 0, [], True, [f"{path} does not exist"])
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
    missing = sorted(expected_ids - set(ids))
    if missing:
        errors.append(f"missing predictions for: {', '.join(missing)}")
    return PredictionSet(path, len(ids), ids, all(not patch.strip() for patch in patches), errors)


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
    for instance_id in instance_ids:
        command.extend(["--instance_ids", instance_id])
    return command


def maybe_run(command: list[str], enabled: bool, raw_dir: Path, name: str) -> dict[str, Any]:
    if not enabled:
        return {"ran": False, "returncode": None, "stdout": "", "stderr": "", "command": command}
    return capture(command, raw_dir, name, timeout=7200)


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
    if can_run and baseline_run["returncode"] == 0 and candidate_run["returncode"] == 0:
        conclusion = "INCONCLUSIVE"
        limitations.append("Harness ran, but resolved-count parsing is not implemented in this scaffold.")
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
        "docker": docker_run,
        "swebench_install": install_run,
        "swebench_help": help_run,
        "baseline": {
            "path": str(args.baseline_predictions),
            "count": baseline.count,
            "placeholder": baseline.placeholder,
            "errors": baseline.errors,
            "resolved": None,
            "command": baseline_command,
            "run": baseline_run,
        },
        "candidate": {
            "path": str(args.candidate_predictions),
            "count": candidate.count,
            "placeholder": candidate.placeholder,
            "errors": candidate.errors,
            "resolved": None,
            "command": candidate_command,
            "run": candidate_run,
        },
    }
    write_reports(report, args.report_json, args.report_md)
    return 0 if conclusion != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
