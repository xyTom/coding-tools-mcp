# RM01 Handoff — HTTP upstream recovery and authoritative headers

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `2e40b54ba13cbc74c0a8c8d970f33281f741c7e9`
- Implementation commit: `0ed327fd6861fe7e1bbcce31e8b21b01c971d43a`

## Defect Closed

- Non-2xx `HTTPError` responses no longer return a decoded JSON-RPC body as success. The HTTP status remains in `UpstreamError.details["status"]`.
- 404/410 stale-session failures clear the local Session and enter `NEW`.
- 5xx, timeout, disconnect, and reset failures clear the local Session before entering `BACKING_OFF`; the next initialize therefore cannot carry the old Session header.
- DELETE and POST protected headers are filtered case-insensitively before authoritative Session, protocol, and resolved Authorization headers are applied.
- The current ambiguous `tools/call` is still sent exactly once; no automatic replay was added.

## Files Changed

- `coding_tools_mcp/upstream.py`: preserve HTTP failure classification, clear stale transport state, and enforce protected header precedence.
- `tests/test_upstream_http_integration.py`: loopback HTTP coverage for valid error bodies, recovery headers, timeout/disconnect, stale Session statuses, DELETE header precedence, and single mutating-call delivery.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: unchanged
- Real upstream credential or transcript data used: no

## Validation

| Command | Exit code | Passed / skipped | Notes |
| --- | ---: | --- | --- |
| `python -m unittest tests.test_upstream_http_integration` before fix | 1 | 0 passed / 7 failed | Stable repro: HTTP 502/404/410 bodies were accepted, Session was retained after timeout/disconnect, and DELETE used the configured attacker Session header. |
| `python -m unittest tests.test_upstream_resilience tests.test_upstream_http_integration` | 0 | 14 passed | Existing resilience test emits the pre-existing initialization-close race traceback from its background thread; unittest still exits 0. This is retained for later thread-cleanup/review and was present in RM00. |
| `python -m unittest tests.compliance.test_upstream_gateway tests.compliance.test_upstream_lifecycle` | 0 | 40 passed | Gateway and lifecycle regression suite passed. |
| `pytest -W error::pytest.PytestUnhandledThreadExceptionWarning tests/test_upstream_http_integration.py` | 0 | 5 passed | New loopback HTTP suite passes with warning-as-error. |
| `git diff --check` | 0 | pass | No whitespace errors. |

## Security Review

- Full Session ID/token/argv secret exposed: no
- Configured protected header can override the live Session: no
- HTTP error body is bounded and discarded as diagnostic input; it cannot change HTTP classification or become a successful result.
- Real credential/data used: no

## Remaining Risks

- The existing background initialize/close test still prints an expected `UpstreamError` traceback from its worker thread; RM05/RM07 should address or formally capture this without weakening close semantics.
- RM02–RM13 remain open. Phase 18/22 must remain `in remediation`.

## Next Card Preconditions

- Start RM02 from `0ed327fd6861fe7e1bbcce31e8b21b01c971d43a`.
- Keep the execution book untracked and modify only the next card allowlist.
