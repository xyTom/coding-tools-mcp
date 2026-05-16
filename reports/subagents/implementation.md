# Implementation Engineer Report

## Task Scope

- Role: `implementation-engineer`.
- Waited for research, contract, security, and test-harness direction before creating runtime code.
- Owned implementation and packaging files:
  - `codex_tool_runtime_mcp/`
  - `pyproject.toml`
  - `.gitignore` packaging hygiene update
  - generated runtime validation reports under `reports/compliance/` and `reports/dogfood/`
- Did not edit tests, docs, or other subagent reports.

## Implementation Summary

Implemented a stdlib-only Python MCP runtime server with:

- Streamable HTTP endpoint at `/mcp`.
- P1 stdio transport via `--stdio`.
- Fixed P0 tool inventory:
  - `read_file`
  - `list_dir`
  - `list_files`
  - `search_text`
  - `apply_patch`
  - `exec_command`
  - `write_stdin`
  - `kill_session`
  - `git_status`
  - `git_diff`
  - `request_permissions`
- Optional P1 `view_image`, hidden unless `--enable-view-image` or `CODEX_TOOL_RUNTIME_ENABLE_VIEW_IMAGE=1` is set.
- Workspace path confinement with rejection of absolute paths, `..` traversal, and symlink escapes.
- Codex-style `apply_patch` envelope support for add, update, delete, and move, with staged rollback on write failure.
- Exec process management with bounded output, timeout handling, process-group termination, sessions, stdin, and kill.
- Command policy for workspace path escapes, symlink arguments, destructive commands, network-looking commands, and sensitive env keys.
- Structured MCP tool success/error results with `content`, `structuredContent`, and `isError`.
- Git status/diff for git workspaces, plus a patch-preimage fallback diff for non-git dogfood fixtures.
- No forbidden product-layer tools such as Codex wrappers, web search, login, memory, marketplace, model routing, or subagent spawning.

## Key Design Notes

- The runtime has one canonical workspace root per server process.
- Path-taking tools share a single resolver.
- `exec_command` permits absolute argv[0] executables, because normal toolchains live outside the workspace, but blocks absolute or escaping data-path arguments.
- Child environments are allowlisted and do not inherit compliance-injected secrets such as `AWS_SECRET_ACCESS_KEY` or `OPENAI_API_KEY`.
- `request_permissions` is intentionally non-granting in this implementation and returns `ELICITATION_UNSUPPORTED` unless a future client approval path is added.
- `view_image` is implemented and tested when explicitly enabled, but is not exposed by default to preserve the P0 profile behavior.

## Validation Performed

- `python -m py_compile codex_tool_runtime_mcp/*.py`
- `python -m pip install -e .`
- `make test-mcp-contract`
- `make test-tool-golden`
- `make test-security`
- `make test-e2e`
- `make test-codex-compat`
- `make dogfood-mcp`
- `make compliance`
- `CODEX_TOOL_RUNTIME_SERVER_CMD='codex-tool-runtime-mcp --enable-view-image --workspace {workspace} --host 127.0.0.1 --port {port}' make test-e2e`
- `CODEX_TOOL_RUNTIME_SERVER_CMD='codex-tool-runtime-mcp --enable-view-image --workspace {workspace} --host 127.0.0.1 --port {port}' make test-codex-compat`
- stdio smoke test using `codex-tool-runtime-mcp --stdio --workspace .`
- `python benchmarks/dogfood/mcp_deterministic_runner.py --server-command 'codex-tool-runtime-mcp --workspace {workspace} --host 127.0.0.1 --port 8765'`
- `python benchmarks/swebench/run_smoke.py`

## Current Results

- Compliance: PASS
  - Report: `reports/compliance/latest.md`
  - JSON: `reports/compliance/latest.json`
  - Tests: 29 run, 29 passed, 2 skipped because default profile does not expose P1 `view_image`.
- P1 `view_image` enabled checks: PASS for e2e and Codex compatibility suites.
- stdio smoke: PASS.
- Deterministic Codex-on-MCP dogfood runner: PASS.
  - Report: `reports/dogfood/codex-on-mcp.md`
  - JSON: `reports/dogfood/codex-on-mcp.json`
- SWE-bench smoke preflight: INCONCLUSIVE.
  - Report: `reports/benchmark/swebench-regression.md`
  - JSON: `reports/benchmark/swebench-regression.json`
  - Docker is unavailable, the `swebench` package is not installed, and prediction files are placeholders.

## Known Gaps

- No OS/container sandbox is implemented. P0 command safety is policy-based and workspace-aware, not a full syscall/network sandbox.
- Permission approval is not interactive yet; `request_permissions` reports unsupported elicitation.
- `apply_patch` supports the tested Codex envelope subset but not every advanced Codex parser leniency.
- Output buffering is bounded, but there is no cursor API beyond session stdin/poll responses.
- `git_diff` fallback for non-git workspaces is based on patch preimages recorded by this server process, not a substitute for real git history.
- Official SWE-bench evaluation was not run because required environment dependencies are missing.

## Follow-up Action Items

1. Add OS-level sandbox profiles or container isolation for command execution.
2. Implement MCP elicitation-backed permission grants.
3. Expand `apply_patch` parser compatibility with additional Codex fixture scenarios.
4. Add richer session output cursoring and idle cleanup.
5. Provision Docker plus the official SWE-bench harness and replace placeholder predictions with real baseline/candidate runs.
