# RM06 Handoff — Shared upstream aggregate observability

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Implementation commit: `8cdde84`

## Defect Closed

- Admin status previously read the control Runtime's local `UpstreamManager._clients`, so another Runtime's live client/session and shared 502 backoff were invisible.
- `UpstreamResilienceCoordinator` now owns only redacted aggregate facts per resilience key: live client count, active client Session count, fixed transport-state counts, aggregate state, shared initialization/failure/backoff/circuit fields, and cumulative remote DELETE outcomes.
- `HttpUpstreamClient` updates the coordinator through narrow register/unregister, state-transition, Session 0↔1, and DELETE outcome helpers. No client object, Session ID, principal, workspace, token, or environment credential is stored in the aggregate.
- Aggregate state priority is fixed as `BACKING_OFF > SUSPECT > INITIALIZING > READY > NEW > CLOSED`; shared backoff remains visible after the last live client closes.
- `UpstreamManager.status_payload()` now uses the shared aggregate for Streamable HTTP aliases even when that Runtime has no local client. Existing Admin composition already binds status through this manager callback, so no server composition change was needed.

## Files Changed

- `coding_tools_mcp/upstream_resilience.py`: aggregate snapshot/payload and lifecycle counters per resilience key.
- `coding_tools_mcp/upstream.py`: HTTP client lifecycle helper calls and aggregate-backed status export.
- `tests/test_upstream_resilience.py`: client/session lifecycle, state priority, backoff persistence, and cumulative DELETE regressions.
- `tests/compliance/test_upstream_admin_observability.py`: no-local-client Admin status, strict JSON, bounded aggregate, and redaction regression.

## Invariants Preserved

- Control Runtime and HTTP Runtime clients remain separate objects/sessions: yes
- Shared initialization/backoff/circuit behavior: yes
- Admin status sees all live HTTP Runtime clients for a resilience key: yes
- Active Session count changes 0→1 and 1→0 through lifecycle helpers: yes
- DELETE success/failure remains observable after client close: yes
- Aggregate state priority is deterministic: yes
- Full Session ID, Authorization, env credential, principal/workspace in status: no
- Aggregate payload has bounded fixed state keys and strict JSON scalars: yes
- Lazy catalog and Runtime isolation: preserved

## Validation

| Command | Exit code | Result |
| --- | ---: | --- |
| RM06 aggregate/status tests before fix | 1 | Expected red: aggregate snapshot/session helper/status path was absent. |
| `python -m unittest tests.compliance.test_upstream_admin_observability tests.test_upstream_resilience tests.test_upstream_lazy_catalog` | 0 | 23 tests passed. |
| `python -m unittest tests.compliance.test_upstream_gateway tests.test_upstream_http_integration` | 0 | 39 tests passed. |
| `python -m compileall -q coding_tools_mcp/upstream_resilience.py coding_tools_mcp/upstream.py` | 0 | Passed. |
| `git diff --check` | 0 | Passed. |

## Remaining Risks

- No real multi-process Admin deployment was used; the shared coordinator and status seam are covered with deterministic Runtime/client fakes.
- RM07–RM13 remain open; Phase 18/22 must remain `in remediation`.

## Next Card Preconditions

- Start RM07 from implementation commit `8cdde84` and this handoff.
- Keep the execution book untracked and modify only the next card's allowlist.
