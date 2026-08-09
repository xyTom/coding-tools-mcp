# RM13B Handoff - concurrent shutdown and Runner remote-ID reuse

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `7d57651ea11def2e80496365272ad6ac51d360fa`
- Implementation commit: `dccef08`

## Defects Closed

- `HTTPSessionManager.close()` now has an explicit shutdown-owner completion
  barrier. Concurrent non-owner callers wait for the owner to finish all
  detached Runtime cleanup instead of inferring completion from counters that
  may not yet have been incremented.
- Runner creation now reserves remote session IDs across installed,
  pending-close, and tombstone states. A candidate Runtime is closed and its
  creation reservation is released when its generated ID is unavailable.
- Runner close cleanup uses identity-checked pending mapping removal, so a
  stale cleanup callback cannot remove a different state's pending close.

## Files Changed

- `coding_tools_mcp/transport_http.py`: explicit HTTP shutdown completion
  barrier.
- `coding_tools_mcp/runner/routing.py`: remote-ID reservation and conditional
  pending cleanup.
- `tests/test_http_session_resilience.py`: controlled Condition regression for
  a concurrent second `close()`.
- `tests/test_runner_mcp_routing.py`: pending-close and tombstone remote-ID
  reuse regressions.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: no
- Public schema, Core 25 tools, DB schema, ADR, and replay semantics changed:
  no

## Validation

| Command | Exit code | Passed / skipped | Notes |
| --- | ---: | --- | --- |
| `python -m unittest tests.test_http_session_resilience.HTTPSessionLeaseTests.test_concurrent_close_waits_for_the_shutdown_owner` before fix | 1 | 1 failure | Stable RED: secondary close returned before the owner completed. |
| `python -m unittest tests.test_runner_mcp_routing.RunnerMcpSessionHostConcurrencyTests.test_pending_remote_id_cannot_be_reused tests.test_runner_mcp_routing.RunnerMcpSessionHostConcurrencyTests.test_tombstone_remote_id_cannot_be_reused` before fix | 1 | 2 failures | Stable RED: both ID reuse attempts were accepted. |
| Same three new regression tests after fix | 0 | 3 passed | HTTP owner barrier and both Runner ID states covered. |
| `python -m unittest tests.test_http_session_resilience tests.test_session_resilience_http_integration tests.test_runner_mcp_routing tests.test_runner_remote tests.test_runner_websocket` | 0 | 77 tests; 0 skips | Adjacent HTTP/Runner regression set. |
| `python -m py_compile coding_tools_mcp/transport_http.py coding_tools_mcp/runner/routing.py` | 0 | Passed | Production syntax check. |
| `git diff --check` | 0 | Passed | Working-tree check before implementation commit. |

The broader Core 25, Runner/resilience aggregate, and security/persistence
release gates were not rerun in this card; earlier results remain historical
evidence only and are not restated as validation of `dccef08`.

## Security Review

- Full Session ID/token/argv secret exposed: no
- Real credential, transcript, OAuth database, benchmark JSON, or public
  network used: no
- No mutating `tools/call` replay was performed.

## Remaining Risks

- The full release aggregates and platform-specific POSIX evidence were not
  rerun after `dccef08`; a merge decision should require those gates on the
  current candidate.
- The execution book remains intentionally untracked and untouched.
- The main worktree was not modified.

## Next Card Preconditions

- Re-run the release aggregate gates on the implementation plus this handoff
  commit before treating the final Phase 18/22 release handoff as current.
