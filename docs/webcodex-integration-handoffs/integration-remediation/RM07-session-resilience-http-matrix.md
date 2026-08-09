# RM07 Handoff — Real HTTP three-layer session resilience matrix

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Implementation commit: `ec6c545`

## Defect / Evidence Closed

- The audit gap was missing evidence across all three HTTP layers: deterministic `HTTPSessionManager`, real `RuntimeHTTPServer` POST/DELETE, and `HttpUpstreamClient` against a loopback fake upstream.
- Added a self-contained `127.0.0.1:0` fixture with Event-controlled timeout/disconnect actions, bounded teardown, collected fake-handler/serve-thread exceptions, and no fixed sleep synchronization.
- Added real RuntimeHTTPServer coverage proving POST initialize returns a real Session ID, DELETE closes the session once, and duplicate DELETE returns 404 without a second Runtime.close.
- Added upstream POST coverage for 404/410/502/503/504, timeout, disconnect, and valid JSON-RPC bodies; HTTP status remains authoritative, stale/ambiguous calls do not replay, and Session state is cleared.
- Added upstream DELETE coverage for 200/204/404/410/403/502/timeout, duplicate close idempotency, authoritative protected headers, and never-initialized close without DELETE.
- Added the deterministic manager shutdown fixture proving `close()` waits for an uninstalled Runtime close.
- Existing close-vs-initialize test now captures its expected `UPSTREAM_NOT_AVAILABLE` in the worker and asserts it on the main test thread, eliminating the pytest unhandled-thread warning without changing production behavior.

## Files Changed

- `tests/test_session_resilience_http_integration.py`: new RM07 three-layer real HTTP matrix and bounded server fixtures.
- `tests/test_upstream_resilience.py`: minimal exception collection for an existing expected close race.

No production files were changed. No public schema, Core 25 tool, database schema, ADR, catalog mutation, or replay behavior was changed.

## Invariants Preserved

- RuntimeHTTPServer binds loopback ephemeral port only: yes
- POST initialize / DELETE use real HTTP: yes
- Duplicate DELETE does not close a Runtime twice: yes
- Never-initialized upstream close sends no DELETE: yes
- HTTP DELETE 404/410 are treated as already closed; 403/502/timeout are failures: yes
- HTTP POST status remains authoritative even with a valid JSON-RPC body: yes
- Ambiguous tools/call is sent once and never replayed: yes
- Protected Session/Protocol/Authorization headers cannot be overridden by config: yes
- Server and handler thread exceptions are collected and asserted: yes
- Fixed sleep used as synchronization: no

## Validation

| Command | Exit code | Result |
| --- | ---: | --- |
| `python -m unittest tests.test_session_resilience_http_integration` initial run | 1 | Matrix assertion expected one Session-bearing POST but the protocol correctly produced two (`notifications/initialized` and `tools/call`); corrected the test expectation. |
| `python -m unittest tests.test_session_resilience_http_integration` | 0 | 5 tests passed. |
| `python -m unittest tests.test_session_resilience_http_integration tests.test_http_session_resilience tests.test_upstream_resilience` | 0 | 30 tests passed. |
| `pytest -W error::pytest.PytestUnhandledThreadExceptionWarning tests/test_session_resilience_http_integration.py tests/test_http_session_resilience.py tests/test_upstream_resilience.py` initial run | 1 | Existing close-race test exposed an unhandled expected worker exception. |
| Same pytest command after fixture fix | 0 | 30 passed; no skips. |
| `python -m compileall -q tests/test_session_resilience_http_integration.py tests/test_upstream_resilience.py` | 0 | Passed. |
| `git diff --check` | 0 | Passed. |

## Remaining Risks

- The fake upstream is loopback and deterministic; no external upstream, real token, or production credential was used.
- RM08–RM13 remain open; Phase 18/22 must remain `in remediation`.

## Next Card Preconditions

- Start RM08 from implementation commit `ec6c545` and this handoff.
- Keep the execution book untracked and modify only the next card's allowlist.
