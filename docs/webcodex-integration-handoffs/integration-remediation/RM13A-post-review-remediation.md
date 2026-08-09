# RM13A Handoff - post-review P1 remediation

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `9541d6ee8486b77d0b25d4b93e7cbb76ee8e7016`
- Implementation commit: `73888d5`

## Defects Closed

- HTTP `HTTPSessionManager` now tracks detached Runtime closes and makes every
  close caller wait for creating, active leases, and pending Runtime cleanup.
  Shutdown cannot return while a delete/prune close is still blocked.
- Runner `RunnerMcpSessionHost` now retains pending close state, rechecks
  `close_started` after active-call lease waiting, gives exactly one close owner,
  and makes shutdown wait for detached route cleanup.
- `safe_target()` reconstructs URL netloc from hostname/port, removing userinfo
  as well as query and fragment. Admin target output and resilience keys no
  longer contain URL credentials.
- ADR 0002 now explicitly permits only read-only aggregate transport telemetry
  in the server coordinator; live client/session/transport objects, IDs, tokens,
  and ResultStores remain Runtime-scoped.
- Phase 18/22 and RM13 documentation now states that Runner CLI supports the
  explicit `--upstream-config` option.

## Files Changed

- `coding_tools_mcp/transport_http.py`: pending detached-close tracking and
  shutdown completion barrier.
- `coding_tools_mcp/runner/routing.py`: close ownership recheck and pending
  route cleanup barrier.
- `coding_tools_mcp/upstream.py`: URL userinfo redaction.
- `tests/test_http_session_resilience.py`: deterministic HTTP shutdown race.
- `tests/test_runner_mcp_routing.py`: deterministic Runner shutdown race.
- `tests/compliance/test_upstream_admin_observability.py`: URL target/key
  redaction regression.
- `docs/adr/0002-session-resilience-catalog-template.md`: coordinator telemetry
  ownership clarified.
- Phase 18/22 and RM13 final handoff: stale Runner limitation corrected.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: no
- Public schema, Core 25 tools, DB schema, or replay semantics expanded: no
- Real credential, transcript DB, OAuth DB, or benchmark JSON staged: no

## Validation

| Command | Exit code | Result | Notes |
| --- | ---: | --- | --- |
| HTTP detached-shutdown regression before fix | 1 | Stable red | Shutdown returned while detached `Runtime.close()` was blocked. |
| Runner detached-shutdown regression before fix | 1 | Stable red | Runner shutdown returned while detached route close was blocked. |
| URL userinfo regression before fix | 1 | Stable red | Admin target retained `user:SUPER-SECRET@...`. |
| `python -m unittest tests.test_http_session_resilience tests.test_session_resilience_http_integration tests.test_runner_mcp_routing tests.test_runner_remote tests.test_runner_websocket tests.test_upstream_resilience tests.compliance.test_upstream_admin_observability tests.compliance.test_mcp_admin` | 0 | 110 tests passed | Targeted post-fix regression set. |
| `python -m unittest tests.compliance.test_tool_golden tests.compliance.test_schema_drift tests.compliance.test_mcp_contract` | 0 | 54 tests; 46 skipped | Core contract gate; Windows `/dev/null` fixture skips recorded. |
| RM12 Runner/resilience aggregate | 0 | 201 tests; 1 skipped | PySide6-only skip; no failures or unhandled thread exception. |
| RM12 security/persistence aggregate | 0 | 65 tests; 1 skipped | Windows POSIX skip. |
| `python -m py_compile coding_tools_mcp/transport_http.py coding_tools_mcp/runner/routing.py coding_tools_mcp/upstream.py` | 0 | Passed | Syntax check. |
| `git diff --check` | 0 | Passed | Working-tree check. |
| `git diff --check main...HEAD` | 0 | Passed | Range check. |
| `git diff --name-status main...HEAD` | 0 | Passed | Range inventory. |

## Security Review

- Full Session ID/token/argv secret exposed: no
- URL userinfo and synthetic password are absent from Admin target and
  resilience key output.
- Real credential/data used: no
- No public network or mutating `tools/call` replay performed.

## Remaining Risks

- Platform skips remain explicitly documented: Windows `/dev/null` fixtures,
  one PySide6 UI test, and one Windows file-sharing test.
- Linux Landlock enforcement remains platform-conditional; this remediation does
  not claim it was exercised.
- The execution book remains intentionally untracked. The main worktree's local
  changes are outside this canonical worktree and were not modified.

## Next Card Preconditions

- No additional remediation card is required for these review findings. Any
  merge decision should use implementation commit `73888d5` plus this handoff
  commit and review the intentionally untracked execution book separately.
