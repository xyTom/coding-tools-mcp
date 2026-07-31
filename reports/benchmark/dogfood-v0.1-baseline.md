# Coding Tools MCP Dogfood Report

- Conclusion: **PASS**
- Endpoint: `http://127.0.0.1:18774/mcp`
- Workspace: `/tmp/coding-tools-mcp-dogfood-smyj_sdr/workspace`
- Server command: `env PYTHONPATH=/tmp/coding-tools-mcp-v01-VBxmkb python3 -P -m coding_tools_mcp --workspace {workspace} --host 127.0.0.1 --port 18774`
- Codex version: `unknown`
- Direct filesystem/shell bypass during task execution: `False`

## tools/list

- `apply_patch`
- `check_exec_environment`
- `exec_command`
- `get_default_cwd`
- `git_blame`
- `git_diff`
- `git_log`
- `git_show`
- `git_status`
- `kill_session`
- `list_dir`
- `list_files`
- `read_file`
- `read_output`
- `request_permissions`
- `search_text`
- `server_info`
- `set_default_cwd`
- `view_image`
- `write_stdin`

## Efficiency Metrics

- Completion rate: `1.0`
- Total elapsed: `1985.322 ms`
- Tool calls: `18`
- Argument bytes: `1682`
- Result bytes: `18898`
- First patch success: `True`
- First patch success rate: `1.0` across `2` attempts
- All case assertions passed: `True`
- Session poll calls: `0`
- Tool latency p50/p95: `4.851 / 409.371 ms`

## Prompt

Use only MCP tools to search/read, patch, test, exercise stdin, and inspect diff for deterministic fixtures.

## Case Results

