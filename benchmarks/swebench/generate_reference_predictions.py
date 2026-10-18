#!/usr/bin/env python3
"""Generate SWE-bench prediction JSONL files from reference patches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.swebench.pinned import dataset_path, load_instances, load_pins  # noqa: E402


def load_subset(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def selected_instance_ids(subset: dict[str, Any], requested: list[str]) -> list[str]:
    instances = [item for item in subset.get("instances", []) if isinstance(item, dict)]
    available = [str(item["instance_id"]) for item in instances if isinstance(item.get("instance_id"), str)]
    if not requested:
        return available
    missing = sorted(set(requested) - set(available))
    if missing:
        raise SystemExit(f"instance IDs are not in subset {path_display(missing)}")
    return requested


def path_display(values: list[str]) -> str:
    return ", ".join(values)


def fetch_reference_patches(dataset_name: str, split: str, instance_ids: list[str]) -> dict[str, str]:
    """Read verified fixtures; never query a floating datasets-server revision."""
    pins = load_pins()
    if dataset_name != pins["dataset"]["name"] or split != pins["dataset"]["split"]:
        raise ValueError("requested dataset does not match pinned fixture")
    rows = {row["instance_id"]: row for row in load_instances(pins)}
    missing = sorted(set(instance_ids) - set(rows))
    if missing:
        raise ValueError(f"reference patches not pinned for: {path_display(missing)}")
    return {instance_id: rows[instance_id]["patch"] for instance_id in instance_ids}


def write_predictions(path: Path, model_name: str, patches: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": patch,
        }
        for instance_id, patch in patches.items()
    ]
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", type=Path, default=Path("benchmarks/swebench/subsets/smoke-lite-10.json"))
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--baseline-output", type=Path, required=True)
    parser.add_argument("--candidate-output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path)
    parser.add_argument("--baseline-model-name", default="baseline_native_reference_patch")
    parser.add_argument("--candidate-model-name", default="candidate_reference_patch_harness_control")
    args = parser.parse_args(argv)

    subset = load_subset(args.subset)
    dataset_name = str(subset.get("dataset_name", "princeton-nlp/SWE-bench_Lite"))
    split = str(subset.get("split", "test"))
    instance_ids = selected_instance_ids(subset, args.instance_id)
    patches = fetch_reference_patches(dataset_name, split, instance_ids)
    write_predictions(args.baseline_output, args.baseline_model_name, patches)
    write_predictions(args.candidate_output, args.candidate_model_name, patches)
    if args.metadata_output is not None:
        write_metadata(
            args.metadata_output,
            {
                "dataset_name": dataset_name,
                "split": split,
                "instance_ids": instance_ids,
                "source": str(dataset_path(load_pins())),
                "pins": load_pins(),
                "prediction_source": "reference_patch",
                "warning": "Reference patches validate the official harness path; they are not model-generated benchmark predictions.",
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
