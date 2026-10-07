"""Verified, offline SWE-bench inputs shared by the replay and official smoke."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PINS_PATH = ROOT / "pins.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_pins(path: Path = PINS_PATH) -> dict[str, Any]:
    pins: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if pins.get("schema_version") != 1:
        raise ValueError("unsupported SWE-bench pin schema")
    if not re.fullmatch(r"[0-9a-f]{40}", pins["dataset"]["revision"]):
        raise ValueError("dataset revision must be an immutable commit")
    return pins


def dataset_path(pins: dict[str, Any]) -> Path:
    path = ROOT / pins["dataset"]["fixture"]
    if sha256(path.read_bytes()) != pins["dataset"]["fixture_sha256"]:
        raise ValueError("pinned dataset fixture SHA-256 mismatch")
    return path


def load_instances(pins: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    pins = pins or load_pins()
    rows: list[dict[str, Any]] = json.loads(dataset_path(pins).read_text(encoding="utf-8"))
    ids = [row["instance_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate instances in pinned fixture")
    return rows


def replay_instance(pins: dict[str, Any]) -> dict[str, Any]:
    replay = pins["replay"]
    row = next(row for row in load_instances(pins) if row["instance_id"] == replay["instance_id"])
    if row["base_commit"] != replay["base_commit"]:
        raise ValueError("replay base commit does not match fixture")
    if sha256(row["patch"].encode()) != replay["reference_patch_sha256"]:
        raise ValueError("reference patch SHA-256 mismatch")
    return row


def docker_image(pins: dict[str, Any], instance_id: str) -> tuple[str, str]:
    image = pins["docker"]["images"][instance_id]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"]):
        raise ValueError("Docker image must be digest-pinned")
    return (
        f"{image['repository']}@{image['digest']}",
        f"{image['repository']}:{pins['docker']['instance_image_tag']}",
    )
