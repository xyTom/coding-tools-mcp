# RM05 Handoff — HTTP session close waits for uninstalled Runtime cleanup

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Implementation commit: `70e8298`

## Defect Closed

- `HTTPSessionManager.create()` previously decremented `_creating` and notified `close()` before closing a Runtime that failed to install, allowing manager close to return while Runtime cleanup was still running.
- The create `finally` block now closes an uninstalled Runtime first, then releases the creation reservation in an inner `finally`, so factory failure, install conflict, and manager-closing paths release exactly once even if Runtime.close raises.
- Installed Runtime instances remain owned by the manager and are not closed by the create path.
- Runtime.close remains outside the manager condition/global lock.

## Files Changed

- `coding_tools_mcp/transport_http.py`: order uninstalled Runtime cleanup before reservation release/notification.
- `tests/test_http_session_resilience.py`: replaced timing-based shutdown race coverage with Event-gated Runtime.close coverage that proves `manager.close()` remains blocked until cleanup completes.

## Invariants Preserved

- `close()` waits for uninstalled Runtime.close completion: yes
- Factory failure releases all reservations: yes
- Install conflict releases reservation and closes the rejected Runtime once: yes
- Manager-closing create closes the uninstalled Runtime once: yes
- Installed Runtime is not closed by create cleanup: yes
- Runtime.close under manager condition/global lock: no
- `close()` idempotency and rejection of new create: yes
- Fixed sleep used as synchronization guarantee: no

## Validation

| Command | Exit code | Result |
| --- | ---: | --- |
| `python -m unittest tests.test_http_session_resilience.HTTPSessionLeaseTests.test_shutdown_racing_initialize_closes_uninstalled_runtime_and_returns_reservation` before fix | 1 | Expected red: manager close returned while blocking Runtime.close was still active. |
| Same targeted regression after fix | 0 | Passed. |
| `python -m unittest tests.test_http_session_resilience` | 0 | 13 tests passed. |
| `python -m compileall -q coding_tools_mcp/transport_http.py` | 0 | Passed. |
| `git diff --check` | 0 | Passed. |

## Remaining Risks

- No real HTTP server or production Runtime was used; cleanup ordering is covered at the deterministic manager/factory seam.
- RM06–RM13 remain open; Phase 18/22 must remain `in remediation`.

## Next Card Preconditions

- Start RM06 from implementation commit `70e8298` and this handoff.
- Keep the execution book untracked and modify only the next card's allowlist.
