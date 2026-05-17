#!/usr/bin/env python3
"""Run MCP runtime checks against real public repositories."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.mcp_http import McpHttpClient, McpHttpError  # noqa: E402


@dataclass(frozen=True)
class Workload:
    name: str
    category: str
    repo: str
    required_executable: str
    read_path: str
    search_query: str
    command: str
    timeout_ms: int = 120000
    env: dict[str, str] = field(default_factory=dict)


WORKLOADS = [
    Workload(
        name="python-click",
        category="python",
        repo="https://github.com/pallets/click.git",
        required_executable="python",
        read_path="src/click/core.py",
        search_query="class Command",
        command="python -m compileall -q src tests",
        env={"PYTHONPYCACHEPREFIX": ".pycache"},
    ),
    Workload(
        name="node-mime-types",
        category="node",
        repo="https://github.com/jshttp/mime-types.git",
        required_executable="node",
        read_path="index.js",
        search_query="mime.lookup",
        command="node -e \"console.log('node-workload-ok')\"",
    ),
    Workload(
        name="rust-itoa",
        category="rust",
        repo="https://github.com/dtolnay/itoa.git",
        required_executable="cargo",
        read_path="src/lib.rs",
        search_query="Buffer",
        command="cargo test -q",
        timeout_ms=180000,
        env={"CARGO_HOME": ".cargo-home", "CARGO_TARGET_DIR": "target"},
    ),
    Workload(
        name="go-errors",
        category="go",
        repo="https://github.com/pkg/errors.git",
        required_executable="go",
        read_path="errors.go",
        search_query="func New",
        command="go test",
        timeout_ms=180000,
        env={"GOCACHE": ".gocache", "GOMODCACHE": ".gomodcache", "GO111MODULE": "off", "GOTOOLCHAIN": "local"},
    ),
    Workload(
        name="monorepo-changesets",
        category="monorepo",
        repo="https://github.com/changesets/changesets.git",
        required_executable="node",
        read_path="packages/cli/package.json",
        search_query="workspaces",
        command="node -e \"console.log('monorepo-workload-ok')\"",
    ),
]


def run(command: list[str], cwd: Path, timeout: int = 300) -> dict[str, Any]:
    started = time.time()
    result = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout[-12000:],
        "stderr": result.stderr[-12000:],
        "elapsed_seconds": round(time.time() - started, 3),
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def command_stdout(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def toolchain_allow_roots() -> list[str]:
    roots: set[str] = set()
    for executable in ("python", "node", "cargo", "rustc", "rustup", "go"):
        path = shutil.which(executable)
        if not path:
            continue
        try:
            roots.add(str(Path(path).resolve().parent))
        except OSError:
            pass
    for raw in (
        os.environ.get("CARGO_HOME"),
        os.environ.get("RUSTUP_HOME"),
        str(Path.home() / ".cargo"),
        str(Path.home() / ".rustup"),
        command_stdout(["go", "env", "GOROOT"]),
        command_stdout(["go", "env", "GOPATH"]),
    ):
        if not raw:
            continue
        try:
            path = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if path.exists():
            roots.add(str(path))
    return sorted(roots)


def tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        raise RuntimeError(json.dumps(result.get("structuredContent", result), indent=2, sort_keys=True))
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        raise RuntimeError(f"tool result missing structuredContent: {result!r}")
    return payload


def start_server(workspace: Path, port: int, raw_dir: Path, name: str) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-c",
        "import sys; from codex_tool_runtime_mcp.server import main; raise SystemExit(main())",
        "--workspace",
        str(workspace),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    raw_dir.mkdir(parents=True, exist_ok=True)
    stdout = (raw_dir / f"{name}-server.stdout.txt").open("wb")
    stderr = (raw_dir / f"{name}-server.stderr.txt").open("wb")
    env = os.environ.copy()
    env["CODEX_TOOL_RUNTIME_EXEC_ALLOW_ROOTS"] = os.pathsep.join(toolchain_allow_roots())
    (raw_dir / f"{name}-exec-allow-roots.txt").write_text(env["CODEX_TOOL_RUNTIME_EXEC_ALLOW_ROOTS"] + "\n", encoding="utf-8")
    return subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env)


def initialize_client(endpoint: str) -> McpHttpClient:
    last_error: Exception | None = None
    for _ in range(50):
        client = McpHttpClient(endpoint, timeout=10)
        try:
            client.initialize()
            return client
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"MCP server did not become ready: {last_error}")


def clone_repo(workload: Workload, checkout_root: Path, raw_dir: Path) -> tuple[Path, dict[str, Any], str | None]:
    destination = checkout_root / workload.name
    clone_log = run(["git", "clone", "--depth", "1", workload.repo, str(destination)], checkout_root, timeout=300)
    write_json(raw_dir / f"{workload.name}-git-clone.json", clone_log)
    if clone_log["returncode"] != 0:
        return destination, clone_log, None
    head = run(["git", "rev-parse", "HEAD"], destination, timeout=30)
    write_json(raw_dir / f"{workload.name}-git-head.json", head)
    commit = head["stdout"].strip() if head["returncode"] == 0 else None
    return destination, clone_log, commit


def write_large_fixture(workspace: Path) -> str:
    relative = "mcp-large-workload.txt"
    line = "mcp-large-workload-line data data data data data data data data data data\n"
    (workspace / relative).write_text(line * 25000, encoding="utf-8")
    return relative


def run_workload(workload: Workload, checkout_root: Path, raw_dir: Path, port: int, allow_missing_tools: bool) -> dict[str, Any]:
    if shutil.which(workload.required_executable) is None:
        status = "SKIP" if allow_missing_tools else "FAIL"
        return {
            "name": workload.name,
            "category": workload.category,
            "repo": workload.repo,
            "status": status,
            "reason": f"required executable not found: {workload.required_executable}",
        }

    workspace, clone_log, commit = clone_repo(workload, checkout_root, raw_dir)
    result: dict[str, Any] = {
        "name": workload.name,
        "category": workload.category,
        "repo": workload.repo,
        "commit": commit,
        "clone": clone_log,
        "checks": [],
        "status": "FAIL",
    }
    if clone_log["returncode"] != 0:
        result["reason"] = "git clone failed"
        return result

    large_file = write_large_fixture(workspace) if workload.category == "monorepo" else None
    server = start_server(workspace, port, raw_dir, workload.name)
    try:
        client = initialize_client(f"http://127.0.0.1:{port}/mcp")
        tools = client.list_tools()
        tool_names = {tool.get("name") for tool in tools}
        missing_tools = sorted({"read_file", "list_files", "search_text", "exec_command"} - tool_names)
        if missing_tools:
            raise RuntimeError(f"missing MCP tools: {missing_tools}")

        listed = tool_payload(client.call_tool("list_files", {"path": ".", "max_results": 2000}))
        listed_files = listed.get("files") if isinstance(listed.get("files"), list) else []
        result["checks"].append({"name": "list_files", "count": len(listed_files), "ok": bool(listed_files)})

        read = tool_payload(client.call_tool("read_file", {"path": workload.read_path, "max_bytes": 65536}))
        result["checks"].append({"name": "read_file", "path": workload.read_path, "ok": bool(read.get("content"))})

        search = tool_payload(
            client.call_tool(
                "search_text",
                {"query": workload.search_query, "path": ".", "max_results": 20, "max_preview_bytes": 512},
            )
        )
        result["checks"].append({"name": "search_text", "query": workload.search_query, "ok": bool(search.get("matches"))})

        exec_payload = tool_payload(
            client.call_tool(
                "exec_command",
                {
                    "cmd": workload.command,
                    "timeout_ms": workload.timeout_ms,
                    "yield_time_ms": min(workload.timeout_ms, 30000),
                    "max_output_bytes": 65536,
                    "env": workload.env,
                },
            )
        )
        result["checks"].append(
            {
                "name": "exec_command",
                "cmd": workload.command,
                "exit_code": exec_payload.get("exit_code"),
                "stdout": str(exec_payload.get("stdout", ""))[-1000:],
                "stderr": str(exec_payload.get("stderr", ""))[-1000:],
                "ok": exec_payload.get("exit_code") == 0,
            }
        )

        if large_file:
            large_read = tool_payload(client.call_tool("read_file", {"path": large_file, "max_bytes": 1048576}))
            result["checks"].append(
                {
                    "name": "large_file",
                    "path": large_file,
                    "bytes": len(str(large_read.get("content", "")).encode("utf-8")),
                    "ok": bool(large_read.get("truncated")),
                }
            )
            big_output = tool_payload(
                client.call_tool(
                    "exec_command",
                    {
                        "cmd": "python -c \"for i in range(25000): print('real-workload-output-%05d' % i)\"",
                        "timeout_ms": 60000,
                        "yield_time_ms": 30000,
                        "max_output_bytes": 8192,
                    },
                )
            )
            result["checks"].append(
                {
                    "name": "large_output",
                    "exit_code": big_output.get("exit_code"),
                    "stdout_truncated": bool(big_output.get("stdout_truncated") or big_output.get("stdout_dropped_bytes")),
                    "ok": big_output.get("exit_code") == 0,
                }
            )
            long_test = tool_payload(
                client.call_tool(
                    "exec_command",
                    {
                        "cmd": "python -c \"import time; time.sleep(2); print('long-test-ok')\"",
                        "timeout_ms": 10000,
                        "yield_time_ms": 5000,
                        "max_output_bytes": 8192,
                    },
                )
            )
            result["checks"].append(
                {
                    "name": "long_test",
                    "exit_code": long_test.get("exit_code"),
                    "stdout": long_test.get("stdout", "")[-200:],
                    "ok": long_test.get("exit_code") == 0 and "long-test-ok" in str(long_test.get("stdout", "")),
                }
            )

        result["status"] = "PASS" if all(check.get("ok") for check in result["checks"]) else "FAIL"
    except (McpHttpError, RuntimeError) as exc:
        result["reason"] = str(exc)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
    return result


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Real Workload MCP Benchmark",
        "",
        f"- Conclusion: **{report['conclusion']}**",
        f"- Workloads: `{len(report['workloads'])}`",
        f"- Raw log directory: `{report['raw_dir']}`",
        "",
        "## Workloads",
        "",
    ]
    for item in report["workloads"]:
        lines.append(f"- `{item['name']}` ({item['category']}): **{item['status']}**")
        lines.append(f"  - repo: `{item['repo']}`")
        if item.get("commit"):
            lines.append(f"  - commit: `{item['commit']}`")
        if item.get("reason"):
            lines.append(f"  - reason: `{item['reason']}`")
        for check in item.get("checks", []):
            lines.append(f"  - {check['name']}: `{'PASS' if check.get('ok') else 'FAIL'}`")
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            "- Python repository",
            "- Node repository",
            "- Rust repository",
            "- Go repository",
            "- Monorepo",
            "- Large file read",
            "- Large output command",
            "- Long-running command",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-json", type=Path, default=Path("reports/benchmark/real-workloads.json"))
    parser.add_argument("--report-md", type=Path, default=Path("reports/benchmark/real-workloads.md"))
    parser.add_argument("--raw-dir", type=Path, default=Path("reports/benchmark/real-workloads/raw"))
    parser.add_argument("--allow-missing-tools", action="store_true")
    parser.add_argument("--start-port", type=int, default=8870)
    args = parser.parse_args(argv)

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-real-workloads-") as tmp:
        checkout_root = Path(tmp)
        workloads = [
            run_workload(workload, checkout_root, args.raw_dir, args.start_port + index, args.allow_missing_tools)
            for index, workload in enumerate(WORKLOADS)
        ]

    failures = [item for item in workloads if item.get("status") == "FAIL"]
    skips = [item for item in workloads if item.get("status") == "SKIP"]
    if failures:
        conclusion = "FAIL"
    elif skips:
        conclusion = "PARTIAL"
    else:
        conclusion = "PASS"
    report = {
        "conclusion": conclusion,
        "raw_dir": str(args.raw_dir),
        "workloads": workloads,
        "failures": failures,
        "skips": skips,
    }
    write_json(args.report_json, report)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(render_markdown(report), encoding="utf-8")
    return 0 if conclusion == "PASS" or (conclusion == "PARTIAL" and args.allow_missing_tools) else 1


if __name__ == "__main__":
    raise SystemExit(main())
