#!/usr/bin/env python3
"""Replay a pinned reference repair through the real coding-tools-mcp HTTP API.

This is deterministic tool-path evidence, not a model-generated solve-rate claim.
Only newly created temporary checkouts are changed.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.mcp_http import connect_with_retry, local_server_env  # noqa: E402
from benchmarks.swebench.generate_reference_predictions import write_predictions  # noqa: E402
from benchmarks.swebench.pinned import load_pins, replay_instance, sha256  # noqa: E402

PATH = "sympy/matrices/expressions/matexpr.py"
IMPORT_PATCH = """*** Begin Patch
*** Update File: sympy/matrices/expressions/matexpr.py
@@
-from sympy.core import S, Symbol, Tuple, Integer, Basic, Expr
+from sympy.core import S, Symbol, Tuple, Integer, Basic, Expr, Eq
@@
 from sympy.functions import conjugate, adjoint
+from sympy.functions.special.tensor_functions import KroneckerDelta
@@
-        from sympy import KroneckerDelta
         return KroneckerDelta(self.args[1], v.args[1])*KroneckerDelta(self.args[2], v.args[2])
*** End Patch
"""
OLD_ENTRY = """    def _entry(self, i, j):
        if i == j:
            return S.One
        else:
            return S.Zero"""
NEW_ENTRY = """    def _entry(self, i, j):
        eq = Eq(i, j)
        if eq is S.true:
            return S.One
        elif eq is S.false:
            return S.Zero
        return KroneckerDelta(i, j)"""


def run(command: list[str], cwd: Path, *, stdin: str | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, input=stdin, text=True, capture_output=True, timeout=300, check=False)
    if result.returncode:
        raise RuntimeError(f"{command!r} failed ({result.returncode}): {result.stderr[-4000:]}")
    return result.stdout


def checkout(destination: Path, source: str, commit: str) -> None:
    destination.mkdir()
    run(["git", "init", "--quiet"], destination)
    run(["git", "fetch", "--quiet", "--depth=1", source, commit], destination)
    run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], destination)
    head = run(["git", "rev-parse", "HEAD"], destination).strip()
    if head != commit or run(["git", "status", "--porcelain"], destination):
        raise RuntimeError("replay requires a clean checkout at the pinned base commit")


def line_edit(content: str, revision: str) -> dict[str, Any]:
    if content.count(OLD_ENTRY) != 1:
        raise ValueError("pinned Identity._entry edit context must occur exactly once")
    line = content[:content.index(OLD_ENTRY)].count("\n") + 1
    return {"changes": [{"action": "edit", "path": PATH, "revision": revision,
                         "edits": [{"op": "replace", "start_line": line,
                                    "end_line": line + len(OLD_ENTRY.splitlines()) - 1,
                                    "content": NEW_ENTRY}]}]}


def verify_execution(call: Callable[[str, dict[str, Any]], dict[str, Any]], execution: dict[str, Any]) -> None:
    """Check the final exit code against output from every incremental response."""
    stdout = [str(execution.get("stdout", ""))]
    stderr = [str(execution.get("stderr", ""))]
    deadline = time.monotonic() + 65
    while execution.get("exit_code") is None and execution.get("status") in {"running", "queued"}:
        if time.monotonic() > deadline:
            raise RuntimeError("MCP verification command timed out")
        execution = call("write_stdin", {"command_id": execution["command_id"], "chars": "", "yield_time_ms": 1000})
        stdout.append(str(execution.get("stdout", "")))
        stderr.append(str(execution.get("stderr", "")))
    output = "".join(stdout)
    if execution.get("exit_code") != 0 or "syntax-ok" not in output:
        evidence = {**execution, "stdout": output, "stderr": "".join(stderr)}
        raise RuntimeError(f"MCP exec verification failed: {evidence!r}")


def replay(workspace: Path, expected_content: str, raw_dir: Path, calls: list[dict[str, Any]]) -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = local_server_env()
    env["CODING_TOOLS_MCP_TELEMETRY"] = "off"
    env["CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS"] = os.pathsep.join({str(Path(sys.executable).resolve().parent), str(REPO_ROOT)})
    command = [sys.executable, "-m", "coding_tools_mcp", "--workspace", str(workspace),
               "--host", "127.0.0.1", "--port", str(port), "--permission-mode", "trusted"]
    raw_dir.mkdir(parents=True, exist_ok=True)
    with (raw_dir / "server.stdout.txt").open("wb") as stdout, (raw_dir / "server.stderr.txt").open("wb") as stderr:
        server = subprocess.Popen(command, cwd=REPO_ROOT, env=env, stdout=stdout, stderr=stderr)
        try:
            client, initialize, error = connect_with_retry(f"http://127.0.0.1:{port}/mcp", 15, catch=(Exception,))
            if client is None:
                raise RuntimeError(f"MCP startup unavailable: {error}")
            (raw_dir / "initialize.json").write_text(json.dumps(initialize, indent=2) + "\n")

            def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                response = client.call_tool(name, arguments)
                calls.append({"tool": name, "arguments": arguments, "result": response})
                (raw_dir / "mcp-transcript.json").write_text(json.dumps(calls, indent=2) + "\n")
                payload = response.get("structuredContent")
                if response.get("isError") or not isinstance(payload, dict):
                    raise RuntimeError(f"{name} failed: {response!r}")
                if payload.get("truncated"):
                    raise RuntimeError(f"{name} returned truncated evidence")
                return payload

            before = call("read_file", {"path": PATH, "max_bytes": 131072})
            if sha256(before["content"].encode()) != load_pins()["replay"]["source_sha256"]:
                raise RuntimeError("initial MCP read did not match pinned source")
            call("apply_patch", {"patch": IMPORT_PATCH})
            after_patch = call("read_file", {"path": PATH, "max_bytes": 131072})
            call("apply_changes", line_edit(after_patch["content"], after_patch["revision"]))
            after = call("read_file", {"path": PATH, "max_bytes": 131072})
            if after["content"] != expected_content:
                raise RuntimeError("MCP read-back differs from the native reference patch result")
            diff = call("git_diff", {"max_bytes": 131072, "include_untracked": True})
            patch = str(diff.get("diff", ""))
            if not patch.strip():
                raise RuntimeError("MCP diff did not contain a patch")
            # No legacy SymPy import or compatibility shim: official tests run in
            # the pinned harness image. Here exec verifies patch whitespace and
            # Python syntax without modifying the source checkout.
            script = f"import pathlib; compile(pathlib.Path({PATH!r}).read_text(), {PATH!r}, 'exec'); print('syntax-ok')"
            execution = call("exec_command", {"cmd": f"git diff --check && {shlex.quote(sys.executable)} -c {shlex.quote(script)}",
                                               "workdir": ".", "timeout_ms": 60000, "yield_time_ms": 1000,
                                               "max_output_bytes": 8192})
            verify_execution(call, execution)
            return patch
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Replace the summary atomically, including when an attempt is interrupted."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".replay-report-", delete=False) as pending:
        temporary = Path(pending.name)
    try:
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-checkout", type=Path, help="Read-only local Git source; replay always uses fresh temporary checkouts")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/benchmark/swebench-mcp-replay"))
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    report: dict[str, Any] = {"conclusion": "INCONCLUSIVE", "prediction_source": "mcp_reference_replay", "mcp_calls": calls,
                              "official_harness": "NOT_RUN", "limitations": ["Scripted reference repair, not model-generated predictions.",
                              "MCP exec checks syntax and patch whitespace; official resolution is a separate harness stage."]}
    report_path = output / "report.json"
    predictions = [output / name for name in ("baseline_native.jsonl", "candidate_mcp.jsonl")]
    # Invalidate the old summary before removing predictions or doing any
    # fallible work. Even an abrupt stop must not leave a prior PASS as current.
    try:
        write_report(report_path, report)
        for path in predictions:
            path.unlink(missing_ok=True)
        pins = load_pins()
        row = replay_instance(pins)
        report.update({"pins": pins, "instance_id": row["instance_id"], "base_commit": row["base_commit"],
                       "runtime_source_sha": run(["git", "rev-parse", "HEAD"], REPO_ROOT).strip()})
        source = str(args.source_checkout.resolve()) if args.source_checkout else f"https://github.com/{row['repo']}.git"
        with tempfile.TemporaryDirectory(prefix="coding-tools-swebench-") as tmp:
            root = Path(tmp)
            candidate, native = root / "candidate", root / "native"
            checkout(candidate, source, row["base_commit"])
            checkout(native, str(candidate), row["base_commit"])
            run(["git", "apply", "--check", "-"], native, stdin=row["patch"])
            run(["git", "apply", "-"], native, stdin=row["patch"])
            native_patch = run(["git", "diff", "--unified=3"], native)
            expected = (native / PATH).read_text(encoding="utf-8")
            if sha256(expected.encode()) != pins["replay"]["result_sha256"]:
                raise RuntimeError("native reference result differs from pinned result hash")
            candidate_patch = replay(candidate, expected, output / "raw", calls)
            if candidate_patch != native_patch:
                raise RuntimeError("MCP-generated diff differs from native reference replay")
            write_predictions(output / "baseline_native.jsonl", "native_reference_replay", {row["instance_id"]: native_patch})
            write_predictions(output / "candidate_mcp.jsonl", "coding_tools_mcp_reference_replay", {row["instance_id"]: candidate_patch})
        completed_report = {**report, "conclusion": "PASS", "candidate_patch_sha256": sha256(candidate_patch.encode()),
                            "reference_result_sha256": sha256(expected.encode()), "native_diff_matches_mcp": True,
                            "baseline_predictions": str(output / "baseline_native.jsonl"),
                            "candidate_predictions": str(output / "candidate_mcp.jsonl")}
        write_report(report_path, completed_report)
        report = completed_report
    except Exception as exc:
        report["conclusion"] = "FAIL"
        report["error"] = str(exc)
    finally:
        if report["conclusion"] != "PASS":
            # Publish non-success before cleanup, even if final PASS publication
            # was interrupted after its atomic replace had already completed.
            write_report(report_path, report)
            # Remove only our named predictions, including partially written
            # current outputs. Preserve unrelated files and historical evidence.
            for path in predictions:
                path.unlink(missing_ok=True)
    print(f"MCP reference replay: {report['conclusion']} ({report_path})")
    if "error" in report:
        print(report["error"], file=sys.stderr)
    return 0 if report["conclusion"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
