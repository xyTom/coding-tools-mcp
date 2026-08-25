# Conversation Center / Session Continuity Final Remediation Taskbook

> Prepared: 2026-08-24
>
> Repository: `coding-tools-mcp`
>
> Audience: the implementation Agent responsible for finishing the work end to
> end in the current working tree.
>
> Status at preparation time: **not complete**. The main happy paths exist, but
> task-scoped correctness, browser acceptance, privacy coverage, and the declared
> static toolchain are not all complete.

## 1. Mission

Finish the Conversation Center and MCP Session Continuity implementation without
claiming completion from mock-only tests, partial projections, stale UI state,
incorrect tool versions, skipped acceptance paths, or undocumented exceptions.

This task is complete only when:

1. every defect in section 5 has a production fix;
2. every required regression test has been observed failing before the fix or is
   otherwise demonstrated to exercise the previously broken production path;
3. the exact commands in section 15 pass using the repository-declared tool
   versions;
4. the real HTTP and browser scenarios in sections 11 and 12 pass without using
   the same live Store/Runtime objects to simulate a restart;
5. the final report contains actual command output summaries, skip reasons,
   browser scenarios, and remaining risks;
6. no required item is waived as "pre-existing". If a repository-wide required
   gate is red, fix it or leave the Definition of Done unchecked.

Do not stop after proposing changes. Continue through implementation, tests,
WebUI build, browser QA, privacy scanning, static gates, documentation, and final
workspace inspection.

## 2. Source Documents

Read these before editing:

- `docs/conversation-center-session-continuity-integration-taskbook.md`
- `docs/conversation-center-session-continuity-completion-taskbook.md`
- `docs/admin-api.md`
- `docs/admin-webui.md`
- `docs/ci-and-tests.md`
- `docs/adr/0003-operator-event-streaming-sse.md`

The previous completion taskbook contains useful implementation history, but its
checked boxes are not proof. This taskbook and current production behavior take
precedence where the earlier audit overclaims completion.

## 3. Working-Tree Rules

The working tree is intentionally dirty and contains the implementation under
review. Treat all existing changes as user work.

Before doing anything:

```powershell
git rev-parse --show-toplevel
git status --short --branch
git log --oneline --decorate -20
git diff --stat HEAD
```

Rules:

