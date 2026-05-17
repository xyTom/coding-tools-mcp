#!/usr/bin/env python3
"""Run deterministic dogfood tasks through MCP tools only.

This runner may prepare fixtures and start the local server process. Once the
server is running, task execution uses only MCP ``tools/list`` and
``tools/call`` over the configured HTTP endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.mcp_http import McpHttpClient, McpHttpError


FIXTURE_FILES: dict[str, str] = {
    "tiny-js-project/package.json": json.dumps(
        {"scripts": {"test": "node test/math.test.js"}, "devDependencies": {}},
        indent=2,
    )
    + "\n",
    "tiny-js-project/src/math.js": """function add(a, b) {
  return a - b;
}

module.exports = { add };
""",
    "tiny-js-project/test/math.test.js": """const assert = require("assert");
const { add } = require("../src/math");

assert.strictEqual(add(2, 3), 5);
console.log("js ok");
""",
    "tiny-python-project/src/__init__.py": "",
    "tiny-python-project/src/math_utils.py": """def add(a, b):
    return a + b
""",
    "tiny-python-project/tests/test_math_utils.py": """import unittest

from src.math_utils import add, multiply


class MathUtilsTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_multiply(self):
        self.assertEqual(multiply(4, 5), 20)


if __name__ == "__main__":
    unittest.main()
""",
    "long-running-project/repl.py": """import sys

print("ready", flush=True)
for line in sys.stdin:
    value = line.strip()
    if value == "exit":
        print("bye", flush=True)
        break
    print(f"echo:{value}", flush=True)
""",
    ".gitignore": "__pycache__/\n*.pyc\nnode_modules/\n",
}


JS_PATCH = """*** Begin Patch
*** Update File: tiny-js-project/src/math.js
@@
 function add(a, b) {
-  return a - b;
+  return a + b;
 }
*** End Patch
"""


PYTHON_PATCH = """*** Begin Patch
*** Update File: tiny-python-project/src/math_utils.py
@@
 def add(a, b):
     return a + b
