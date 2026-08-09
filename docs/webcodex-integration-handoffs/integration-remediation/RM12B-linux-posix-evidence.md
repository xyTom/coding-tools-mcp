# RM12B Handoff - Linux POSIX evidence and aggregate closure

## Status

- Result: complete; RM12 release gates pass for candidate commit `81072da`
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `823e341b15835bcde251bfe289067f44400f02b6`
- Implementation commit: `81072da`

Phase 18 and Phase 22 remain `in remediation` until RM13 records the final
handoff. This document closes the RM12 evidence gate; it does not itself change
release status.

## Defect Closed

- Ubuntu WSL was available outside the Codex sandbox and supplied the required
  independent Linux POSIX permission environment.
- The first real Linux run found a test-observation defect: the idle SQLite
  connection did not retain WAL/SHM sidecars after the store's write connection
  closed, so the test asserted existence after SQLite had legally checkpointed
  and removed the files.
- The permission test now holds an active read transaction while the store
  writes. WAL and SHM remain observable and both have mode `0600`; the database,
  rollback journal, settings file, config directory, and transcript directory
  permission assertions also pass.
- No production persistence behavior or database schema changed.

## Files Changed

- `tests/compliance/test_chat_persistence.py`: keeps a SQLite read transaction
  active while asserting WAL/SHM sidecar modes.

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
| Ubuntu WSL POSIX permission test before the test fix | 1 | Stable red | `transcripts.sqlite3-wal` had already been removed by SQLite. |
| Ubuntu WSL SQLite probe with idle connection | 0 | Diagnostic | Journal mode was `wal`; sidecars were checkpointed/removed after the store write. |
| Ubuntu WSL SQLite probe with active read transaction | 0 | Diagnostic | WAL/SHM persisted and both reported mode `0600`. |
| Ubuntu WSL targeted POSIX permission test after commit `81072da` | 0 | 1 passed | Real Linux settings/transcript/database/WAL/SHM/journal permission evidence. |
| Ubuntu WSL `python3 -m unittest tests.compliance.test_chat_persistence` | 0 | 17 tests; 1 skipped | Only Windows file-sharing behavior skipped; POSIX permission test passed. |
| Windows `python -m unittest tests.compliance.test_chat_persistence` | 0 | 17 tests; 1 skipped | POSIX-only permission test skipped on Windows as designed. |
| RM12 Core 25 aggregate on `81072da` | 0 | 54 tests; 46 skipped | 9 tool-golden and 37 MCP-contract Windows `/dev/null` fixture skips; schema-drift 8 passed. |
| In-process Runtime catalog check | 0 | Passed | Registry 25; exposed list 25; `listChanged=False`. |
| RM12 Runner/resilience aggregate on `81072da` | 0 | 198 tests; 1 skipped | PySide6-only test skipped; no failures or unhandled thread exception. |
| RM12 security/persistence aggregate on `81072da` | 0 | 65 tests; 1 skipped | Windows POSIX skip is complemented by the successful Ubuntu WSL test above. |
| `git diff --check` | 0 | Passed | Working-tree check. |
| `git diff --check main...HEAD` | 0 | Passed | Required range check. |
| `git diff --name-status main...HEAD` | 0 | Passed | Range inventory completed. |

## Security Review

- Full Session ID/token/argv secret exposed: no
- Real credential/data used: no
- Tests used temporary synthetic settings and SQLite files only.
- No public network, dependency installation, or mutating `tools/call` replay
  was used.

## Remaining Risks / RM13 Preconditions

- Core platform skips remain explicitly recorded; the frozen catalog was
  independently verified as exactly 25 tools with `listChanged=False`.
- Linux evidence was produced on Ubuntu WSL with Python 3.10.12. It verifies
  POSIX file modes for this candidate commit, not unrelated Linux distribution
  or filesystem behavior.
- RM13 may now update Phase 18/22 status and final evidence using these exact
  results. It must rerun final diff/status checks and must not rewrite earlier
  blocked handoffs as if they had passed at the time.
