# Conversation Center Final Verification

**Status: COMPLETE**

Verified on 2026-08-24. The implementation, HTTP acceptance, browser acceptance,
privacy scan, and all test/build/static gates below passed. A final independent
review also corrected owner-scoped MCP list `truncated` metadata and added a
regression assertion before rerunning the required gates. A later real startup
reproduction exposed an Agent Session keyset-pagination binding defect; that
defect is also fixed and covered by an over-one-batch regression test.

## Architecture And Continuity

- Conversation binding storage is schema v3 with explicit ownership namespace,
  `repo_scope_digest`, and fail-closed MCP claimability.
- Historical Agent Session backfill scans the full store and records ambiguity
  rather than guessing an owner.
- MCP automatic continuity requires exact principal, window, Workspace, and
  repository scope. A new transport does not inherit a recent Conversation;
  explicit owner-scoped resume is required after identity loss.
- Transcript, Agent Session, durable Conversation bindings, live MCP transport
  sessions, and backend state remain separate stores/identities.
- Evidence producers record compact allowlisted paths, job/checkpoint state, and
  validation state through production dispatch. Retention is capped at 500 rows;
  attempt selection, pending/active state, validation ordering, and the 8192-byte
  JSON handoff bound are covered by focused tests.

## HTTP And Admin UI

- `tests.test_conversation_continuity_http` constructs fresh Server, Runtime,
  HTTP-session, TranscriptStore, and binding-store objects across restart. It
  covers two-window isolation, exact retained-header recovery, no automatic
  recovery for a new transport, explicit resume, changed-repository rejection,
  original-repository recovery, and non-oracle unauthorized behavior.
- Admin list progress is calculated from durable evidence before rendering.
  Message and context pagination use independent page state. Approval requests
  carry the exact owning Agent Session ID and are validated against the selected
  Conversation.
- The Admin UI shows transcript-only, zero-message, historical non-Admin Agent,
  and multi-execution Conversations. Create, send, resume, close, approval, and
  validation actions refresh both list and detail state.
- Mobile Conversation summaries now use one column at widths at or below 650px,
  preventing compressed multi-column summaries on a 390px screen.

## Browser Acceptance

The temporary QA service ran at `http://127.0.0.1:65346/admin` and was stopped
after verification.

- Desktop `1440x1000`: one-time Admin-token exchange, reload/session retention,
  all four Conversation shapes, real list progress, structured Work / Progress,
  independent message/context pages, exact non-latest approval routing, execution
  lifecycle, failed validation followed by retry/pass, close, and durable reload
  all passed.
- Mobile `390x844`: Conversation Center was visible with one-column summaries;
  `document.documentElement.scrollWidth <= window.innerWidth` was true.
- Screenshots were captured under `.tmp/conversation-center-qa-desktop.png` and
  `.tmp/conversation-center-qa-mobile.png`.
- Browser console warnings/errors: 0. Server request records had no unexpected
  4xx/5xx. The initial unauthenticated session check (401) and browser favicon
  request (404) were expected. No `/app` or `/api/app/*` request was observed.

## Privacy Acceptance

The scanner generated and used fresh random values in memory for an Admin token,
MCP bearer credential, Admin cookie, CSRF value, and three MCP Session IDs. It
scanned the current QA state directory, three SQLite databases, available WAL/SHM
sidecars, both screenshot files, and this report.

| Result | Value |
| --- | --- |
| Files scanned | 9 |
| SQLite files scanned | 3 |
| Screenshot files scanned | 2 |
| Raw-value matches | 0 |
| WAL/SHM after shutdown | absent |

No raw secret, cookie, CSRF value, or MCP Session ID is included in this report.
The QA server did not write a durable request log; its reviewed request output
contained method/path/status only.

## Toolchain Evidence

| Command | Result |
| --- | --- |
| Python/Ruff/Mypy/Node/npm versions | `3.12.0` / `0.15.4` / `2.1.0` / `v24.12.0` / `11.6.2` |
| Focused Conversation suite | 42 passed, 0 skipped |
| Post-fix focused Conversation/Agent Session suite | 49 passed, 0 skipped |
| `unittest discover` | 684 passed, 86 skipped |
| Compliance suite | 89 passed, 72 skipped |
| Ruff | passed |
| Desktop i18n | 138 translated messages, passed |
| Mypy | 70 source files, passed |
| Compileall and `git diff --check` | passed; only LF/CRLF warnings from Git |
| WebUI Node tests | 36 passed |
| WebUI build | passed; source/dist rebuilt from `webui/src` |
| Post-build WebUI/Admin tests | 24 passed |

All compliance skips are Windows fixture-preflight skips caused by `/dev/null`
not being a read/write Windows device path. The focused Conversation HTTP suite
was not skipped. The full Python suite recorded the expected platform-specific
skips; none replace Conversation acceptance.

