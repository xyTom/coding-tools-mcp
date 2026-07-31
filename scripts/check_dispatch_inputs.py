#!/usr/bin/env python3
"""Verify the sandbox-control Worker and start-sandbox.yml agree on inputs.

GitHub rejects a ``workflow_dispatch`` API call with ``422 Unexpected inputs
provided`` before it creates a run, so a Worker that sends a field the workflow
does not declare fails without leaving any trace in the Actions log. This check
compares the two sources so that mismatch is caught in CI instead.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

WORKER_FUNCTION = "buildWorkflowInputs"
KEY_RE = re.compile(r"^([A-Za-z_$][\w$]*)\s*(?::|$)")


def workflow_triggers(document: Any) -> dict[str, Any]:
    # PyYAML resolves the bare ``on:`` key to the boolean True.
    triggers = document.get(True)
    if triggers is None:
        triggers = document.get("on")
    if not isinstance(triggers, dict):
        raise ValueError("workflow has no usable 'on:' block")
    return triggers


def declared_inputs(triggers: dict[str, Any], trigger: str) -> dict[str, Any]:
    block = triggers.get(trigger)
    if not isinstance(block, dict):
        raise ValueError(f"workflow has no '{trigger}' trigger")
    return block.get("inputs") or {}


def _skip_string(source: str, index: int) -> int:
    quote = source[index]
    index += 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    raise ValueError("unterminated string literal in Worker source")


def _skip_comment(source: str, index: int) -> int:
    if source.startswith("//", index):
        end = source.find("\n", index)
        return len(source) if end == -1 else end
    end = source.find("*/", index)
    if end == -1:
        raise ValueError("unterminated block comment in Worker source")
    return end + 2


def object_literal_entries(source: str, start: int) -> list[str]:
    """Split the object literal that opens at ``start`` into top-level entries."""
    if source[start] != "{":
        raise ValueError("expected an object literal")
    depth = 0
    index = start
    entries: list[str] = []
    current: list[str] = []
    while index < len(source):
        char = source[index]
        if char in "\"'`":
            end = _skip_string(source, index)
            current.append(source[index:end])
            index = end
            continue
        if source.startswith("//", index) or source.startswith("/*", index):
            index = _skip_comment(source, index)
            continue
        if char in "{[(":
            depth += 1
            if depth > 1:
                current.append(char)
            index += 1
            continue
        if char in "}])":
            depth -= 1
            if depth == 0:
                entries.append("".join(current))
                return [entry.strip() for entry in entries if entry.strip()]
            current.append(char)
            index += 1
            continue
        if char == "," and depth == 1:
            entries.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    raise ValueError("unterminated object literal in Worker source")


def worker_sent_inputs(worker_source: str) -> set[str]:
    """Collect the keys the Worker puts in its workflow_dispatch request body."""
    function_at = worker_source.find(f"function {WORKER_FUNCTION}")
    if function_at == -1:
        raise ValueError(f"{WORKER_FUNCTION} not found in Worker source")
    return_at = worker_source.find("return {", function_at)
    if return_at == -1:
        raise ValueError(f"{WORKER_FUNCTION} has no object literal return")
    brace_at = worker_source.index("{", return_at)

    keys: set[str] = set()
    for entry in object_literal_entries(worker_source, brace_at):
        match = KEY_RE.match(entry)
        if match is None:
            raise ValueError(f"could not read a key from entry: {entry!r}")
        keys.add(match.group(1))
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check sandbox-control Worker inputs against start-sandbox.yml."
    )
    parser.add_argument(
        "--workflow",
        type=Path,
        default=Path(".github/workflows/start-sandbox.yml"),
    )
    parser.add_argument(
        "--worker",
        type=Path,
        default=Path("cloudflare/sandbox-control/src/index.mjs"),
    )
    args = parser.parse_args()

    triggers = workflow_triggers(yaml.safe_load(args.workflow.read_text(encoding="utf-8")))
    dispatch = declared_inputs(triggers, "workflow_dispatch")
    call = declared_inputs(triggers, "workflow_call")
    sent = worker_sent_inputs(args.worker.read_text(encoding="utf-8"))

    errors: list[str] = []

    for name in sorted(sent - set(dispatch)):
        errors.append(
            f"Worker sends {name!r} but workflow_dispatch.inputs does not declare it. "
            "GitHub will reject the dispatch with 422 Unexpected inputs provided."
        )

    for name in sorted(set(dispatch) - set(call)):
        errors.append(
            f"{name!r} is declared in workflow_dispatch.inputs but missing from "
            "workflow_call.inputs."
        )
    for name in sorted(set(call) - set(dispatch)):
        errors.append(
            f"{name!r} is declared in workflow_call.inputs but missing from "
            "workflow_dispatch.inputs."
        )

    for name in sorted(set(dispatch) - sent):
        spec = dispatch[name] or {}
        if spec.get("required") and "default" not in spec:
            errors.append(
                f"workflow_dispatch.inputs.{name} is required with no default and the "
                "Worker never sends it."
            )

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print(
        f"Dispatch inputs OK: Worker sends {len(sent)} inputs, "
        f"workflow_dispatch declares {len(dispatch)}, workflow_call declares {len(call)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
