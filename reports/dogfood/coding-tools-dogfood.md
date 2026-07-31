# Coding Tools MCP Dogfood Report

- Conclusion: **PASS**
- Endpoint: `http://127.0.0.1:18772/mcp`
- Workspace: `/tmp/coding-tools-mcp-dogfood-773iw14h/workspace`
- Server command: `python3 -m coding_tools_mcp --workspace {workspace} --host 127.0.0.1 --port 18772`
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
- Total elapsed: `1753.589 ms`
- Tool calls: `18`
- Argument bytes: `1682`
- Result bytes: `11853`
- First patch success: `True`
- First patch success rate: `1.0` across `2` attempts
- All case assertions passed: `True`
- Session poll calls: `0`
- Tool latency p50/p95: `4.166 / 301.214 ms`

## Prompt

Use only MCP tools to search/read, patch, test, exercise stdin, and inspect diff for deterministic fixtures.

## Case Results

### js_bugfix: PASS
- PASS search_text finds add: tiny-js-project/src/math.js:1:1: function add(a, b) {\n{"engine": "rg", "matches": [{"after": [], "before": [], "column": 1, "line": 1, "path": "tiny-js-project/src/math.js", "p...
- PASS read_file returns buggy source: function add(a, b) {\n  return a - b;\n}\n\nmodule.exports = { add };\n\n{"bytes_read": 66, "content": "function add(a, b) {\n  return a - b;\n}\n\nmodule.exports = { add };\n",...
- PASS apply_patch fixes add: Patch applied to 1 file (+1 -1).\nM tiny-js-project/src/math.js\n{"additions": 1, "affected_files": [{"operation": "update", "path": "tiny-js-project/src/math.js"}], "clean": tr...
- PASS exec_command npm test passes: \n> test\n> node test/math.test.js\n\njs ok\n\n{"elapsed_ms": 698, "exit_code": 0, "ok": true, "session_id": "zgpQvTvtcvPJXS6GEdHa3Ep5", "signal": null, "status": "exited", "std...
- PASS git_diff shows only math.js fix: --- a/tiny-js-project/src/math.js\n+++ b/tiny-js-project/src/math.js\n@@ -1,5 +1,5 @@\n function add(a, b) {\n\n-  return a - b;\n\n+  return a + b;\n\n }\n\n \n\n module.export...

### python_new_function: PASS
- PASS read_file returns python source: def add(a, b):\n    return a + b\n\n{"bytes_read": 32, "content": "def add(a, b):\n    return a + b\n", "encoding": "utf-8", "end_line": 2, "next_start_line": null, "ok": true, ...
- PASS apply_patch adds multiply: Patch applied to 1 file (+4 -0).\nM tiny-python-project/src/math_utils.py\n{"additions": 4, "affected_files": [{"operation": "update", "path": "tiny-python-project/src/math_util...
- PASS exec_command unittest passes: stderr:\n..\n----------------------------------------------------------------------\nRan 2 tests in 0.000s\n\nOK\n\n{"elapsed_ms": 228, "exit_code": 0, "ok": true, "session_id":...
- PASS git_diff shows multiply: --- a/tiny-python-project/src/math_utils.py\n+++ b/tiny-python-project/src/math_utils.py\n@@ -1,2 +1,6 @@\n def add(a, b):\n\n     return a + b\n\n+\n\n+\n\n+def multiply(a, b):...

### long_running_stdin: PASS
- PASS exec_command returns session_id: ready\n\n{"elapsed_ms": 69, "exit_code": null, "next_action": {"arguments": {"chars": "", "session_id": "7VIBSjPE-CWys7vpvwf5cfYN", "yield_time_ms": 10000}, "tool": "write_stdi...
- PASS write_stdin accepts hello: hello\necho:hello\n\n{"exit_code": null, "next_action": {"arguments": {"chars": "", "session_id": "7VIBSjPE-CWys7vpvwf5cfYN", "yield_time_ms": 10000}, "tool": "write_stdin"}, ...
- PASS write_stdin accepts exit: exit\nbye\n\n{"exit_code": 0, "ok": true, "session_id": "7VIBSjPE-CWys7vpvwf5cfYN", "signal": null, "status": "exited", "stderr": "", "stderr_dropped_bytes": 0, "stderr_omitte...
- PASS kill_session terminates or reports already closed: Session 7VIBSjPE-CWys7vpvwf5cfYN: exited.\n{"evicted": true, "exit_code": 0, "killed": false, "ok": true, "session_id": "7VIBSjPE-CWys7vpvwf5cfYN", "signal": null, "signal_sent"...

### workspace_escape: PASS
- PASS read_file rejects ../ escape: PATH_OUTSIDE_WORKSPACE: Path escapes the configured workspace.\n{"error": {"category": "security", "code": "PATH_OUTSIDE_WORKSPACE", "details": {}, "message": "Path escapes the ...
- PASS apply_patch rejects ../ escape: PATH_OUTSIDE_WORKSPACE: Path escapes the configured workspace.\n{"error": {"category": "security", "code": "PATH_OUTSIDE_WORKSPACE", "details": {}, "message": "Path escapes the ...
- PASS exec_command does not expose outside secret: PERMISSION_REQUIRED: Command path escapes the workspace and is blocked.\n{"error": {"category": "permission", "code": "PERMISSION_REQUIRED", "details": {"path": "../outside-secr...

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
- `write_stdin` ok=True args={"chars": "hello\n", "session_id": "7VIBSjPE-CWys7vpvwf5cfYN"}
- `write_stdin` ok=True args={"chars": "exit\n", "session_id": "7VIBSjPE-CWys7vpvwf5cfYN"}
- `kill_session` ok=True expected_rejection args={"session_id": "7VIBSjPE-CWys7vpvwf5cfYN"}
- `read_file` ok=False expected_rejection args={"path": "../outside-secret.txt"}
- `apply_patch` ok=False expected_rejection args={"patch": "*** Begin Patch\n*** Update File: ../outside-secret.txt\n@@\n-DOGFOOD-OUTSIDE-SECRET\n+MODIFIED\n*** End Patch\n"}
- `exec_command` ok=False expected_rejection args={"cmd": "cat ../outside-secret.txt", "max_output_bytes": 40000, "timeout_ms": 10000, "tty": false, "yield_time_ms": 10000}
- `git_diff` ok=True args={}

## Final Git Diff

```diff
--- a/tiny-js-project/src/math.js
+++ b/tiny-js-project/src/math.js
@@ -1,5 +1,5 @@
 function add(a, b) {

-  return a - b;

+  return a + b;

 }



 module.exports = { add };

--- a/tiny-python-project/src/math_utils.py
+++ b/tiny-python-project/src/math_utils.py
@@ -1,2 +1,6 @@
 def add(a, b):

     return a + b

+

+

+def multiply(a, b):

+    return a * b

{"diff": "--- a/tiny-js-project/src/math.js\n+++ b/tiny-js-project/src/math.js\n@@ -1,5 +1,5 @@\n function add(a, b) {\n\n-  return a - b;\n\n+  return a + b;\n\n }\n\n \n\n module.exports = { add };\n\n--- a/tiny-python-project/src/math_utils.py\n+++ b/tiny-python-project/src/math_utils.py\n@@ -1,2 +1,6 @@\n def add(a, b):\n\n     return a + b\n\n+\n\n+\n\n+def multiply(a, b):\n\n+    return a * b\n", "files": [{"binary": false, "path": "tiny-js-project/src/math.js", "status": "modified"}, {"binary": false, "path": "tiny-python-project/src/math_utils.py", "status": "modified"}], "ok": true, "output_bytes": 364, "output_lines": 30, "truncated": false, "truncated_by": null, "warnings": ["non-git diff fallback"]}
```

## Known Limitations