## Search Classification

The obsolete-route search found only regression tests and historical/superseded
documentation; no live `/app`, `/api/app/*`, `app.html`, or `OperatorSessionStore`
implementation was found. The sensitive-identifier search correctly found source
code that handles authentication/CSRF and generated Admin source/dist. Those are
identifiers and security controls, not raw values from this QA run.

Generic identifier classification now covers version-controlled source and
durable reports. The separate actual-value scanner covers task-owned QA state,
SQLite/WAL/SHM, screenshots, Admin cookie, CSRF value, credentials, and MCP
Session IDs. This avoids conflating source identifiers with raw secret values.

Existing `.tmp/broker-review` and `.tmp/broker-v6-t12-verify` trees were created
on 2026-08-02, are ignored by Git, belong to unrelated Broker sandbox work, and
contain ACL-denied descendants. They are not Conversation Center verification
inputs. Their ownership and ACLs were deliberately left unchanged.

## Independent Final Reverification

- Focused Conversation suite: 42 passed.
- Post-fix focused Conversation/Agent Session suite: 49 passed.
- Full Python suite: 684 passed, 86 skipped.
- Compliance suite: 89 passed, 72 documented Windows `/dev/null` skips.
- Ruff 0.15.4, Mypy 2.1.0, desktop i18n, and compileall: passed.
- WebUI Node: 36 passed; production build and post-build Admin/WebUI tests:
  24 passed.
- Obsolete-route and sensitive-identifier source/report searches: completed and
  classified without traversing unrelated ignored sandbox state.
- `git diff --check`: passed with line-ending warnings only.
- QA/listener ports checked: no listeners remained.

## Post-verification Startup Correction

A production startup with more than `MAX_LIST_LIMIT` historical Agent Sessions
reached the second page of `AgentSessionStore.iter_all()` and failed because the
SQL statement used four placeholders while its parameter tuple supplied three.
The repeated `updated_at` comparison now receives the cursor timestamp twice.

The regression test writes `MAX_LIST_LIMIT + 1` sessions with an identical
timestamp and calls `iter_all()` with its default batch size. This covers both
the real startup boundary and the tie-break on `session_id`. The first full-suite
rerun encountered one transient Windows `WinError 10053` in an unrelated Admin
HTTP test; that test passed in isolation and the complete 684-test rerun passed.

## Cleanup And Working Tree

- QA browser viewport was reset and its tab closed.
- The QA server exited cleanly; a follow-up loopback connection failed and no
  QA SQLite WAL/SHM files remained.
- Unrelated OAuth and pre-existing user changes were not reverted.
- No required QA Server, browser automation tab, test process, or build process
  remains running.

Final `git status --short --branch`:

```text
## main...origin/main [ahead 143, behind 25]
 M .learnings/ERRORS.md
 M apps/desktop-client/mcp_desktop_client/locales/app_zh_CN.ts
 M coding_tools_mcp/admin.py
 M coding_tools_mcp/agent_session_store.py
 M coding_tools_mcp/agent_sessions.py
 M coding_tools_mcp/conversation_continuity.py
 M coding_tools_mcp/conversation_evidence.py
 M coding_tools_mcp/operator_api.py
 M coding_tools_mcp/runner/capabilities.py
 M coding_tools_mcp/runner/websocket.py
 M coding_tools_mcp/server.py
 M coding_tools_mcp/transcript.py
 M coding_tools_mcp/transport_http.py
 M coding_tools_mcp/webui_dist/admin.html
 M coding_tools_mcp/workspace_host.py
 M docs/admin-api.md
 M docs/admin-webui.md
 M docs/adr/0003-operator-event-streaming-sse.md
 M docs/coding-tools-mcp-agent-runner-guide.md
 M docs/coding-tools-mcp-agent-runner-guide.zh-CN.md
 M docs/conversation-center-session-continuity-integration-taskbook.md
 M docs/webcodex-feature-integration-agent-execution-plan.md
 M reports/compliance/latest.json
 M reports/compliance/latest.md
 M tests/compliance/fixtures.py
 M tests/test_admin_conversation_routes.py
 M tests/test_conversation_binding_store.py
 M tests/test_conversation_evidence.py
 M tests/test_conversation_service.py
 M tests/test_runner_mcp_routing.py
 M tests/test_workspace_session_binding.py
 M webui/src/admin.css
 M webui/src/admin.html
 M webui/src/admin.js
 M webui/src/i18n.js
 M webui/tests/dom-interactions.test.mjs
?? coding_tools_mcp/conversation_recorder.py
?? docs/conversation-center-session-continuity-completion-taskbook.md
?? docs/conversation-center-session-continuity-final-remediation-taskbook.md
?? reports/conversation-center-final-verification.md
?? tests/test_conversation_continuity_http.py
```