### js_bugfix: PASS
- PASS search_text finds add: {"engine": "rg", "matches": [{"after": [], "before": [], "column": 1, "line": 1, "path": "tiny-js-project/src/math.js", "preview": "function add(a, b) {"}], "ok": true, "query":...
- PASS read_file returns buggy source: {"bytes_read": 66, "content": "function add(a, b) {\n  return a - b;\n}\n\nmodule.exports = { add };\n", "encoding": "utf-8", "end_line": 5, "next_start_line": null, "ok": true,...
- PASS apply_patch fixes add: {"affected_files": [{"operation": "update", "path": "tiny-js-project/src/math.js"}], "clean": true, "dry_run": false, "ok": true, "summary": "M tiny-js-project/src/math.js", "wa...
- PASS exec_command npm test passes: {"elapsed_ms": 817, "exit_code": 0, "ok": true, "session_id": "LTYkE_go-4ENbgc--ID8ciay", "signal": null, "status": "exited", "stderr": "", "stderr_dropped_bytes": 0, "stderr_om...
- PASS git_diff shows only math.js fix: {"diff": "--- a/tiny-js-project/src/math.js\n+++ b/tiny-js-project/src/math.js\n@@ -1,5 +1,5 @@\n function add(a, b) {\n\n-  return a - b;\n\n+  return a + b;\n\n }\n\n \n\n mod...

### python_new_function: PASS
- PASS read_file returns python source: {"bytes_read": 32, "content": "def add(a, b):\n    return a + b\n", "encoding": "utf-8", "end_line": 2, "next_start_line": null, "ok": true, "output_bytes": 32, "output_lines": ...
- PASS apply_patch adds multiply: {"affected_files": [{"operation": "update", "path": "tiny-python-project/src/math_utils.py"}], "clean": true, "dry_run": false, "ok": true, "summary": "M tiny-python-project/src...
- PASS exec_command unittest passes: {"elapsed_ms": 334, "exit_code": 0, "ok": true, "session_id": "ewDI7l2Sx0_j7G78YiDMqdRf", "signal": null, "status": "exited", "stderr": "..\n------------------------------------...
- PASS git_diff shows multiply: {"diff": "--- a/tiny-python-project/src/math_utils.py\n+++ b/tiny-python-project/src/math_utils.py\n@@ -1,2 +1,6 @@\n def add(a, b):\n\n     return a + b\n\n+\n\n+\n\n+def multi...

### long_running_stdin: PASS
- PASS exec_command returns session_id: {"elapsed_ms": 89, "exit_code": null, "ok": true, "session_id": "eRES2WV3J1zYF5bvTO3s0HBw", "signal": null, "status": "running", "stderr": "", "stderr_dropped_bytes": 0, "stderr...
- PASS write_stdin accepts hello: {"exit_code": null, "ok": true, "session_id": "eRES2WV3J1zYF5bvTO3s0HBw", "signal": null, "status": "running", "stderr": "", "stderr_dropped_bytes": 0, "stderr_omitted_bytes": 0...
- PASS write_stdin accepts exit: {"exit_code": 0, "ok": true, "session_id": "eRES2WV3J1zYF5bvTO3s0HBw", "signal": null, "status": "exited", "stderr": "", "stderr_dropped_bytes": 0, "stderr_omitted_bytes": 0, "s...
- PASS kill_session terminates or reports already closed: {"evicted": true, "exit_code": 0, "killed": false, "ok": true, "session_id": "eRES2WV3J1zYF5bvTO3s0HBw", "signal": null, "signal_sent": "SIGTERM", "status": "exited", "stderr": ...

### workspace_escape: PASS
- PASS read_file rejects ../ escape: {"error": {"category": "security", "code": "PATH_OUTSIDE_WORKSPACE", "details": {}, "message": "Path escapes the configured workspace.", "retryable": false}, "ok": false}\n{"err...
- PASS apply_patch rejects ../ escape: {"error": {"category": "security", "code": "PATH_OUTSIDE_WORKSPACE", "details": {}, "message": "Path escapes the configured workspace.", "retryable": false}, "ok": false}\n{"err...
- PASS exec_command does not expose outside secret: {"error": {"category": "permission", "code": "PERMISSION_REQUIRED", "details": {"path": "../outside-secret.txt", "permission": "filesystem_escape"}, "message": "Command path esc...

## MCP Tool Calls

- `server_info` ok=True args={}
- `search_text` ok=True args={"path": "tiny-js-project", "query": "function add"}
- `read_file` ok=True args={"path": "tiny-js-project/src/math.js"}
- `apply_patch` ok=True args={"patch": "*** Begin Patch\n*** Update File: tiny-js-project/src/math.js\n@@\n function add(a, b) {\n-  return a - b;\n+  return a + b;\n }\n*** End Patch\n"}
- `exec_command` ok=True args={"cmd": "npm test", "cwd": "tiny-js-project", "max_output_bytes": 40000, "timeout_ms": 20000, "tty": false, "workdir": "tiny-js-project", "yield_time_ms": 20000}
- `git_diff` ok=True args={"path": "tiny-js-project/src/math.js", "paths": ["tiny-js-project/src/math.js"]}
- `read_file` ok=True args={"path": "tiny-python-project/src/math_utils.py"}
- `apply_patch` ok=True args={"patch": "*** Begin Patch\n*** Update File: tiny-python-project/src/math_utils.py\n@@\n def add(a, b):\n     return a + b\n+\n+\n+def multiply(a, b):\n+    return a * b\n*** End Patch\n"}
- `exec_command` ok=True args={"cmd": "/usr/local/bin/python3 -m unittest discover -s tests", "cwd": "tiny-python-project", "max_output_bytes": 40000, "timeout_ms": 20000, "tty": false, "workdir": "tiny-python-project", "yield_time_ms": 20000}
- `git_diff` ok=True args={"path": "tiny-python-project/src/math_utils.py", "paths": ["tiny-python-project/src/math_utils.py"]}
- `exec_command` ok=True args={"cmd": "/usr/local/bin/python3 repl.py", "cwd": "long-running-project", "max_output_bytes": 40000, "timeout_ms": 30000, "tty": true, "workdir": "long-running-project", "yield_time_ms": 1000}
- `write_stdin` ok=True args={"chars": "hello\n", "session_id": "eRES2WV3J1zYF5bvTO3s0HBw"}
- `write_stdin` ok=True args={"chars": "exit\n", "session_id": "eRES2WV3J1zYF5bvTO3s0HBw"}
- `kill_session` ok=True expected_rejection args={"session_id": "eRES2WV3J1zYF5bvTO3s0HBw"}
- `read_file` ok=False expected_rejection args={"path": "../outside-secret.txt"}
- `apply_patch` ok=False expected_rejection args={"patch": "*** Begin Patch\n*** Update File: ../outside-secret.txt\n@@\n-DOGFOOD-OUTSIDE-SECRET\n+MODIFIED\n*** End Patch\n"}
- `exec_command` ok=False expected_rejection args={"cmd": "cat ../outside-secret.txt", "max_output_bytes": 40000, "timeout_ms": 10000, "tty": false, "yield_time_ms": 10000}
- `git_diff` ok=True args={}

## Final Git Diff

```diff
{"diff": "--- a/tiny-js-project/src/math.js\n+++ b/tiny-js-project/src/math.js\n@@ -1,5 +1,5 @@\n function add(a, b) {\n\n-  return a - b;\n\n+  return a + b;\n\n }\n\n \n\n module.exports = { add };\n\n--- a/tiny-python-project/src/math_utils.py\n+++ b/tiny-python-project/src/math_utils.py\n@@ -1,2 +1,6 @@\n def add(a, b):\n\n     return a + b\n\n+\n\n+\n\n+def multiply(a, b):\n\n+    return a * b\n", "files": [{"binary": false, "path": "tiny-js-project/src/math.js", "status": "modified"}, {"binary": false, "path": "tiny-python-project/src/math_utils.py", "status": "modified"}], "ok": true, "output_bytes": 364, "output_lines": 30, "truncated": false, "truncated_by": null, "warnings": ["non-git diff fallback"]}
{"diff": "--- a/tiny-js-project/src/math.js\n+++ b/tiny-js-project/src/math.js\n@@ -1,5 +1,5 @@\n function add(a, b) {\n\n-  return a - b;\n\n+  return a + b;\n\n }\n\n \n\n module.exports = { add };\n\n--- a/tiny-python-project/src/math_utils.py\n+++ b/tiny-python-project/src/math_utils.py\n@@ -1,2 +1,6 @@\n def add(a, b):\n\n     return a + b\n\n+\n\n+\n\n+def multiply(a, b):\n\n+    return a * b\n", "files": [{"binary": false, "path": "tiny-js-project/src/math.js", "status": "modified"}, {"binary": false, "path": "tiny-python-project/src/math_utils.py", "status": "modified"}], "ok": true, "output_bytes": 364, "output_lines": 30, "truncated": false, "truncated_by": null, "warnings": ["non-git diff fallback"]}
```

## Known Limitations

