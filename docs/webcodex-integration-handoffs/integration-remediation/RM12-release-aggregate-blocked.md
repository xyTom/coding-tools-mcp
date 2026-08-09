# RM12 Handoff - release aggregate blocked

## Status

- Result: blocked
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `b6e7829b9d03d089fbfd1b8eea004fcf49f268c9`
- Implementation commit: none; RM12 made no production or test changes.

## Defect / Gate Outcome

- The Core 25 aggregate returned exit 0, but 46 of 54 tests were skipped on
  Windows because the compliance fixture requires readable/writable POSIX
  `/dev/null`. Module distribution: `test_tool_golden` 9 skipped,
  `test_schema_drift` 8 passed, and `test_mcp_contract` 37 skipped.
- The Core catalog was independently checked in-process: `TOOL_REGISTRY=25`,
  `list_tools=25`, and `initialize().capabilities.tools.listChanged=False`.
- The Runner/resilience aggregate failed with 198 tests, 1 failure, and 1
  skip. The failure was
  `WorkspaceHostFactoryTests.test_local_validation_runs_through_workspace_runtime_exec_boundary`:
  expected `passed`, received `unavailable`.
- A focused diagnostic reproduced the first root cause without changing
  production code: the structured validation executor returned unavailable
  because Runtime command startup raised Windows `WinError 5` for a PATH entry
  ending in `AppData\\Roaming\\npm`. This needs a separately scoped owner/RM;
  RM12 is not authorized to repair it.
- The security/persistence aggregate returned exit 0: 65 tests, 1 skip. The
  skip was `test_startup_settings_transcript_and_sqlite_sidecars_are_private`
  with reason `POSIX permission modes are not portable to Windows`.
- An independent Linux POSIX permission probe was attempted with
  `wsl.exe -d Ubuntu`; it returned exit -1 with `WSL_E_DISTRO_NOT_FOUND`. No
  Linux permission evidence is claimed.
- Working-tree `git diff --check` returned exit 0. The required
  `git diff --check main...HEAD` returned exit 2 due to the existing extra EOF
  blank line at `docs/webcodex-feature-integration-agent-execution-plan.md:1947`.
  The range `git diff --name-status main...HEAD` returned exit 0.

Because a required aggregate and both platform/range release conditions are
not fully satisfied, RM12 remains blocked. Phase 18 and Phase 22 must remain
`in remediation`; no release-complete handoff is issued.

## Files Changed

- `docs/webcodex-integration-handoffs/integration-remediation/RM12-release-aggregate-blocked.md`:
  records the exact release-gate outcomes and the first observed failure.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: no
- Public schema, Core 25 tools, DB schema, ADR, or replay semantics expanded: no
- Real credential, transcript DB, OAuth DB, or benchmark JSON staged: no

## Validation

| Command | Exit code | Result | Notes |
| --- | ---: | --- | --- |
| `python -m unittest tests.compliance.test_tool_golden tests.compliance.test_schema_drift tests.compliance.test_mcp_contract` | 0 | Gate incomplete | 54 tests; 46 Windows `/dev/null` fixture skips. |
| `python -c "... Runtime/list_tools/initialize ..."` | 0 | Passed | Registry 25; exposed list 25; `listChanged=False`. |
| `python -m unittest tests.test_http_session_resilience tests.test_session_resilience_http_integration tests.test_upstream_resilience tests.test_upstream_lazy_catalog tests.compliance.test_upstream_admin_observability tests.compliance.test_upstream_gateway tests.compliance.test_upstream_lifecycle tests.test_runner_mcp_routing tests.test_runner_capabilities tests.test_runner_websocket tests.test_runner_remote tests.test_workspace_host tests.test_operator_api tests.test_validation_backend tests.compliance.test_agent_platform tests.test_desktop_client` | 1 | Failed | 198 tests; one WorkspaceHost validation failure; one PySide6 skip. |
| `python -m unittest tests.compliance.test_mcp_admin tests.compliance.test_chat_persistence tests.test_oauth_integration tests.test_oauth_fail_closed tests.test_oauth_refresh tests.test_oauth_signing tests.test_oauth_store` | 0 | Passed with skip | 65 tests; one Windows POSIX permission skip. |
| `wsl.exe -d Ubuntu -- python3 -c "import os; print(os.name)"` | -1 | Skipped/unavailable | WSL distro not found; no Linux evidence. |
| `git diff --check` | 0 | Passed | Working-tree check. |
| `git diff --check main...HEAD` | 2 | Failed | Existing execution-plan EOF blank line at line 1947. |
| `git diff --name-status main...HEAD` | 0 | Passed | Range inventory collected for audit. |
| `git status --short --branch` | 0 | Passed with intentional untracked file | Only the execution book is untracked; it remains untouched. |

No implementation commit was created because RM12's failure rule prohibits a
release-complete change and requires returning to the corresponding scoped
remediation owner rather than making an unrelated fix in this card.

## Security Review

- Full Session ID/token/argv secret exposed: no
- Real credential/data used: no
- No mutating `tools/call` replay performed.

## Remaining Risks / Required Follow-up

- The WorkspaceHost local validation failure must be diagnosed and fixed under
  its own scoped remediation before RM12 can be rerun.
- A Linux host or installed Linux CI/WSL distro must provide independent
  transcript/config/SQLite sidecar permission evidence.
- The tracked execution-plan EOF blank line must be corrected by an authorized
  card, then `git diff --check main...HEAD` must be rerun.
- RM12 must rerun all aggregate gates after those conditions change; RM13 may
  issue the final handoff only after every required gate passes.
