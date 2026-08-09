# RM13 Handoff - final Phase 18 and Phase 22 closure

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `f9668dd9667bb704f2ef64bddc1b66621df4e672`
- Implementation commit: `e7005b0`

## Defect Closed

- Phase 18 and Phase 22 now report `complete` only after RM12 passed its Core
  25, aggregate, security/persistence, Linux POSIX, and range-diff gates.
- Phase 22 records the exact candidate-commit aggregate commands, exit codes,
  test counts, and platform skips instead of reusing earlier broad claims.
- The retrospective links the RM00-RM12 handoffs and preserves the historical
  RM12 blocked handoff as historical evidence; it does not rewrite history.
- The benchmark report links the RM10 capacity evidence and RM12B Linux
  evidence. The 500 full-Runtime result remains capacity evidence only and the
  production default remains 128.
- No public schema, Core 25 catalog, DB schema, ADR, or replay semantics were
  changed by RM13.

## Files Changed

- `docs/webcodex-integration-handoffs/STATUS.md`: marks Phases 18 and 22
  complete and links final evidence.
- `docs/webcodex-integration-handoffs/phase-18-session-resilience-final.md`:
  records the passed RM12 gate and bounded recovery wording.
- `docs/webcodex-integration-handoffs/phase-22.md`: records exact RM12
  aggregate and range-diff results.
- `docs/webcodex-integration-handoffs/phase-18-session-resilience-retrospective.md`:
  links all RM handoffs without fabricating RS history.
- `docs/webcodex-session-resilience-benchmark-report.md`: links RM10 and RM12B
  release evidence.
- `docs/webcodex-integration-handoffs/integration-remediation/RM13-final-release-handoff.md`:
  this final handoff.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: no
- Public schema, Core 25 tools, DB schema, ADR, or replay semantics expanded: no
- Real token, transcript DB, OAuth DB, Runner credential, or benchmark JSON staged: no

## Validation

| Command | Exit code | Result | Notes |
| --- | ---: | --- | --- |
| Core 25 aggregate from RM12.1 | 0 | 54 tests; 46 skipped | 9 tool-golden and 37 MCP-contract Windows `/dev/null` fixture skips; schema drift 8 passed. |
| In-process Runtime catalog check | 0 | Passed | Registry 25; exposed list 25; `listChanged=False`. |
| Runner/resilience aggregate from RM12.2 | 0 | 198 tests; 1 skipped | PySide6-only skip; no failures or unhandled thread exception. |
| Security/persistence aggregate from RM12.3 | 0 | 65 tests; 1 skipped | Windows POSIX skip. |
| Ubuntu WSL targeted POSIX permission test on `81072da` | 0 | 1 passed | Settings/config/transcript/database/WAL/SHM/journal modes verified. |
| Ubuntu WSL full `tests.compliance.test_chat_persistence` | 0 | 17 tests; 1 skipped | Only Windows file-sharing behavior skipped. |
| `git diff --check` after RM13 implementation and handoff | 0 | Passed | Final working-tree check. |
| `git diff --check main...HEAD` after RM13 implementation and handoff | 0 | Passed | Final range check. |
| `git diff --name-status main...HEAD` | 0 | Passed | Final range inventory. |

The mandated execution book remains untracked and untouched. No other
uncommitted file changes remain after the handoff commit.

## Security Review

- Full Session ID/token/argv secret exposed: no
- Real credential/data used: no
- Only synthetic loopback, temporary workspace, SQLite, and Ubuntu WSL test
  state were used.
- No public network, dependency installation, or mutating `tools/call` replay
  was performed.

## Remaining Risks

- Linux Landlock enforcement remains a separate platform-conditional limitation;
  the RM12 POSIX permission evidence does not claim Landlock enforcement.
- PySide6-specific UI coverage remains skipped when PySide6 is unavailable.
- Real Codex/model latency and third-party LSP deployment costs remain outside
  the synthetic benchmark scope.
- The standalone Runner CLI supports explicit Runner-local upstream gateway
  configuration through `--upstream-config`; it is not implicitly copied from
  the Control Plane.
- The 500 full-Runtime measurements are Windows capacity evidence only; no
  production default was raised.

## Next Card Preconditions

- No further RM card is required by the remediation execution book. Any future
  release action must rerun the final aggregate and platform-specific gates for
  its own candidate commit.