+
+
+def multiply(a, b):
+    return a * b
*** End Patch
"""


ESCAPE_PATCH = """*** Begin Patch
*** Update File: ../outside-secret.txt
@@
-DOGFOOD-OUTSIDE-SECRET
+MODIFIED
*** End Patch
"""


REJECTION_MARKERS = (
    "denied",
    "forbidden",
    "outside",
    "escape",
    "traversal",
    "permission",
    "not allowed",
    "workspace",
)


@dataclass
class ToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    expected_rejection: bool = False
    summary: str = ""


@dataclass
class CaseResult:
    name: str
    status: str
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def add_check(self, label: str, passed: bool, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        if passed:
            self.checks.append(f"PASS {label}{suffix}")
        else:
            self.failures.append(f"FAIL {label}{suffix}")

    def finalize(self) -> "CaseResult":
        self.status = "PASS" if not self.failures else "FAIL"
        return self


class ToolAdapter:
    def __init__(self, tools: list[dict[str, Any]]) -> None:
        self.tools = {tool.get("name"): tool for tool in tools if isinstance(tool.get("name"), str)}

    def names(self) -> list[str]:
        return sorted(self.tools)

    def missing(self, required: list[str]) -> list[str]:
        return [name for name in required if name not in self.tools]

    def read_file_args(self, path: str) -> dict[str, Any]:
        return self._args("read_file", {"path": path})

    def search_text_args(self, query: str, path: str | None = None) -> dict[str, Any]:
        canonical: dict[str, Any] = {"query": query, "pattern": query, "text": query}
        if path:
            canonical.update({"path": path, "cwd": path, "workdir": path})
        return self._args("search_text", canonical)

    def apply_patch_args(self, patch: str) -> dict[str, Any]:
        return self._args("apply_patch", {"patch": patch})

    def exec_args(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: int = 10,
        tty: bool = False,
    ) -> dict[str, Any]:
        canonical: dict[str, Any] = {
            "cmd": command,
            "command": command,
            "shell_command": command,
            "timeout": timeout_seconds,
            "timeout_seconds": timeout_seconds,
            "timeout_ms": timeout_seconds * 1000,
            "max_output_bytes": 40_000,
            "yield_time_ms": min(timeout_seconds * 1000, 20000),
            "tty": tty,
            "interactive": tty,
        }
        if cwd:
            canonical.update({"cwd": cwd, "workdir": cwd, "working_directory": cwd})
        args = self._args("exec_command", canonical)
        if "cmd" not in args and "command" not in args and "argv" in self._properties("exec_command"):
            args["argv"] = shlex.split(command)
        return args

    def write_stdin_args(self, session_id: str, chars: str) -> dict[str, Any]:
        return self._args("write_stdin", {"session_id": session_id, "sessionId": session_id, "chars": chars, "input": chars})

    def kill_session_args(self, session_id: str) -> dict[str, Any]:
        return self._args("kill_session", {"session_id": session_id, "sessionId": session_id})

    def git_diff_args(self, path: str | None = None) -> dict[str, Any]:
        if path is None:
            return self._args("git_diff", {})
        return self._args("git_diff", {"path": path, "paths": [path]})

    def _args(self, tool_name: str, canonical: dict[str, Any]) -> dict[str, Any]:
        properties = self._properties(tool_name)
        required = self._required(tool_name)
        if not properties:
            return {key: value for key, value in canonical.items() if key in ("path", "query", "patch", "cmd", "command")}
        args: dict[str, Any] = {}
        for key, value in canonical.items():
            if key in properties:
                args[key] = value
        for key in required:
            if key in args:
                continue
            if key in canonical:
                args[key] = canonical[key]
                continue
            if key in ("path", "file", "filename") and "path" in canonical:
                args[key] = canonical["path"]
            elif key in ("query", "pattern", "text") and "query" in canonical:
                args[key] = canonical["query"]
            elif key in ("cmd", "command", "shell_command") and "command" in canonical:
                args[key] = canonical["command"]
            elif key in ("patch", "input") and "patch" in canonical:
                args[key] = canonical["patch"]
        return args

    def _schema(self, tool_name: str) -> dict[str, Any]:
        tool = self.tools.get(tool_name, {})
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        return schema if isinstance(schema, dict) else {}

    def _properties(self, tool_name: str) -> dict[str, Any]:
        properties = self._schema(tool_name).get("properties", {})
        return properties if isinstance(properties, dict) else {}

    def _required(self, tool_name: str) -> list[str]:
        required = self._schema(tool_name).get("required", [])
        return [item for item in required if isinstance(item, str)]


class DogfoodRunner:
    def __init__(self, client: McpHttpClient, adapter: ToolAdapter) -> None:
        self.client = client
        self.adapter = adapter
        self.calls: list[ToolCallRecord] = []

    def run_all(self) -> list[CaseResult]:
        return [
            self.case_js_bugfix(),
            self.case_python_function(),
            self.case_long_running_stdin(),
            self.case_workspace_escape(),
        ]

    def case_js_bugfix(self) -> CaseResult:
        case = CaseResult("js_bugfix", "FAIL")
        search = self.call("search_text", self.adapter.search_text_args("function add", "tiny-js-project"))
        case.add_check("search_text finds add", not is_error_result(search), summarize(search))
        read = self.call("read_file", self.adapter.read_file_args("tiny-js-project/src/math.js"))
        case.add_check("read_file returns buggy source", result_contains(read, "return a - b"), summarize(read))
        patch = self.call("apply_patch", self.adapter.apply_patch_args(JS_PATCH))
        case.add_check("apply_patch fixes add", not is_error_result(patch), summarize(patch))
        test = self.call(
            "exec_command",
            self.adapter.exec_args("npm test", cwd="tiny-js-project", timeout_seconds=20),
        )
        case.add_check("exec_command npm test passes", command_passed(test), summarize(test))
        diff = self.call("git_diff", self.adapter.git_diff_args("tiny-js-project/src/math.js"))
        diff_text = result_text(diff)
        expected_diff = "return a + b" in diff_text and "return a - b" in diff_text
        case.add_check("git_diff shows only math.js fix", expected_diff and "outside-secret" not in diff_text, summarize(diff))
        return case.finalize()

    def case_python_function(self) -> CaseResult:
        case = CaseResult("python_new_function", "FAIL")
        read = self.call("read_file", self.adapter.read_file_args("tiny-python-project/src/math_utils.py"))
        case.add_check("read_file returns python source", result_contains(read, "def add"), summarize(read))
        patch = self.call("apply_patch", self.adapter.apply_patch_args(PYTHON_PATCH))
        case.add_check("apply_patch adds multiply", not is_error_result(patch), summarize(patch))
        test = self.call(
            "exec_command",
            self.adapter.exec_args(
                f"{shlex.quote(sys.executable)} -m unittest discover -s tests",
                cwd="tiny-python-project",
                timeout_seconds=20,
            ),
        )
        case.add_check("exec_command unittest passes", command_passed(test), summarize(test))
        diff = self.call("git_diff", self.adapter.git_diff_args("tiny-python-project/src/math_utils.py"))
        case.add_check("git_diff shows multiply", result_contains(diff, "def multiply"), summarize(diff))
        return case.finalize()

    def case_long_running_stdin(self) -> CaseResult:
        case = CaseResult("long_running_stdin", "FAIL")
        started = self.call(
            "exec_command",
            self.adapter.exec_args(
                f"{shlex.quote(sys.executable)} repl.py",
                cwd="long-running-project",
                timeout_seconds=30,
                tty=True,
            ),
        )
        session_id = find_session_id(started)
        case.add_check("exec_command returns session_id", bool(session_id), summarize(started))
        if not session_id:
            return case.finalize()
        hello = self.call("write_stdin", self.adapter.write_stdin_args(session_id, "hello\n"))
        case.add_check("write_stdin accepts hello", not is_error_result(hello), summarize(hello))
        exit_reply = self.call("write_stdin", self.adapter.write_stdin_args(session_id, "exit\n"))
        case.add_check("write_stdin accepts exit", not is_error_result(exit_reply), summarize(exit_reply))
        killed = self.call("kill_session", self.adapter.kill_session_args(session_id), expected_rejection=True)
        case.add_check(
            "kill_session terminates or reports already closed",
            not is_error_result(killed) or rejected_as_expected(killed),
            summarize(killed),
        )
        return case.finalize()

    def case_workspace_escape(self) -> CaseResult:
        case = CaseResult("workspace_escape", "FAIL")
        read = self.call("read_file", self.adapter.read_file_args("../outside-secret.txt"), expected_rejection=True)
        case.add_check("read_file rejects ../ escape", rejected_as_expected(read), summarize(read))
        patch = self.call("apply_patch", self.adapter.apply_patch_args(ESCAPE_PATCH), expected_rejection=True)
        case.add_check("apply_patch rejects ../ escape", rejected_as_expected(patch), summarize(patch))
        escaped = self.call(
            "exec_command",
            self.adapter.exec_args("cat ../outside-secret.txt", timeout_seconds=10),
            expected_rejection=True,
        )
        leaked_secret = result_contains(escaped, "DOGFOOD-OUTSIDE-SECRET")
        case.add_check("exec_command does not expose outside secret", rejected_as_expected(escaped) and not leaked_secret, summarize(escaped))
        return case.finalize()

    def call(self, tool: str, arguments: dict[str, Any], *, expected_rejection: bool = False) -> dict[str, Any]:
        try:
            result = self.client.call_tool(tool, arguments)
            ok = not is_error_result(result)
            self.calls.append(ToolCallRecord(tool, arguments, ok, expected_rejection, summarize(result)))
            return result
        except McpHttpError as exc:
            result = {"isError": True, "transport_error": str(exc), "payload": exc.payload}
            self.calls.append(ToolCallRecord(tool, arguments, False, expected_rejection, str(exc)))
            return result


def prepare_workspace(base_dir: Path | None = None) -> tuple[Path, Path]:
    if base_dir is None:
        root = Path(tempfile.mkdtemp(prefix="codex-mcp-dogfood-"))
    else:
        root = base_dir
        root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    for relative, content in FIXTURE_FILES.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / "outside-secret.txt").write_text("DOGFOOD-OUTSIDE-SECRET\n", encoding="utf-8")
    return root, workspace


def start_server(command: str | None, workspace: Path, endpoint: str) -> subprocess.Popen[bytes] | None:
    if not command:
        return None
    env = os.environ.copy()
    env.setdefault("CODEX_TOOL_RUNTIME_WORKSPACE", str(workspace))
    env.setdefault("CODEX_TOOL_RUNTIME_MCP_ENDPOINT", endpoint)
    argv = shlex.split(command.format(workspace=str(workspace), endpoint=endpoint))
    return subprocess.Popen(argv, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect(endpoint: str, startup_timeout: float) -> tuple[McpHttpClient | None, dict[str, Any] | None, str | None]:
    deadline = time.monotonic() + startup_timeout
    last_error: str | None = None
    while time.monotonic() <= deadline:
        client = McpHttpClient(endpoint, timeout=10)
        try:
            initialize_result = client.initialize()
            return client, initialize_result, None
        except McpHttpError as exc:
            last_error = str(exc)
            time.sleep(0.25)
    return None, None, last_error or "startup timeout elapsed"


def result_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    for key in ("stdout", "stderr", "output", "text", "transport_error"):
        if isinstance(result.get(key), str):
            parts.append(result[key])
    payload = result.get("payload")
    if payload is not None:
        parts.append(json.dumps(payload, sort_keys=True))
    return "\n".join(parts)


def parse_text_json(result: dict[str, Any]) -> dict[str, Any]:
    text = result_text(result).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_error_result(result: dict[str, Any]) -> bool:
    if result.get("isError") is True or result.get("transport_error"):
        return True
    parsed = parse_text_json(result)
    return parsed.get("isError") is True or parsed.get("error") is not None


def rejected_as_expected(result: dict[str, Any]) -> bool:
    if is_error_result(result):
        return True
    text = result_text(result).lower()
    return any(marker in text for marker in REJECTION_MARKERS) and not command_passed(result)


def result_contains(result: dict[str, Any], needle: str) -> bool:
    return needle in result_text(result)


def command_passed(result: dict[str, Any]) -> bool:
    if is_error_result(result):
        return False
    parsed = parse_text_json(result)
    candidates: list[Any] = [
        result.get("exit_code"),
        result.get("exitCode"),
        result.get("status"),
        parsed.get("exit_code"),
        parsed.get("exitCode"),
        parsed.get("status"),
    ]
    for value in candidates:
        if value == 0 or value == "passed" or value == "success":
            return True
        if isinstance(value, int) and value != 0:
            return False
    text = result_text(result)
    if re.search(r"\b(exit_code|exitCode)\b[^0-9-]*0\b", text):
        return True
    return "js ok" in text or "OK" in text


def find_session_id(result: dict[str, Any]) -> str | None:
    parsed = parse_text_json(result)
    for source in (result, parsed):
        for key in ("session_id", "sessionId", "session"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    text = result_text(result)
    match = re.search(r'"?(session_id|sessionId)"?\s*[:=]\s*"([^"]+)"', text)
    return match.group(2) if match else None


def summarize(result: dict[str, Any], limit: int = 180) -> str:
    text = result_text(result).replace("\n", "\\n")
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def write_reports(report_json: Path, report_md: Path, report: dict[str, Any]) -> None:
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_md.write_text(render_markdown(report), encoding="utf-8")


def write_transcript(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript = {
        "endpoint": report.get("endpoint"),
        "workspace": report.get("workspace"),
        "direct_bypass": report.get("direct_bypass"),
        "tool_calls": report.get("tool_calls", []),
        "cases": report.get("cases", []),
    }
    path.write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Codex-on-MCP Dogfood Report",
        "",
        f"- Conclusion: **{report['conclusion']}**",
        f"- Endpoint: `{report['endpoint']}`",
        f"- Workspace: `{report['workspace']}`",
        f"- Server command: `{report.get('server_command') or 'not started by runner'}`",
        f"- Codex version: `{report.get('codex_version') or 'unknown'}`",
        f"- Direct filesystem/shell bypass during task execution: `{report['direct_bypass']}`",
        "",
        "## tools/list",
        "",
    ]
    for tool in report.get("tools", []):
        lines.append(f"- `{tool}`")
    if not report.get("tools"):
        lines.append("- not available")
    lines.extend(["", "## Prompt", "", report["prompt"], "", "## Case Results", ""])
    for case in report.get("cases", []):
        lines.append(f"### {case['name']}: {case['status']}")
        for check in case.get("checks", []):
            lines.append(f"- {check}")
        for failure in case.get("failures", []):
            lines.append(f"- {failure}")
        lines.append("")
    lines.extend(["## MCP Tool Calls", ""])
    for call in report.get("tool_calls", []):
        expected = " expected_rejection" if call["expected_rejection"] else ""
        lines.append(f"- `{call['tool']}` ok={call['ok']}{expected} args={json.dumps(call['arguments'], sort_keys=True)}")
    if not report.get("tool_calls"):
        lines.append("- none")
    lines.extend(["", "## Final Git Diff", "", report.get("final_git_diff") or "Not available.", "", "## Known Limitations", ""])
    for item in report.get("known_limitations", []):
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--server-command", default=None)
    parser.add_argument("--fixture-root", type=Path, default=None)
    parser.add_argument("--startup-timeout", type=float, default=5.0)
    parser.add_argument("--report-json", type=Path, default=ROOT / "reports/dogfood/codex-on-mcp.json")
    parser.add_argument("--report-md", type=Path, default=ROOT / "reports/dogfood/codex-on-mcp.md")
    parser.add_argument("--transcript-json", type=Path, default=ROOT / "docs/dogfood/codex-on-mcp-transcript.json")
    args = parser.parse_args(argv)

    fixture_root, workspace = prepare_workspace(args.fixture_root)
    server = start_server(args.server_command, workspace, args.endpoint)
    report: dict[str, Any] = {
        "conclusion": "INCONCLUSIVE",
        "endpoint": args.endpoint,
        "workspace": str(workspace),
        "fixture_root": str(fixture_root),
        "server_command": args.server_command,
        "codex_version": os.environ.get("CODEX_VERSION"),
        "prompt": "Use only MCP tools to search/read, patch, test, exercise stdin, and inspect diff for deterministic fixtures.",
        "direct_bypass": False,
        "initialize": None,
        "tools": [],
        "cases": [],
        "tool_calls": [],
        "final_git_diff": None,
        "known_limitations": [],
    }
    try:
        client, initialize_result, connect_error = connect(args.endpoint, args.startup_timeout)
        report["initialize"] = initialize_result
        if client is None:
            report["known_limitations"].append(f"No local MCP HTTP server was reachable: {connect_error}")
            write_reports(args.report_json, args.report_md, report)
            write_transcript(args.transcript_json, report)
            return 2
        tools = client.list_tools()
        adapter = ToolAdapter(tools)
        report["tools"] = adapter.names()
        required = [
            "read_file",
            "search_text",
            "apply_patch",
            "exec_command",
            "write_stdin",
            "kill_session",
            "git_diff",
        ]
        missing = adapter.missing(required)
        if missing:
            report["conclusion"] = "FAIL"
            report["known_limitations"].append(f"Required MCP tools missing: {', '.join(missing)}")
            write_reports(args.report_json, args.report_md, report)
            write_transcript(args.transcript_json, report)
            return 1
        runner = DogfoodRunner(client, adapter)
        cases = runner.run_all()
        final_diff = runner.call("git_diff", adapter.git_diff_args())
        report["cases"] = [case.__dict__ for case in cases]
        report["tool_calls"] = [call.__dict__ for call in runner.calls]
        report["final_git_diff"] = result_text(final_diff)
        report["conclusion"] = "PASS" if all(case.status == "PASS" for case in cases) else "FAIL"
        write_reports(args.report_json, args.report_md, report)
        write_transcript(args.transcript_json, report)
        return 0 if report["conclusion"] == "PASS" else 1
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