- Do not reset, checkout, revert, rebase, pull, or overwrite existing changes.
- Do not remove unrelated OAuth, Runner, desktop, WebUI, or user changes.
- Do not change branches or create a worktree unless explicitly authorized.
- Keep temporary scripts and browser artifacts under `<repo>\.tmp\`.
- Use `apply_patch` for manual edits.
- Rebuild `coding_tools_mcp/webui_dist/admin.html` from source; do not hand-edit
  the packaged HTML.
- Do not commit, push, or alter remotes unless explicitly requested.

## 4. Non-Negotiable Architecture and Security

- Keep `TranscriptStore`, `AgentSessionStore`, Conversation binding persistence,
  live MCP transport sessions, and backend execution state separate.
- Do not merge Transcript and Agent Session tables.
- `Mcp-Session-Id`, `conversation_id`, and Agent Session ID remain different
  identities.
- Raw `Mcp-Session-Id`, bearer/OAuth/Admin tokens, cookies, passwords, CSRF
  values, and opaque backend thread IDs must not be durably stored or logged.
- Window identity must remain domain-separated SHA-256.
- Automatic continuity requires exact principal + window + workspace + repo
  binding. Never guess from a credential, principal, recent session, title, or
  last-used Conversation.
- Identity loss uses explicit owner-scoped `conversation_list` and
  `conversation_resume` only.
- Knowledge of a Conversation ID or Agent Session ID is never authorization.
- Admin privileged visibility must not rewrite historical ownership to `admin`.
- Evidence must remain compact and allowlisted. Never store file bodies, complete
  command output, arbitrary tool payloads, credentials, or hidden model context.
- Continuation/handoff projection must be deterministic, side-effect free, and
  perform no repository I/O, command execution, Git calls, Runner calls, or LLM
  calls.
- `/admin` remains the only management UI. Do not restore `/app`, `/api/app/*`,
  `OperatorSessionStore`, or a second browser authentication system.

## 5. Confirmed Defects That Must Be Fixed

These are current production defects, not optional improvements.

### 5.1 Ownership domains, backfill, and repo binding

Current problems:

- Historical Agent ownership is hashed with `conversation_owner_digest()` while
  MCP authorization compares `principal_scope_digest()`. The namespaces and
  inputs are not equivalent.
- Startup backfill reads only the latest 1000 Agent Sessions. A second owner
  outside that slice can be missed, causing ambiguous ownership to be guessed.
- `conversation_ownership` does not bind ownership to a repository scope.
- MCP list/resume checks workspace + owner but not current repo scope.

Required design:

1. Upgrade the binding database to schema v3 or later.
2. Represent the ownership namespace explicitly. The minimum acceptable shape is
   equivalent to:

   ```text
   workspace_id
   conversation_id
   owner_kind            # mcp_principal | agent_principal | admin | ambiguous | unowned
   owner_scope_digest
   repo_scope_digest     # required for MCP-claimable rows
   mcp_claimable         # boolean, fail closed by default
   source
   created_at
   updated_at
   ```

3. New MCP Conversations use the same canonical principal digest for both write
   and comparison, include exact repo scope, and are MCP-claimable.
4. Legacy Agent owners remain `agent_principal`. Do not claim they are equivalent
   to an MCP authorization scope unless an exact, documented mapping is provable.
5. Ambiguous, unowned, or repo-ambiguous rows remain Admin-visible but
   MCP-unclaimable.
6. Migrate v2 rows fail closed:
   - an MCP row may become claimable only when its principal and repo can be
     derived unambiguously from durable binding rows;
   - multiple principal or repo candidates become ambiguous/unclaimable;
   - legacy Agent ownership remains non-MCP unless exact equivalence is proven.
7. Replace the fixed `list_all(limit=1000)` migration with a complete paginated
   or cursor-based scan. Group all sessions before deciding uniqueness.
8. Migration must be idempotent and fail closed on newer schema versions.
9. MCP `conversation_list`, `conversation_resume`, and retained-header recovery
   must all require exact owner namespace/digest + workspace + repo.
10. Admin workspace authorization remains mandatory for every privileged action.

Required tests:

- [x] schema v2 -> v3 migration preserves valid bindings and ownership;
- [x] a database newer than supported fails closed;
- [x] more than 1000 historical sessions are scanned;
- [x] owner B outside the first page makes the Conversation ambiguous;
- [x] an `agent_principal` row cannot be resumed as an MCP principal merely
      because text IDs appear related;
- [x] exact new MCP owner + repo can list and resume;
- [x] same owner/workspace with changed repo cannot list or resume the old
      Conversation;
- [x] returning to the original repo restores exact access;
- [x] ambiguous and unowned Conversations remain Admin-visible;
- [x] principal A cannot list/resume principal B in the same workspace/repo;
- [x] migration and reopen do not persist raw principal or credential material.

### 5.2 Owner-scoped list pagination

Current code paginates workspace Conversations first and filters ownership
afterward. A caller's older valid Conversations disappear when newer rows belong
to other principals.

Required behavior:

- Apply ownership and repo authorization before pagination.
- Return an accurate owner-scoped `total`.
- Return accurate `truncated`/page metadata.
- Never scan one arbitrary page and report `truncated: false` after filtering.
- Keep Transcript and ownership stores separate. A suitable implementation is to
  obtain the exact authorized Conversation IDs from the binding store and pass a
  bounded ID filter into `TranscriptStore.list_conversations()`.

Required tests:

- [x] 40 newer foreign-owner Conversations do not hide owner A's older rows;
- [x] `limit=1` returns A's first authorized row rather than an empty result;
- [x] `total` counts only authorized owner+repo rows;
- [x] transcript-only/unowned rows do not leak through MCP list;
- [x] unauthorized resume still uses non-oracle unavailable/not-found semantics.

### 5.3 Real tool evidence producers

Current problems:

- `apply_patch` looks for `path/file_path/target` in input even though actual
  changed paths are returned under `payload.affected_files`.
- `git_*` and `view_image` ToolSpecs do not opt into a Conversation mode, so their
  evidence adapters are unreachable.
- Existing Recorder unit tests do not prove real tool dispatch records evidence.

Required behavior:

- Successful non-dry-run `apply_patch` records every unique affected path from
  the trusted result payload, including add/update/delete/move old and new paths
  where appropriate.
- Dry-run and failed patches record no `changed_path`.
- `read_file` and successful `view_image` record explored paths.
- list/search tools record bounded explored roots where meaningful.
- successful Git inspection records either a path or a compact checkpoint.
- exec/session tools upsert job state by stable Session ID.
- classified failures record only compact allowlisted error codes.
- Evidence recording failure must not turn a successful tool operation into a
  tool failure.

Required tests must invoke the real Runtime tool-dispatch path:

- [x] patching two files records both changed paths once;
- [x] moving a file records the bounded source and destination;
- [x] patch dry-run records no mutation;
- [x] failed patch records no changed path and a compact failure if classified;
- [x] `view_image` records an explored path after successful image loading;
- [x] every supported `git_*` tool records a compact checkpoint/path;
- [x] evidence never contains file content, patch body, complete command output,
      token, or raw MCP Session ID.

### 5.4 Attempt lookup and evidence retention

Current problems:

- `current_attempt()` searches only the latest 200 rows. A single large attempt
  can cause later evidence to become `no-instruction`.
- Projection considers `running`, `queued`, and `starting` jobs active, and
  `requested`/`required` approvals pending, while pruning protects a smaller,
  inconsistent status set.
- More than 500 protected rows can leave more than the declared hard cap.
- Upsert states retain old `created_at`, while pruning sorts by `created_at`
  rather than their current transition time.

Required behavior:

1. Add a direct indexed Store query for the latest `task_instruction` or latest
   attempt ID. Do not infer it from an arbitrary recent-row limit.
2. Use one shared definition of active job and pending approval statuses across
   recorder, pruning, projection, UI, and tests.
3. State retention ordering must use the latest transition (`updated_at`) for
   upserted states.
4. Per-kind limits remain bounded and deterministic.
5. The absolute invariant is: after recording/pruning, a Conversation has at
   most 500 evidence rows.
6. Under normal admitted workloads, all live jobs and pending approvals within
   configured capacity survive pruning.
7. For corrupt/legacy overflow beyond capacity, retain the newest states
   deterministically, expose bounded overflow/truncation totals, and still obey
   the hard cap. Never silently leave 501+ rows.
8. If preserving all live states requires a Conversation-level concurrency
   limit, enforce that limit at admission and test it. Do not rely on an
   undocumented assumption.

Required tests:

- [x] an attempt remains current after more than 200 and more than 500 generated
      evidence events;
- [x] paths deduplicate only within their attempt;
- [x] `running/queued/starting/active/recovering` are handled consistently;
- [x] `pending/requested/required` are handled consistently;
- [x] 501 protected states are reduced to at most 500 deterministically;
- [x] normal-cap active jobs and pending approvals survive pressure from all
      other evidence kinds;
- [x] state updates reorder by current transition time;
- [x] reopening the Store preserves the latest attempt and state projection.

### 5.5 Validation and unresolved failure semantics

Current projection treats every failure in the current attempt as unresolved,
even after a later successful validation.

Required behavior:

- Failures before the latest successful validation are historical, not
  unresolved.
- A failure or failed validation after the latest pass becomes unresolved again.
- Preserve bounded historical failure information separately if useful, but do
  not keep suggesting `resolve_failure` after the work has passed validation.
- Conversation list progress and detail projection must use the same rule.

Required tests:

- [x] failure -> validation passed yields zero unresolved failures;
- [x] passed -> later failure yields one unresolved failure;
- [x] failed validation remains visible;
- [x] latest-attempt selection excludes failures from older attempts;
- [x] continuation and handoff remain deterministic and <=8192 bytes.

### 5.6 Conversation list progress and stale UI state

Current backend list items do not contain `progress`; the WebUI renders missing
values as `0/unknown`. After create/send/resume/close, detail refreshes but the
left-hand list remains stale.

Required behavior:

- Every list item includes real bounded current-attempt progress derived from the
  same projection logic used by detail:

  ```text
  changed_path_count
  explored_path_count
  validation_status
  unresolved_failure_count
  active_job_count
  pending_approval_count
  truncated/overflow indicators where applicable
  ```

- Avoid divergent handcrafted status rules.
- Bound database work per page; avoid unbounded context loading.
- After execution create, send, resume, close, approval, and validation, refresh
  or accurately update both detail and the selected list summary.
- Preserve the user's selected Conversation and current page when refreshing.

Required tests:

- [x] real API list responses contain nonzero progress from recorded evidence;
- [x] list and detail agree on validation/failure/job/approval counts;
- [x] create/send/close changes the list execution/message summary immediately;
- [x] validation updates list status immediately;
- [x] API/projection failure is visible and retryable, never silently rendered
      as a successful all-zero state.

### 5.7 Detail pagination

Current WebUI constructs pagination query parameters but does not append them to
the request. The Admin route/service also hard-code the default page.

Required behavior:

- Admin route accepts message/context page and page-size query parameters.
- Service passes them into `TranscriptStore.conversation_detail()`.
- WebUI appends the encoded query string.
- Previous/next controls update the requested page and visible content.
- Response exposes accurate page, page size, total, and navigation state.
- Page changes must not reset unrelated Conversation selection or list filters.

Required tests:

- [x] more than 100 messages can be navigated across pages;
- [x] more than 100 context rows can be navigated independently;
- [x] page 2 contains different IDs from page 1;
- [x] the DOM test asserts the actual requested URL includes pagination query;
- [x] invalid page/page-size inputs are bounded or rejected consistently.

### 5.8 Multi-execution approval routing

Current approval projection omits the owning Agent Session and the WebUI sends
every decision to the latest execution's `session_id`.

Required behavior:

- Approval evidence metadata includes bounded `session_id`.
- Approval stable identity is collision-safe across Sessions, for example
  Session ID + approval ID.
- Projection returns `session_id` with every pending approval item.
- WebUI submits the decision to that exact Session, never the latest execution by
  assumption.
- Backend verifies the Session belongs to the requested Conversation/workspace
  and that the approval is pending for that Session.
- Missing or mismatched Session identity fails closed with a visible error.

Required tests:

- [x] two executions with different pending approvals route correctly;
- [x] identical approval IDs in different Sessions do not collapse together;
- [x] approval for an older execution is not sent to the latest execution;
- [x] cross-Conversation and cross-workspace Session IDs are rejected;
- [x] approve/deny updates evidence, detail, and list progress.

## 6. Implementation Batches

Complete these batches in order. Do not stop between batches unless genuinely
blocked by user input or external credentials.

### Batch 0: Baseline and correct toolchain

- [x] Capture initial `git status`, diff stat, Python/Node/npm versions.
- [x] Inspect existing user changes before touching overlapping files.
- [x] Create or reuse a repository-local `.venv`.
- [x] Install the declared development dependencies from `.[dev]`.
- [x] Verify Ruff is `>=0.15,<0.16` and Mypy is `>=2.1,<2.2`.
- [x] Run the focused tests once before fixes and record the real baseline.
- [x] Do not use global Ruff 0.9.9 as completion evidence.

Suggested setup, subject to the existing environment:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m ruff --version
& .\.venv\Scripts\python.exe -m mypy --version
```

Do not claim installation succeeded unless the commands actually completed.

### Batch 1: Ownership and exact authorization

- [x] Implement schema v3+ migration.
- [x] Implement canonical ownership namespaces and MCP claimability.
- [x] Add exact repo scope to MCP ownership checks.
- [x] Replace fixed-1000 backfill with complete iteration.
- [x] Fix owner-scoped list-before-pagination behavior.
- [x] Add migration, >1000-row, owner, repo, and non-oracle tests.
- [x] Reopen Stores in tests to prove durability.

### Batch 2: Producers and current attempt

- [x] Fix `apply_patch` result-based changed paths.
- [x] Classify Git and image tools with correct Conversation modes.
- [x] Add real dispatch-path producer tests.
- [x] Add direct latest-attempt Store query.
- [x] Ensure restart/reopen preserves the current attempt.

### Batch 3: Retention and projection

- [x] Centralize active/pending status definitions.
- [x] Make pruning use current state transition time.
- [x] Enforce the absolute 500-row cap.
- [x] Add deterministic overflow/truncation reporting.
- [x] Correct unresolved failure semantics.
- [x] Re-run handoff byte-bound and determinism tests.

### Batch 4: Admin API and WebUI

- [x] Add real list progress.
- [x] Implement backend and frontend detail pagination.
- [x] Fix stale list summaries after actions.
- [x] Carry approval Session identity end to end.
- [x] Add two-execution approval tests.
- [x] Make projection and API errors visible/retryable.
- [x] Rebuild packaged WebUI from source.

### Batch 5: Real restart and HTTP continuity

- [x] Strengthen the HTTP test so Server, Runtime, binding Store, and Transcript
      Store objects are all closed and reconstructed from the same durable paths.
- [x] Reconstruct every Server/Runtime/Store object across restart; use a
      subprocess-level acceptance test where practical.
- [x] Directly assert a new transport has no current binding before explicit
      resume.
- [x] Test changed repo rejection and return-to-original recovery.
- [x] Verify no duplicate execution/job launch during retained-header recovery.
- [x] Verify unknown retained headers remain subject to admission quotas.

### Batch 6: Browser and privacy acceptance

- [x] Run a real Admin server and browser, not a DOM-only fixture.
- [x] Complete every scenario in section 12.
- [x] Capture actual generated secrets/session values only in memory.
- [x] Scan databases, WAL/SHM, logs, browser artifacts, and reports for every
      actual token/cookie/session/CSRF value.
- [x] Close all services and verify no listeners or QA processes remain.

### Batch 7: Repository gates and documentation

- [x] Fix full-repository Ruff findings using the declared Ruff version.
- [x] Fix desktop i18n source/catalog drift.
- [x] Run full Mypy and fix all reported errors in required targets.
- [x] Run full unit and compliance suites.
- [x] Rebuild WebUI and verify source/dist consistency.
- [x] Update docs to describe actual ownership namespaces, claimability, repo
      binding, retention overflow behavior, pagination, and approval routing.
- [x] Append a truthful final audit to the previous completion taskbook.

## 7. Files Expected to Change

The exact set may vary, but inspect these first:

```text
coding_tools_mcp/conversation_continuity.py
coding_tools_mcp/conversation_recorder.py
coding_tools_mcp/conversation_evidence.py
coding_tools_mcp/transcript.py
coding_tools_mcp/server.py
coding_tools_mcp/agent_session_store.py
coding_tools_mcp/agent_sessions.py
coding_tools_mcp/operator_api.py
coding_tools_mcp/admin.py
coding_tools_mcp/transport_http.py
webui/src/admin.js
webui/src/admin.html
webui/src/admin.css
webui/tests/dom-interactions.test.mjs
tests/test_conversation_binding_store.py
tests/test_conversation_evidence.py
tests/test_conversation_service.py
tests/test_admin_conversation_routes.py
tests/test_operator_api.py
tests/test_conversation_continuity_http.py
docs/admin-api.md
docs/admin-webui.md
docs/conversation-center-session-continuity-completion-taskbook.md
```

Full gate cleanup may also legitimately touch the currently failing Ruff/i18n
files. Keep those fixes mechanical and scoped.

## 8. Required Focused Test Matrix

Do not replace these with direct calls to private helpers when the defect is in
the production route/dispatch path.

### Ownership

- migration v2 -> current;
- all-session backfill beyond 1000 rows;
- ambiguous owner beyond the first page;
- owner namespace mismatch;
- exact repo match, changed repo rejection, original repo recovery;
- list filtering before pagination;
- unauthorized resume non-oracle behavior.

### Evidence

- real `apply_patch`, Git, image, read/list/search, exec/session producers;
- dry-run and failure behavior;
- current attempt beyond 200/500 rows;
- active/pending status consistency;
- strict hard cap with 501 protected rows;
- state transition ordering;
- failure before/after latest passing validation;
- 8192-byte handoff bound.

### Admin/WebUI

- transcript-only, zero-message, one-execution, and multi-execution Conversations;
- real list progress;
- independent message/context pagination;
- stale list refresh after every supported action;
- approval routing to an older execution;
- validation success/failure and retryable errors;
- source/dist synchronization.

### HTTP

- same credential, workspace, repo, two windows -> different Conversations;
- same exact retained header after complete Store reconstruction -> same
  Conversation;
- new transport -> no automatic reuse;
- explicit authorized resume -> exact rebind;
- changed repo -> no list/resume/recovery;
- original repo restored -> original exact binding;
- unauthorized ID -> unavailable/not-found;
- raw identities absent from durable artifacts.

## 9. Continuation and Handoff Contract

The resulting projection must include bounded, current-attempt data for:

- previous instruction and attempt ID;
- changed and explored paths with totals/truncation;
- latest validation status;
- unresolved failures under the corrected temporal rule;
- active/recovering jobs with stable IDs and Session identity where needed;
- pending approvals with exact Session identity;
- checkpoints;
- deterministic suggested actions;
- execution summary.

`handoff_brief()` must always serialize to valid JSON at or below 8192 UTF-8
bytes. Add a defensive final fallback and test adversarial multibyte input.

## 10. Admin API Contract

Document and test the final shapes for:

```text
GET  /admin/api/conversations
GET  /admin/api/conversations/{workspace_id}/{conversation_id}
GET  /admin/api/conversations/{workspace_id}/{conversation_id}/continuation
GET  /admin/api/conversations/{workspace_id}/{conversation_id}/handoff
POST /admin/api/conversations/{workspace_id}/{conversation_id}/executions
POST /admin/api/conversations/{workspace_id}/{conversation_id}/turns
POST /admin/api/conversations/{workspace_id}/{conversation_id}/resume
POST /admin/api/conversations/{workspace_id}/{conversation_id}/close
POST /admin/api/conversations/{workspace_id}/{conversation_id}/approvals/{approval_id}
POST /admin/api/conversations/{workspace_id}/{conversation_id}/validation
```

All actions require Admin workspace authorization. Action requests with a
Session ID must verify that the Session belongs to the same workspace and
Conversation.

## 11. Real HTTP Acceptance Procedure

Use independently initialized clients A, B, and C with the same credential,
workspace, and repository.

1. Start Server process 1 with durable config/database paths.
2. Initialize A and B; retain headers A and B only in memory.
3. Perform successful evidence-producing work in each window.
4. Assert Conversation A != Conversation B.
5. Assert later A/B calls remain attached to their exact Conversation.
6. Shut down Server process 1 cleanly.
7. Ensure Server, Runtime, HTTP Session manager, binding Store, Transcript Store,
   and database connections are closed.
8. Start Server process 2 using the same durable paths and newly constructed
   objects.
9. Send an authenticated non-initialize request with retained header A.
10. Assert exact recovery to Conversation A and no duplicate job launch.
11. Initialize C and assert C has no automatic binding to A or B.
12. Call owner-scoped list; assert authorized Conversations are visible even when
    newer foreign-owner rows exist.
13. Explicitly resume A from C and assert exact rebind.
14. Change the workspace root/repo scope; assert A is unavailable.
15. Restore the original root; assert A resolves again.
16. Attempt unauthorized resume and assert non-oracle unavailable semantics.
17. Stop Server process 2 and verify all ports/processes are clean.

This acceptance is invalid if process 2 reuses the same live Store or Runtime
objects from process 1.

## 12. Real Browser Acceptance Procedure

Run at desktop `1440x1000` and mobile `390x844`.

Required scenarios:

1. Login with a newly generated Admin token.
2. Reload and verify the Admin session remains valid.
3. Show at the same time:
   - a transcript-only Conversation;
   - a zero-message Conversation shell;
   - an Agent-backed Conversation owned historically by a non-Admin principal;
   - a Conversation with multiple executions.
4. Verify list progress contains real nonzero changed/explored/job/approval data.
5. Open detail and verify structured Work / Progress, not raw JSON as the primary
   experience.
6. Navigate message and context pages and verify content changes.
7. Create an execution.
8. Send a turn and verify both detail and list summary update.
9. Resume and close; verify both detail and list summary update.
10. Run validation through the visible control; verify a real validation request,
    result, evidence, list progress update, and retryable failure presentation.
11. Resolve an approval belonging to a non-latest execution and verify the exact
    owning Session receives it.
12. Reload and confirm durable Conversation/progress state remains visible.
13. Verify projection/API failures are visible and retryable.
14. Verify no horizontal overflow, clipped controls, or incoherent overlap on
    mobile.
15. Verify no `/app` or `/api/app/*` network requests.
16. Record console errors, page errors, failed requests, and unexpected 4xx/5xx.

If the approval backend cannot naturally emit an approval, use a deterministic
test backend in the QA harness. Do not add a production debug bypass.

## 13. Privacy Acceptance

Generate fresh random values for the QA run:

- Admin token;
- MCP bearer/OAuth credential used by the harness;
- Admin session cookie;
- actual CSRF value used by the browser/API;
- MCP Session IDs A/B/C;
- any opaque backend thread/session identity that must remain private.

After the full HTTP/browser run, scan:

- Conversation binding database;
- Transcript database;
- Agent Session database;
- all WAL and SHM sidecars before shutdown where applicable;
- logs and captured request summaries;
- browser QA JSON and screenshots metadata;
- generated verification reports.

Assertions:

- [x] none of the raw values appear in durable files or logs;
- [x] only expected domain-separated digests appear;
- [x] no raw `Mcp-Session-Id` header marker/value is persisted;
- [x] privacy scan reports the number of files/databases scanned;
- [x] sanitized evidence contains no secret values;
- [x] all WAL/SHM files are checkpointed/closed after service shutdown.

## 14. Static and Build Gates

The previous audit used Ruff 0.9.9 and had no Mypy. That is not acceptable final
evidence because `pyproject.toml` declares Ruff 0.15.x and Mypy 2.1.x.

The scope of this task includes fixing the repository-wide blockers necessary to
make the required gates green. "Pre-existing" may be noted in the report but is
not a completion waiver.

Known areas from the previous audit include:

- Ruff findings in `coding_tools_mcp/runner/capabilities.py`,
  `coding_tools_mcp/runner/websocket.py`, and
  `tests/test_runner_mcp_routing.py`;
- desktop i18n drift in desktop client source/catalog files;
- unknown full Mypy results because the correct Mypy was not installed.

Fix these conservatively. Do not perform unrelated refactors.

## 15. Mandatory Final Commands

Use the repository-local Python environment. Record exact versions, exit codes,
test counts, and skips.

### Tool versions

```powershell
& .\.venv\Scripts\python.exe --version
& .\.venv\Scripts\python.exe -m ruff --version
& .\.venv\Scripts\python.exe -m mypy --version
node --version
npm --version
```

### Focused Conversation tests

```powershell
& .\.venv\Scripts\python.exe -m unittest `
  tests.test_conversation_binding_store `
  tests.test_conversation_evidence `
  tests.test_conversation_service `
  tests.test_admin_conversation_routes `
  tests.test_operator_api `
  tests.test_conversation_continuity_http
```

Add any new focused modules to this command.

### Full Python and compliance

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
& .\.venv\Scripts\python.exe -m tests.compliance.runner --suite all --report
```

Platform skips must be itemized. No Conversation acceptance scenario may be
skipped.

### Ruff, i18n, Mypy, compile, whitespace

```powershell
& .\.venv\Scripts\python.exe -m ruff check --exclude benchmarks/dogfood --ignore=E501 coding_tools_mcp apps\desktop-client\mcp_desktop_client tests benchmarks
& .\.venv\Scripts\python.exe scripts\check_desktop_i18n.py
& .\.venv\Scripts\python.exe -m mypy --python-version 3.11 --disable-error-code union-attr --disable-error-code assignment --disable-error-code arg-type --disable-error-code no-untyped-def coding_tools_mcp benchmarks\mcp_http.py benchmarks\runtime_latency.py benchmarks\swebench\run_smoke.py benchmarks\swebench\generate_reference_predictions.py benchmarks\real_workloads.py
& .\.venv\Scripts\python.exe -m compileall -q coding_tools_mcp tests
git diff --check
```

Every command above must exit `0`.

### WebUI

```powershell
Push-Location webui
npm run test
npm run build
Pop-Location
& .\.venv\Scripts\python.exe -m unittest tests.test_webui tests.compliance.test_mcp_admin
```

Run the source/dist consistency test after the final build, not only before it.

### Repository searches

```powershell
rg -n 'OperatorSessionStore|/api/app|href="/app"|app\.html' coding_tools_mcp tests webui docs
rg -n 'Mcp-Session-Id|Bearer |csrf|admin_token|access_token' coding_tools_mcp reports
```

Classify every match. Do not call the search passed merely because matches are in
documentation or tests. Generic identifier searches cover version-controlled
source and durable reports. Do not take ownership of, change ACLs on, or treat
unrelated Git-ignored sandbox trees as Conversation Center verification inputs.
Raw-value privacy acceptance for task-owned `.tmp` artifacts still requires the
actual-value scanner from section 13.

## 16. Anti-False-Completion Rules

The implementing Agent must follow all of these:

1. A checked box means the production path and its required test were actually
   executed successfully.
2. A direct unit call to `ConversationEvidenceRecorder` does not prove a Runtime
   tool producer works.
3. A DOM fixture does not prove a real browser/API workflow works.
4. Reconstructing an HTTP listener while reusing live Store objects does not
   prove restart durability.
5. A hand-built API fixture containing `progress` does not prove the real list API
   generates progress.
6. A control visible in the DOM does not prove it sends the correct request or
   updates durable state.
7. Focused Ruff is not a substitute for the mandatory full Ruff command.
8. An unavailable Mypy is a failed gate, not a skip and not a pass.
9. Running a different Ruff/Mypy version from `pyproject.toml` is invalid evidence.
10. "Pre-existing issue" is not permission to mark a mandatory aggregate gate
    complete.
11. Expected platform skips must be counted and explained; new skips require
    explicit justification.
12. Do not edit the final DoD to `[x]` while any required process is running or
    any command result is unknown.
13. Do not claim privacy success without scanning the actual values generated in
    that same QA run, including CSRF.
14. Do not claim source/dist consistency until the final production build and
    post-build consistency test pass.
15. If any required item remains red, report the task as incomplete and leave its
    checkbox unchecked.

## 17. Final Definition of Done

Do not mark this section until all earlier batches and mandatory commands are
complete.

- [x] Ownership has explicit namespaces, exact repo scope, and fail-closed MCP
      claimability.
- [x] Historical backfill scans all Agent Sessions and never guesses ambiguity.
- [x] MCP list/resume/recovery require exact owner + workspace + repo.
- [x] Owner filtering occurs before pagination with accurate totals.
- [x] Two windows with the same credential/workspace/repo remain isolated.
- [x] Complete Store reconstruction recovers only the exact retained identity.
- [x] Identity loss never auto-selects a recent Conversation.
- [x] Real successful tools record explored paths, changed paths, jobs, and
      checkpoints through production dispatch.
- [x] Dry-run/failed tools do not create false mutation evidence.
- [x] Latest attempt remains correct beyond 200 and 500 historical events.
- [x] Evidence rows never exceed the hard cap of 500.
- [x] Active/pending state handling is consistent and overflow is explicit.
- [x] Latest successful validation clears earlier unresolved failures.
- [x] Handoff is deterministic valid JSON <=8192 UTF-8 bytes.
- [x] Conversation list contains real progress and stays fresh after actions.
- [x] Message and context pagination work end to end.
- [x] Multi-execution approvals route to the exact owning Session.
- [x] Validation works through the real browser control and updates durable UI
      state.
- [x] Admin can see transcript-only, zero-message, historical non-Admin-owned,
      and multi-execution Conversations.
- [x] Desktop and mobile Playwright scenarios pass without unexpected errors or
      overflow.
- [x] Actual token/cookie/session/CSRF privacy scan passes.
- [x] Correct-version full Ruff passes.
- [x] Desktop i18n check passes.
- [x] Correct-version full Mypy passes.
- [x] Full Python and compliance suites pass with skips explained.
- [x] WebUI Node tests, production build, and post-build consistency pass.
- [x] `git diff --check`, compile checks, searches, port checks, and process
      cleanup pass.
- [x] Documentation describes actual behavior without overclaiming.
- [x] Final report contains all required evidence and current `git status`.

Only after every box is checked may the Agent state that the Conversation Center
/ Session Continuity Definition of Done is complete.

## 18. Required Final Report

Create or update a durable sanitized report, recommended path:

```text
reports/conversation-center-final-verification.md
```

It must contain:

1. final architecture and schema version;
2. ownership namespaces, migration, backfill, claimability, and repo behavior;
3. exact restart recovery and identity-loss behavior;
4. evidence producers, attempt lookup, retention, and overflow policy;
5. continuation/handoff and unresolved failure semantics;
6. Admin progress, pagination, execution, approval, and validation workflows;
7. every validation command, tool version, exit code, test count, and skip count;
8. browser URL/port, viewports, exact scenarios, screenshots, console/page/network
   results;
9. privacy scan inputs by category only, files scanned, and pass/fail result;
10. static issues fixed, including previously pre-existing blockers;
11. final source/dist hash or consistency result;
12. remaining risks, if any;
13. final `git status --short --branch`;
14. confirmation that unrelated user/OAuth changes were not reverted;
15. confirmation that no required Server, browser, test, or build process remains
    running.

Never put raw secrets, cookies, CSRF values, or MCP Session IDs in the report.

## 19. Completion Decision

Use exactly one final status:

- **COMPLETE**: every item in section 17 is checked and every mandatory command
  passed with valid tool versions.
- **INCOMPLETE**: one or more required items remain red. List each blocker with
  the exact command, failing test, or missing acceptance scenario.

Do not use phrases such as "mostly complete", "checkpoint complete", or
"complete except for pre-existing gates" as a substitute for this decision.

## 20. Final Audit Record, 2026-08-24

**Status: COMPLETE.** Production implementation, focused/full tests, static
gates, final WebUI build/source-dist check, real HTTP restart acceptance, desktop
and mobile browser acceptance, and the targeted actual-value privacy scan passed.

Final independent review found and fixed one remaining API defect: owner-scoped
MCP list results now report `truncated=true` when `total` exceeds the returned
limit, with a regression test. The generic identifier search was corrected to
cover version-controlled source and durable reports; actual-value privacy scanning
continues to cover task-owned QA state and artifacts. Unrelated Git-ignored Broker
sandbox trees created on 2026-08-02 are outside this task's evidence set and were
not subjected to ACL or ownership changes. Sanitized evidence is recorded in
`reports/conversation-center-final-verification.md`.

A later real startup reproduction found one additional defect in the completed
historical backfill path: `AgentSessionStore.iter_all()` supplied three bindings
to a four-placeholder second-page query, so startup failed once the store held
more than `MAX_LIST_LIMIT` sessions. The binding is corrected and a default-batch
regression test now traverses `MAX_LIST_LIMIT + 1` same-timestamp rows. Post-fix
verification passed 49 focused tests, 684 full Python tests with 86 documented
platform skips, the 89-test compliance suite with 72 documented Windows skips,
full Ruff, Mypy, desktop i18n, compileall, and `git diff --check`.
