# RM12A Handoff - local release-gate remediation

## Status

- Result: local implementation complete; RM12 remains blocked on independent Linux POSIX evidence
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `455ed4da44e902d8d6b3336599d4cf39e93c06a4`
- Implementation commit: `320d72c`

## Defects Closed

- `guard_allow_roots()` no longer aborts Runtime command startup when an
  inherited PATH entry resolves but cannot be statted. The inaccessible entry
  is omitted from the allow-root set; no additional root is granted.
- The local WorkspaceHost structured validation path again reaches
  `Runtime.exec_command` and completes `python:syntax` on the Windows
  validation host.
- The extra EOF blank line in
  `docs/webcodex-feature-integration-agent-execution-plan.md` was removed.
  The committed `git diff --check main...HEAD` range now returns exit 0.

## Root Cause

`_guard_allow_roots_cached()` protected `Path.resolve()` with `except OSError`
but called `resolved.is_dir()` outside that guard. One inherited Windows PATH
entry resolved to a directory whose metadata access raised `PermissionError`
(`WinError 5`), so Runtime command startup failed before process spawn and
StructuredValidationBackend projected the exception as `unavailable`.

## Files Changed

- `coding_tools_mcp/server.py`: treats PATH resolution and directory probing as
  one fallible discovery operation.
- `tests/compliance/test_runtime_helpers.py`: adds a deterministic regression
  for an inaccessible PATH entry.
- `docs/webcodex-feature-integration-agent-execution-plan.md`: removes the EOF
  blank line reported by the range diff gate.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: no
- Public schema, Core 25 tools, DB schema, ADR, or replay semantics expanded: no
- Inaccessible PATH entries grant filesystem access: no; they are skipped

## Validation

| Command | Exit code | Result | Notes |
| --- | ---: | --- | --- |
| Existing WorkspaceHost validation test, run three times before the fix | 1 each | Stable red | Each run received `unavailable` instead of `passed`. |
| New `test_guard_allow_roots_skips_path_entries_that_cannot_be_statted` before production change | 1 | Stable red | Raised the synthetic `PermissionError` from `Path.is_dir()`. |
| New guard regression plus original WorkspaceHost validation test after commit | 0 | 2 passed | Public Runtime/WorkspaceHost behavior restored. |
| `python -m unittest tests.compliance.test_runtime_helpers tests.test_workspace_host` | 0 | 88 tests; 9 skipped | Adjacent runtime and host regression set. |
| RM12 Runner/resilience aggregate | 0 | 198 tests; 1 skipped | PySide6-only desktop test skipped; no failures or unhandled thread exception. |
| RM12 Core 25 aggregate | 0 | 54 tests; 46 skipped | 9 tool-golden and 37 MCP-contract Windows `/dev/null` fixture skips; schema-drift 8 passed. |
| In-process Runtime catalog check | 0 | Passed | Registry 25; exposed list 25; `listChanged=False`. |
| RM12 security/persistence aggregate | 0 | 65 tests; 1 skipped | Windows POSIX permission test remains skipped. |
| `git diff --check` after implementation commit | 0 | Passed | Working tree check. |
| `git diff --check main...HEAD` after implementation commit | 0 | Passed | Range EOF issue closed. |
| `git diff --name-status main...HEAD` | 0 | Passed | Range inventory command completed. |
| Linux POSIX test via `wsl.exe -d Ubuntu` | -1 | Not run | `WSL_E_DISTRO_NOT_FOUND`; no Linux result is claimed. |

## Security Review

- Full Session ID/token/argv secret exposed: no
- Real credential/data used: no
- No public network, dependency installation, or mutating `tools/call` replay
  was used.

## Remaining Risk / Next Preconditions

- RM12 is still blocked until
  `tests.compliance.test_chat_persistence.ChatPersistenceTests.test_startup_settings_transcript_and_sqlite_sidecars_are_private`
  passes on a real Linux host or Linux CI for the exact candidate commit.
- Windows skip, workflow configuration, and the absence of a local WSL distro
  are not substitutes for that evidence.
- After Linux evidence exists, rerun/record the RM12 gates for the candidate
  commit before RM13 changes Phase 18 or Phase 22 to complete.
