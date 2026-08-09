# RM04 Handoff — Runner MCP create/call/close/shutdown ownership

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Implementation commit: `5f755d7`

## Defect Closed

- Concurrent `mcp.create` calls could oversell `max_sessions` because factory work was not reserved in the capacity count.
- Same-control creates were not single-flight, so concurrent identical requests could construct multiple Runtime instances.
- `close_session()` and `shutdown()` could call `Runtime.close()` while a Runtime method was active, and shutdown could return before a factory-created but not-yet-installed Runtime was closed.
- `RunnerMcpSessionHost` now uses a `Condition` with `_creating` reservations, `_creating_by_control` identity gates, and mutable `_SessionState` records carrying `active_call_leases` and close ownership.
- Factory execution and all Runtime I/O remain outside the host condition. Factory failure, duplicate remote IDs, and shutdown races release reservations and close uninstalled Runtime instances before shutdown completion.
- `call_session()` acquires a lease after route/auth validation and releases it in `finally`; close marks the state closing, waits for leases, claims close ownership, detaches, and closes outside the condition.
- Shutdown is single-flight, blocks new create/call operations, waits for reservations and active leases, and waits for or claims each Runtime close exactly once.

## Files Changed

- `coding_tools_mcp/runner/routing.py`: condition-based Runner MCP session ownership, create reservations, call leases, close/shutdown coordination, and bounded tombstone handling.
- `tests/test_runner_mcp_routing.py`: deterministic Event-gated regressions for create capacity/single-flight, cleanup, call/close ordering, shutdown races, and lock-free Runtime I/O.

## Invariants Preserved

- Cross-principal/workspace/runner fail-closed routing: yes
- Same-identity duplicate create reuses one installed record: yes
- Factory reservation counted against `max_sessions`: yes
- Active Runtime method cannot overlap `Runtime.close()`: yes
- New calls after close begins are rejected: yes (`RUNNER_SESSION_CLOSING`)
- New calls after shutdown begins are rejected: yes (`RUNNER_SHUTTING_DOWN`)
- Runtime I/O under the host condition/global lock: no
- Existing reconnect storm avoids new Runtime creation: yes, covered by existing test

## Validation

| Command | Exit code | Result |
| --- | ---: | --- |
| `python -m unittest tests.test_runner_mcp_routing.RunnerMcpSessionHostConcurrencyTests` before fix | 1 | Expected red: 6 of 9 RM04 concurrency tests failed. |
| Same targeted concurrency suite after fix | 0 | 9 tests passed. |
| `python -m unittest tests.test_runner_mcp_routing tests.test_runner_remote tests.test_runner_websocket` | 0 | 49 tests passed. |
| `python -m compileall -q coding_tools_mcp/runner/routing.py` | 0 | Passed. |
| `git diff --check` | 0 | Passed. |

## Remaining Risks

- No real upstream HTTP service or production Runtime was used; race coverage uses deterministic Event-gated fakes at the Runner host seam.
- RM05–RM13 remain open; Phase 18/22 must remain `in remediation`.

## Next Card Preconditions

- Start RM05 from the implementation and handoff commits for RM04.
- Keep the execution book untracked and modify only the next card's allowlist.
