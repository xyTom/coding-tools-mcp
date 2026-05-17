#!/usr/bin/env python3
"""Generate SWE-bench prediction JSONL files from reference patches."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DATASETS_SERVER_ROWS = "https://datasets-server.huggingface.co/rows"


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


def fetch_rows(dataset_name: str, split: str, offset: int, length: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "dataset": dataset_name,
            "config": "default",
            "split": split,
            "offset": offset,
            "length": length,
        }
    )
    request = urllib.request.Request(f"{DATASETS_SERVER_ROWS}?{query}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read()
    return json.loads(payload)


def fetch_reference_patches(dataset_name: str, split: str, instance_ids: list[str], page_size: int = 100) -> dict[str, str]:
    wanted = set(instance_ids)
    patches: dict[str, str] = {}
    offset = 0
    while wanted - set(patches):
        page = fetch_rows(dataset_name, split, offset, page_size)
        rows = page.get("rows", [])
        if not isinstance(rows, list) or not rows:
            break
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            row = entry.get("row")
            if not isinstance(row, dict):
                continue
            instance_id = row.get("instance_id")
            patch = row.get("patch")
            if isinstance(instance_id, str) and instance_id in wanted and isinstance(patch, str) and patch.strip():
                patches[instance_id] = patch
        offset += len(rows)
    missing = sorted(wanted - set(patches))
    if missing:
        raise SystemExit(f"reference patches not found for: {path_display(missing)}")
    return {instance_id: patches[instance_id] for instance_id in instance_ids}


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
    parser.add_argument("--candidate-model-name", default="candidate_mcp_reference_patch")
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
                "source": DATASETS_SERVER_ROWS,
                "prediction_source": "reference_patch",
                "warning": "Reference patches validate the official harness path; they are not model-generated benchmark predictions.",
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
