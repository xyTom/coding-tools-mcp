# Conversation Center / Session Continuity Completion Taskbook

> Date prepared: 2026-08-23
>
> Project: `coding-tools-mcp`
>
> Purpose: finish the incomplete Conversation Center and MCP session-continuity
> integration described by
> `docs/conversation-center-session-continuity-integration-taskbook.md`.
>
> Intended reader: a follow-up implementation Agent that should carry this work
> through code changes, migrations, tests, WebUI build, browser QA, and final
> documentation in one sustained task.

## 1. Mission

Complete the existing implementation so that the repository satisfies the
original taskbook's Definition of Done in real HTTP MCP and `/admin` workflows,
not only in isolated unit tests.

The work is complete only when all of these outcomes are simultaneously true:

1. `/admin` is the only WebUI management entry.
2. The Admin Conversation Center displays transcript-only Conversations,
   existing Agent-backed Conversations regardless of their historical owner,
   and Conversation shells with zero messages.
3. One Conversation can own zero, one, or multiple Agent Sessions.
4. MCP work is attached to a Conversation through an exact client-window,
   principal, workspace, and repository binding.
5. Two windows using the same credential and repository never share a
   Conversation unless the user explicitly resumes the same Conversation.
6. A retained, supported MCP window identity can recover its exact Conversation
   after Server restart; identity loss never triggers a recent-conversation or
   credential-wide guess.
7. Explicit `conversation_list` and `conversation_resume` are owner-scoped and
   fail closed across principals and workspaces.
8. Real tool and Agent execution activity records bounded structured evidence:
   explored paths, changed paths, validation, failures, jobs, approvals, and
   checkpoints.
9. Continuation and handoff projections use the latest current-attempt state,
   are deterministic and side-effect free, and remain bounded.
10. `/admin` renders a usable Work / Progress view and execution controls rather
    than raw JSON-only placeholders.
11. Generated WebUI artifacts match source.
12. Focused Python, MCP HTTP, WebUI Node, production build, static checks, and
    real browser QA all pass.

## 2. Baseline observed on 2026-08-23

The implementation commits observed at preparation time were:

```text
7dac062 fix: expose conversation continuity projections
b8f289e refactor: remove obsolete operator app surface
1e74fb5 feat: add bounded conversation evidence and handoff
72644b0 feat: add durable mcp conversation bindings
d2a24f9 feat: route conversations through admin and remove app entry
f499c6b feat: unify conversation projections in admin api
3b50abe checkpoint: conversation continuity baseline
```

Do not assume this baseline is unchanged. Begin by running:

```powershell
git rev-parse --show-toplevel
git status --short --branch
git log --oneline --decorate -20
git diff --stat
```

At preparation time, the working tree contained user changes in:

```text
webui/src/admin.css
webui/src/admin.html
webui/src/admin.js
webui/src/i18n.js
```

These changes are input. Do not revert, overwrite, or silently replace them.
Read the current diff and integrate the Conversation work with it. The packaged
`coding_tools_mcp/webui_dist/admin.html` was not synchronized with those source
changes at preparation time.

The repository was also ahead of and behind `origin/main`. Do not pull, rebase,
reset, or change branches as part of this task unless explicitly authorized.

## 3. Hard architectural boundaries

These rules are non-negotiable:

- Keep `TranscriptStore`, `AgentSessionStore`, binding persistence, live MCP
  transport sessions, and backend execution state as separate responsibilities.
- Do not merge Transcript and Agent Session tables into one large table.
- Do not equate `Mcp-Session-Id`, `conversation_id`, or `agent_session_id`.
- Never persist raw `Mcp-Session-Id`, bearer tokens, OAuth tokens, browser
  cookies, passwords, CSRF values, or GPT/Actions conversation headers.
- Never continue by selecting the most recent Conversation for a principal,
  credential, workspace, or repository.
- Never treat knowledge of `conversation_id`, binding digest, or Agent Session ID
  as authorization.
- Do not expose raw backend thread IDs in broad list projections.
- Do not persist file bodies, complete shell output, search results, arbitrary
  tool-result JSON, hidden model state, or chain of thought as evidence.
- Continuation and handoff builders must not execute commands, call Git, read
  repository files, invoke a Runner, call an LLM, or mutate durable state.
- Do not restore `/app`, `/api/app/*`, `OperatorSessionStore`, or a second browser
  authentication system.
- Preserve unrelated OAuth work in `coding_tools_mcp/oauth.py`,
  `coding_tools_mcp/oauth_store.py`, and `tests/test_oauth_integration.py`.

## 4. Confirmed defects to fix

### 4.1 MCP continuity is not attached to actual work

Current behavior:

- `Runtime.conversation_start()` returns a Conversation ID.
- The selected Conversation is not stored on the Runtime.
- Other MCP tools do not consume the binding.
- File, edit, execution, validation, job, and approval activity is therefore not
  associated with the Conversation.

Required correction:

- Add an explicit current-Conversation context to the Runtime.
- Resolve or create an exact binding automatically before a tool classified as
  Conversation-recording work executes.
- Make `conversation_start` an explicit bootstrap that also sets the Runtime's
  current Conversation and optionally records an instruction.
- Make `conversation_resume` rebind and update the Runtime context.
- Do not create Conversations for genuinely stateless calls such as metadata or
  tool-catalog inspection.

### 4.2 Restart recovery is currently unreachable through real HTTP MCP

Current behavior:

- A Runtime receives a random `http_session_id`.
- `initialize` rejects an incoming `Mcp-Session-Id`.
- After restart, an old header cannot lease a Runtime, while a new initialize
  produces a different identity.
- The SQLite binding survives, but the transport cannot re-present the identity
  to it.

Required correction:

- Preserve protocol-compliant initialize behavior.
- Add a narrowly scoped recovery path for an authenticated non-initialize request
  carrying a well-formed but currently unknown retained `Mcp-Session-Id`.
- Recovery may create a new live Runtime only when all of the following match:
  authenticated principal, transport kind, retained window digest, workspace,
  canonical repository scope, supported protocol version, and an existing
  durable exact binding.
- The recovered Runtime may keep the raw header only in process memory and use it
  as its live transport ID. It must never write the raw value to disk or logs.
- If no exact binding exists, return the normal unknown-session response. Do not
  create a Conversation and do not guess.
- If the client discards its old header and initializes a new transport, recovery
  must be explicit through `conversation_list` and `conversation_resume`.

If the existing MCP transport architecture cannot safely recover a Runtime this
way, stop and document the precise protocol constraint before substituting a
different identity source. Do not silently claim restart continuity from a
Store-only unit test.

### 4.3 Conversation ownership is missing

Current behavior:

- `chat_conversations` has no owner/principal field.
- MCP `conversation_list` lists all Conversations in a workspace.
- MCP `conversation_resume` checks only workspace and Conversation existence.
- Admin uses a synthetic `admin` principal, while Agent Session queries filter by
  `owner_principal_id`, hiding historical `/app`, OAuth, bearer, and noauth Agent
  Sessions.

Required model:

Introduce focused Conversation ownership metadata without merging the Transcript
and Agent Session stores.

Recommended logical record:

```text
ConversationOwnership
├─ workspace_id
├─ conversation_id
├─ owner_scope_digest
├─ repo_scope_digest
├─ source
├─ created_at
└─ updated_at
```

Implementation preference:

- Store this table beside `conversation_bindings` in the focused continuity
  database, or in another narrowly scoped store owned by the Conversation layer.
- `owner_scope_digest` must be a domain-separated hash of a stable, non-secret
  principal identity. It must not contain a token or credential value.
- Admin authorization is an explicit privileged workspace role, not the fake
  owner string `admin`.
- MCP list/detail/resume requires an ownership match in addition to workspace and
  repository authorization.
- Transcript-only imported Conversations without provable ownership remain
  visible to Admin but are not resumable by an arbitrary MCP principal.
- For existing Agent-backed Conversations, backfill ownership only when all
  attached Agent Sessions have one unambiguous owner. Hash that durable owner
  identity using the new ownership domain.
- Conflicting or ambiguous historical owners must remain Admin-visible and
  MCP-fail-closed; report them as migration diagnostics rather than choosing one.

Add explicit privileged Agent Session access methods for Admin:

- list sessions for an authorized workspace without owner filtering;
- inspect a session after workspace authorization;
- perform allowed resume/close/send/approval operations through a clearly named
  Admin path;
- never weaken the existing owner-filtered methods used by non-Admin callers.

### 4.4 Conversation IDs can collide across windows

Replace the current principal-hash plus millisecond ID generation with a
cryptographically random UUID/token, for example:

```text
conversation-<uuid4 hex>
```

The ID must not contain a principal digest and must be unique under concurrent
creation. Add a concurrency test that freezes time and starts two windows for the
same principal/workspace/repository.

### 4.5 Evidence types exist but producers are absent

Current production writers cover only `task_instruction` and `validation`.

Implement a dedicated `ConversationEvidenceRecorder` around
`TranscriptStore.record_context(...)`. Do not scatter raw `record_context` calls
through unrelated modules.

The recorder must support:

```text
task_instruction
explored_path
changed_path
validation
failure
job_state
approval_state
checkpoint
guidance
handoff_note
```

Every write must:

- normalize and bound text;
- use project-relative paths only;
- use a stable structured context ID where deduplication is intended;
- contain only an allowlisted metadata shape;
- avoid secrets and raw arguments/results;
- identify the current attempt when one exists;
- prune retained history according to a documented bounded policy.

### 4.6 Evidence currently selects old state and grows without limit

Implement current-attempt and retention semantics.

Recommended attempt model:

- A new accepted `task_instruction` creates a unique `attempt_id`.
- Evidence recorded after it carries that `attempt_id` in bounded metadata.
- Paths deduplicate within an attempt.
- Validation and failure facts remain ordered within an attempt.
- Job and approval state uses stable IDs and upsert semantics so state transitions
  replace the current projection instead of being counted as separate active
  items forever.

Recommended retention limits per Conversation:

```text
task_instruction: 20
explored_path:    100 across retained attempts
changed_path:     100 across retained attempts
validation:        40
failure:           40
job_state:        100, preserving current active/recovering states
approval_state:   100, preserving current pending states
checkpoint:        20
guidance:           20
handoff_note:       20
total hard cap:    500
```

Exact limits may be adjusted if an existing repository convention is smaller,
but the hard cap, deterministic pruning, and active-state preservation are
required.

Add TranscriptStore APIs that explicitly retrieve recent context in descending
order by kind/attempt. Do not build continuation from page 1 of the oldest 100
Context rows.

### 4.7 Continuation and handoff shapes are incorrect/incomplete

Correct these behaviors:

- Select the latest instruction and validation, not the oldest.
- Segment changed/explored paths by the current attempt.
- Deduplicate paths newest-first.
- Collapse jobs by `job_id` and count only active/recovering states.
- Collapse approvals by `approval_id` and count only pending states.
- Represent unresolved failures deterministically; do not treat every historical
  failure as unresolved forever.
- Include checkpoint data in the Work / Progress projection.
- Return the actual Conversation ID from the nested Transcript detail shape.
- Include truncation flags and real totals when lists are shortened.
- Keep the serialized handoff at or below 8192 bytes for all valid inputs.
- Add a final defensive serialization assertion and a bounded fallback so the
  handoff shrinking loop cannot fail to terminate.

Recommended continuation shape:

```json
{
  "status": "available",
  "attempt": {
    "attempt_id": "...",
    "previous_instruction": "..."
  },
  "execution": {
    "session_id": "...",
    "status": "running",
    "last_turn_id": "..."
  },
  "exploration": {
    "paths": [],
    "total": 0,
    "truncated": false
  },
  "changes": {
    "paths": [],
    "total": 0,
    "truncated": false
  },
  "validation": {
    "status": "passed",
    "latest": null,
    "failures": []
  },
  "jobs": {
    "active": 0,
    "recovering": 0,
    "items": []
  },
  "approvals": {
    "pending": 0,
    "items": []
  },
  "checkpoints": [],
  "suggested_actions": []
}
```

## 5. Target service design

Create or finish a first-class `ConversationService`. It may reuse portions of
`OperatorAPIService`, but the final public boundary should not require the Admin
UI to join Transcript and Agent Session data itself.

Suggested responsibilities:

```text
ConversationService
├─ list_conversations(actor, workspace_id, filters)
├─ create_conversation(actor, workspace_id, title, source)
├─ get_conversation(actor, workspace_id, conversation_id, pagination)
├─ list_executions(actor, workspace_id, conversation_id)
├─ create_execution(actor, workspace_id, conversation_id, request)
├─ send_turn(actor, workspace_id, conversation_id, request)
├─ resume_execution(actor, workspace_id, conversation_id, request)
├─ close_execution(actor, workspace_id, conversation_id, request)
├─ decide_approval(actor, workspace_id, conversation_id, request)
├─ record_evidence(...)
├─ continuation(actor, workspace_id, conversation_id)
└─ handoff(actor, workspace_id, conversation_id)
```

Use an explicit actor type:

```text
ConversationActor
├─ principal_scope_digest
├─ workspace_ids
├─ role: admin | principal
└─ principal_id or equivalent stable internal identity
```

Authorization rules:

- `admin`: may manage Conversations and Agent Sessions in an authorized Admin
  workspace; this is explicit privileged authority.
- `principal`: must match Conversation ownership and workspace/repo scope.
- Unauthorized or ambiguous Conversation IDs return not-found semantics.
- Authorization is checked again whenever a durable binding is used.

## 6. MCP integration design

### 6.1 Separate transport, window, and Conversation identity

Maintain this relation:

```text
raw Mcp-Session-Id (process memory only)
  -> ClientWindowIdentity(domain-separated SHA-256)
    -> exact durable binding digest
      -> Conversation ownership validation
        -> conversation_id
          -> current Runtime Conversation context
```

### 6.2 Tool classification

Extend `ToolSpec` with an explicit Conversation recording policy, for example:

```text
conversation_mode = none | observe | mutate | execution
```

Use a closed allowlist. Do not infer behavior from tool names at runtime.

Examples:

- `none`: `server_info`, tool catalog search/describe, `conversation_list`.
- `observe`: file reads, project text search, structured navigation.
- `mutate`: patch/edit/write operations.
- `execution`: shell/process/job/validation/approval operations.

Before `observe`, `mutate`, or `execution`:

1. Require an authenticated Runtime scope.
2. Resolve/create the exact Conversation binding.
3. Store the Conversation ID in Runtime request context.
4. Run the tool.
5. On success, invoke the tool's explicit evidence adapter.
6. On failure, record only a bounded failure identity where appropriate.

### 6.3 Evidence adapters

Implement explicit adapters for the tools actually exposed by this repository.

Rules:

- Successful read/navigation/search: record only validated relative paths.
- Directory/project enumeration: do not automatically record every returned path.
- Successful edit/apply/write: record each changed relative path and compact
  operation identity.
- Shell/process: do not store command text or stdout/stderr; record job/check ID,
  bounded status, and a compact failure code when needed.
- Validation: record recipe/check identity, structured scope, and pass/fail.
- Job observation/stop: update the stable `job_state` record.
- Approval request/decision: update the stable `approval_state` record.
- Agent backend events: map only allowlisted structured events; continue recording
  accepted user and completed assistant transcript messages separately.

### 6.4 Explicit MCP recovery tools

Keep or finish:

```text
conversation_start
conversation_list
conversation_resume
```

Recommended additions:

```text
conversation_checkpoint
conversation_continuation
conversation_handoff
```

Only add these if they fit the fixed local-tool exposure policy. They must use
bounded schemas and the same authorization service as Admin APIs.

`conversation_start` should accept optional bounded fields:

```json
{
  "title": "optional",
  "instruction": "optional current task instruction"
}
```

`conversation_resume` moves the current window binding to the authorized existing
Conversation. It must not duplicate or merge transcripts.

## 7. Admin API completion

Retain the repository's existing `/admin/api` convention.

Required routes and behavior:

```text
GET    /admin/api/conversations
POST   /admin/api/conversations
GET    /admin/api/conversations/{workspace_id}/{conversation_id}
DELETE /admin/api/conversations/{workspace_id}/{conversation_id}

GET    /admin/api/conversations/{workspace_id}/{conversation_id}/executions
POST   /admin/api/conversations/{workspace_id}/{conversation_id}/executions
POST   /admin/api/conversations/{workspace_id}/{conversation_id}/turns
POST   /admin/api/conversations/{workspace_id}/{conversation_id}/resume
POST   /admin/api/conversations/{workspace_id}/{conversation_id}/close
POST   /admin/api/conversations/{workspace_id}/{conversation_id}/approvals/{approval_id}
POST   /admin/api/conversations/{workspace_id}/{conversation_id}/validation

GET    /admin/api/conversations/{workspace_id}/{conversation_id}/continuation
GET    /admin/api/conversations/{workspace_id}/{conversation_id}/handoff
```

Exact paths may be adjusted to the current router style, but all operations must
be reachable from the unified service and covered by tests.

Fix the existing detail pagination bug:

- pass request query parameters through the Admin router;
- append the built query string in `loadConversationDetail()`;
- validate message/context page and page-size bounds centrally;
- do not use paginated display rows as the continuation evidence source.

Conversation summary must include:

```json
{
  "conversation_id": "...",
  "workspace_id": "...",
  "title": "...",
  "preview": "...",
  "source": "...",
  "message_count": 0,
  "context_count": 0,
  "created_at": 0,
  "updated_at": 0,
  "execution": null,
  "progress": {
    "changed_path_count": 0,
    "explored_path_count": 0,
    "validation_status": "unknown",
    "unresolved_failure_count": 0,
    "active_job_count": 0,
    "pending_approval_count": 0,
    "checkpoint_count": 0
  }
}
```

The summary's execution must be the deterministic latest/current execution, not
an arbitrary row from a limited query.

## 8. Admin WebUI completion

Integrate with the current uncommitted Admin redesign rather than replacing it.

### 8.1 Conversation list

Each row must show:

- title and preview;
- workspace/project;
- last activity;
- message count;
- current/latest execution status and backend;
- compact changed/explored counts;
- validation state;
- blocker/failure count;
- active job and pending approval counts.

Use compact badges/status text. Do not display raw JSON in list rows.

### 8.2 Conversation detail

Provide clear grouped views or tabs:

```text
Messages
Work / Progress
Executions
Handoff
```

Work / Progress must render structured sections for:

- latest instruction;
- changed paths;
- explored paths;
- validation and failures;
- active/recovering jobs;
- pending approvals;
- checkpoints;
- suggested next actions.

Handoff may include a formatted JSON copy/download view as a secondary affordance,
but raw JSON must not be the only user experience.

### 8.3 Execution controls

Implement feature-complete controls supported by the API:

- create/start execution for a Conversation;
- choose backend when more than one is supported;
- send a turn;
- resume a detached/recoverable execution;
- close an execution;
- render pending approvals and submit a decision;
- run a structured validation recipe when available;
- refresh messages, evidence, and execution state after operations.

Imported Conversations with no execution must render normally and offer an
allowed Start Execution action. A zero-message execution shell must also render.

### 8.4 Error handling

Do not silently hide projection failures.

- If continuation or handoff fails, render a bounded inline error with a retry
  action.
- Keep the rest of the Conversation detail usable.
- Record only path/status metadata in the Admin activity log, not response bodies
  or credentials.

### 8.5 Frontend verification

Extend DOM/model tests to cover:

- summary progress rendering;
- transcript-only and multiple-execution detail;
- structured Work / Progress rendering;
- continuation/handoff error state;
- execution create/send/resume/close controls;
- approval action;
- pagination query use;
- untrusted text remains text, never markup;
- Chinese and English coverage.

After source tests pass, run the production build and confirm source/dist
consistency.

## 9. Migration requirements

### 9.1 Database versioning

Add real schema-version migration tests for the continuity/ownership database.

Required scenarios:

- empty database creates the current schema;
- previous binding-only schema upgrades without data loss;
- reopening the database preserves bindings and ownership;
- a database with a newer unsupported version fails closed;
- ambiguous historical ownership is not guessed;
- raw identifiers and credentials are absent from SQLite main, WAL, and SHM files
  after checkpoint/close.

Do not use `INSERT OR REPLACE` on a version table as a substitute for ordered
migrations. Read the current version, apply explicit steps, and advance it only
after each migration succeeds.

### 9.2 Historical Agent Sessions

Migration/backfill must:

1. enumerate Conversations referenced by Agent Sessions;
2. group sessions by workspace, Conversation, and owner;
3. create ownership when the owner is unambiguous;
4. retain all sessions without changing their owner IDs;
5. leave ambiguous ownership Admin-visible and MCP-unclaimable;
6. produce bounded diagnostics/counts for migrated, unowned, and ambiguous rows.

Admin visibility must not depend on rewriting historical owners to `admin`.

### 9.3 Existing transcript-only Conversations

- Keep them intact.
- They remain visible in Admin.
- Do not invent MCP ownership.
- Starting a new Admin execution may attach an Admin-managed Agent Session without
  making the transcript automatically resumable by unrelated MCP principals.

## 10. Detailed test matrix

### 10.1 Binding and identity unit tests

- same raw value under different transport kinds yields different window digests;
- raw ID is absent from object repr and durable files;
- principal, window, workspace, and repo changes independently break exact match;
- same exact scope resolves after reopening the Store;
- two windows created concurrently at the same frozen time get distinct
  Conversation IDs;
- binding use revalidates ownership and workspace authorization;
- unsupported/malformed/oversized IDs fail closed.

### 10.2 Ownership and migration tests

- principal A cannot list/resume principal B's Conversation in the same workspace;
- unauthorized IDs return not-found semantics;
- Admin can view historical Agent Sessions owned by OAuth/bearer/noauth principals;
- Admin actions still require workspace authorization;
- unambiguous legacy owner backfills correctly;
- ambiguous owners are not guessed;
- transcript-only imports remain Admin-visible and MCP-unowned.

### 10.3 Real HTTP MCP tests

Use two independent clients with the same credential, workspace, and repository:

```text
initialize A -> header A
initialize B -> header B
A performs Conversation-recording work -> Conversation A
B performs Conversation-recording work -> Conversation B
A second call -> Conversation A
B second call -> Conversation B
assert Conversation A != Conversation B
```

Restart scenario:

```text
establish A exact binding
stop Server while preserving durable databases
start a new Server instance
send an authenticated request with retained header A
recover a new live Runtime under the exact binding
assert same Conversation A
assert no duplicate work/job launch
```

Identity-loss scenario:

```text
initialize C with a new header
assert no automatic reuse of A or B
conversation_list returns only C-principal-owned Conversations
conversation_resume(A) explicitly rebinds C when authorized
unauthorized resume returns not-found
```

Also test:

- missing `Mcp-Session-Id` never uses a recent Conversation;
- changed repo scope creates/resolves a separate binding;
- returning to the original repo restores its exact binding;
- raw session ID/token is absent from database and logs;
- recovered unknown sessions remain subject to admission/identity quotas.

### 10.4 Evidence tests

For each supported tool category, test both success and failure:

- read/search/navigation records only relative explored paths;
- edit/apply records changed paths;
- validation records structured status and compact identity;
- failures omit shell output/file bodies/secrets;
- job transitions collapse by job ID;
- approval transitions collapse by approval ID;
- checkpoint persists bounded note/phase/next action;
- repeated paths deduplicate within an attempt;
- new instruction starts a new attempt;
- retention pruning honors per-kind and hard limits;
- active jobs and pending approvals survive pruning;
- more than 100 historical entries still projects the latest attempt;
- repeated continuation/handoff reads write no rows.

### 10.5 Projection purity tests

Inject spies/counters around:

- filesystem reads;
- Git/command execution;
- Runner calls;
- Agent backend calls;
- LLM calls;
- Transcript/context writes;
- activity refresh.

Build continuation and handoff repeatedly and assert all side-effect counters stay
zero and unchanged durable input produces byte-equivalent JSON.

### 10.6 Conversation service tests

Seed:

1. transcript-only imported Conversation;
2. Conversation with active Agent Session;
3. zero-message execution shell;
4. Conversation with closed A1 and active A2;
5. historical Agent Session owned by a non-Admin principal.

Assert one list row per Conversation, correct latest execution, complete execution
history, progress counts, authorization, and all execution operations.

### 10.7 WebUI tests

Node tests must cover the rendering and interaction cases in section 8.

Add real Playwright/browser QA on an isolated port:

1. open `/admin`;
2. authenticate with the existing Admin model;
3. open Conversation Center;
4. verify transcript-only and Agent-backed entries in one list;
5. verify historical non-Admin-owned Agent Session appears;
6. inspect structured Work / Progress;
7. create an execution and send a turn;
8. exercise resume/close and an approval when fixtures support it;
9. reload and confirm Admin session behavior;
10. confirm no `/app` or `/api/app/*` requests;
11. confirm browser console/page errors are zero;
12. test desktop and narrow mobile viewport layouts.

Do not use or restart the user's main `127.0.0.1:8765` service. Use an isolated
temporary config/database and a different port.

## 11. Implementation order

Keep the repository passing after each batch.

### Batch 0: baseline and safety

- [x] Confirm repository root and current worktree status.
- [x] Read all current diffs, especially the four WebUI files.
- [x] Record baseline focused test results.
- [x] Identify current database paths and test fixtures.
- [x] Do not edit unrelated OAuth files.

Acceptance gate:

- No existing user change is lost.
- Baseline failures are recorded honestly.

### Batch 1: ownership and migration foundation

- [x] Add explicit versioned ownership schema/store.
- [x] Add domain-separated owner digest helper.
- [x] Add ordered migrations.
- [x] Add historical Agent Session backfill.
- [x] Add Admin privileged workspace Session queries/actions.
- [x] Add cross-principal authorization tests.

Acceptance gate:

- Admin sees historical sessions.
- MCP principals cannot list/resume each other's Conversations.

### Batch 2: exact MCP continuity

- [x] Replace collision-prone Conversation ID generation.
- [x] Add Runtime current-Conversation context.
- [x] Add ToolSpec Conversation classification.
- [x] Resolve/create exact binding before classified work.
- [x] Implement retained-header restart recovery.
- [x] Make start/resume update Runtime context.
- [x] Add real two-window and restart HTTP tests.

Acceptance gate:

- Same window continues exactly.
- Two windows remain isolated.
- Restart recovery works only with retained exact identity.
- Identity loss requires explicit resume.

### Batch 3: bounded evidence recorder

- [x] Create centralized recorder.
- [x] Add attempt IDs.
- [x] Add stable dedupe/upsert IDs.
- [x] Add per-kind and hard retention limits.
- [x] Add explicit tool evidence adapters.
- [x] Add Agent event, job, approval, validation, and checkpoint hooks.
- [x] Add secret/large-payload negative tests.

Acceptance gate:

- Every required evidence kind has a real production producer and focused test.
- Durable rows remain bounded under repeated work.

### Batch 4: correct projections and summaries

- [x] Add recent-context query APIs.
- [x] Build latest-attempt continuation.
- [x] Collapse job/approval state correctly.
- [x] Fix handoff Conversation ID.
- [x] Harden 8192-byte bounding.
- [x] Add list progress projection.
- [x] Add purity and >100-history regression tests.

Acceptance gate:

- Latest facts win.
- Projections are pure, deterministic, and bounded.

### Batch 5: Admin API and WebUI completion

- [x] Complete unified API routes and pagination.
- [x] Add structured Conversation summary/detail payloads.
- [x] Add Work / Progress UI.
- [x] Add execution/turn/resume/close/approval/validation controls.
- [x] Add visible projection error states.
- [x] Extend i18n and DOM/model tests.
- [x] Integrate current uncommitted Admin UI changes.

Acceptance gate:

- A user can perform the supported Conversation workflow entirely in `/admin`.

### Batch 6: cleanup, build, documentation, and QA

- [x] Remove stale `/app` and Operator API documentation, including accepted ADRs
      that still prescribe `/api/app/*`, or supersede them explicitly.
- [x] Update Admin/Runner guides to match controls that actually exist.
- [x] Run migration-cleanup repository searches.
- [x] Run WebUI production build.
- [x] Verify generated source/dist consistency.
- [x] Run full focused Python/MCP/WebUI/static gates.
- [x] Run isolated Playwright/browser QA.
- [x] Record exact commands and results.

Acceptance gate:

- No stale product docs, dead routes, dangling imports, or generated-file drift.
- All final gates pass.

## 12. Expected file areas

Likely backend files:

```text
coding_tools_mcp/conversation_continuity.py
coding_tools_mcp/conversation_evidence.py
coding_tools_mcp/transcript.py
coding_tools_mcp/operator_api.py
coding_tools_mcp/admin.py
coding_tools_mcp/server.py
coding_tools_mcp/transport_http.py
coding_tools_mcp/agent_session_store.py
coding_tools_mcp/agent_sessions.py
```

A new focused module is encouraged where it clarifies ownership or recording:

```text
coding_tools_mcp/conversation_service.py
coding_tools_mcp/conversation_ownership.py
coding_tools_mcp/conversation_recorder.py
```

Do not create all three automatically. Add only abstractions that produce a
clear ownership boundary and reduce duplicated policy.

Likely frontend files:

```text
webui/src/admin.html
webui/src/admin.css
webui/src/admin.js
webui/src/i18n.js
webui/tests/dom-interactions.test.mjs
webui/tests/i18n.test.mjs
webui/scripts/build.mjs
coding_tools_mcp/webui_dist/admin.html
```

Likely test files:

```text
tests/test_conversation_binding_store.py
tests/test_conversation_service.py
tests/test_conversation_evidence.py
tests/test_conversation_continuity.py
tests/test_admin_conversation_routes.py
tests/test_operator_api.py
tests/test_workspace_session_binding.py
tests/test_session_resilience_http_integration.py
tests/compliance/test_mcp_contract.py
tests/compliance/test_chat_persistence.py
tests/compliance/test_mcp_admin.py
tests/test_webui.py
```

Add focused files rather than turning existing suites into one large integration
test.

## 13. Validation commands

Discover and use repository-standard commands. At minimum run the following as
separate gates so failures remain attributable.

### Focused Python

```powershell
pytest tests\test_conversation_binding_store.py tests\test_conversation_evidence.py tests\test_conversation_service.py tests\test_admin_conversation_routes.py tests\test_operator_api.py -q
```

Add the new continuity/ownership suites to this command.

### MCP and persistence

```powershell
pytest tests\compliance\test_mcp_contract.py tests\test_workspace_session_binding.py tests\test_session_resilience_http_integration.py -q
```

Ensure the Conversation continuity cases execute rather than being skipped.

### Admin/WebUI Python

```powershell
pytest tests\test_webui.py tests\compliance\test_mcp_admin.py -q
```

### WebUI Node

```powershell
npm run test
```

Run from `webui`.

### WebUI build

```powershell
npm run build
```

Run from `webui`, then rerun `tests/test_webui.py`.

### Static checks

Use the repository's configured Ruff/lint/type/compile commands for every touched
Python and JavaScript file. Do not claim static validation if no command ran.

### Browser QA

Run Playwright against an isolated test Server and retain screenshots/log output
under the repository-controlled test-artifact location. Do not write project
work to system temp directories.

## 14. Final repository searches

Before completion, search for:

```text
/app
/api/app
OperatorSessionStore
operator session
Chat Conversations
Agent Sessions
conversation_start
conversation_resume
explored_path
changed_path
job_state
approval_state
checkpoint
continuation_feedback
handoff_brief
```

Every `/app` occurrence must be one of:

- an explicit removal/404 regression test;
- a historical taskbook clearly marked as historical;
- an unrelated path such as Docker `WORKDIR /app`;
- or removed/superseded documentation.

Every evidence kind must have:

- at least one real producer where applicable;
- a retention/deduplication rule;
- projection coverage;
- a WebUI representation or an explicit documented reason it is not displayed.

## 15. Final completion checklist

- [x] Current user WebUI changes were preserved and integrated.
- [x] `/admin` is the only functional management UI.
- [x] `/app` and `/api/app/*` remain removed/404.
- [x] Conversation ownership is durable and principal-scoped.
- [x] Admin can see authorized historical Agent Sessions without owner rewriting.
- [x] MCP explicit list/resume cannot cross principal or workspace boundaries.
- [x] Conversation IDs are collision-resistant.
- [x] Runtime work is attached to the exact bound Conversation.
- [x] Retained MCP identity supports exact restart recovery where the transport
      contract permits it.
- [x] Missing/changed identity never triggers automatic guessing.
- [x] Transcript-only, zero-message, and multiple-execution Conversations work.
- [x] Explored paths are recorded from real successful operations.
- [x] Changed paths are recorded from real successful mutations.
- [x] Validation and compact failure facts are recorded.
- [x] Job and approval state transitions are recorded and folded by identity.
- [x] Checkpoints are recordable and displayed.
- [x] Evidence history is deduplicated and hard-bounded.
- [x] Latest-attempt continuation is correct beyond 100 historical rows.
- [x] Continuation and handoff are pure and deterministic.
- [x] Handoff is always valid JSON at or below 8192 bytes.
- [x] Conversation summaries include progress.
- [x] WebUI provides structured Work / Progress.
- [x] WebUI provides supported execution controls.
- [x] Projection/API failures are visible and retryable.
- [x] Source and packaged WebUI are synchronized.
- [x] Migration tests pass.
- [x] Two-window isolation HTTP test passes.
- [x] Server-restart exact recovery HTTP test passes.
- [x] Explicit resume authorization tests pass.
- [x] SQLite/WAL/log privacy scan passes.
- [x] Node, Python, MCP, static, build, and browser gates pass.
- [x] Documentation describes actual behavior and no longer overclaims controls.

## 16. Required final report from the implementing Agent

The implementing Agent must not end with only “tests pass.” Its final report must
include:

1. architecture changes made;
2. database migrations and backfill behavior;
3. exact MCP restart-recovery semantics and limitations;
4. authorization behavior for Admin and MCP principals;
5. evidence producers and retention limits;
6. WebUI workflows now available;
7. every validation command actually run and its result;
8. browser QA URL/port and scenarios executed;
9. any skipped tests and why;
10. remaining risks, if any;
11. final `git status --short` summary;
12. confirmation that unrelated OAuth and pre-existing user changes were not
    reverted.

Do not mark the task complete while a required Server/test/build process is still
running, while generated WebUI differs from source, or while a critical
acceptance scenario is skipped.

## 17. Historical DoD audit, 2026-08-24

**Checkpoint status at that time: not marked.** Conversation acceptance and browser
privacy checks pass, but repository-standard static gates are not all green.
The failures below are in files outside this task's working-tree diff; they were
recorded rather than modified during the no-feature-expansion audit.

### Passing commands

| Command | Result |
| --- | --- |
| `& .\.tmp\dod-qa\run-dod-qa.ps1` | Pass. Real Admin login and execution create/send/resume/close on `127.0.0.1:18766`; no browser console/page/request failures and no `/app` requests. |
| `python -m unittest discover -s tests -p 'test_*.py'` | Pass: 667 tests, 86 skipped. |
| `python -m tests.compliance.runner --suite all --report` | Pass: 89 tests, 72 skipped. |
| `pytest tests\test_conversation_continuity_http.py -q` | Pass: 1 passed, no skip. Covers two windows, Server restart, retained exact identity, and explicit resume. |
| `pytest tests\test_conversation_binding_store.py tests\test_conversation_evidence.py tests\test_conversation_service.py tests\test_admin_conversation_routes.py tests\test_operator_api.py -q` | Pass: 25 passed, no skip. |
| `pytest tests\compliance\test_mcp_contract.py tests\test_workspace_session_binding.py tests\test_session_resilience_http_integration.py -q` | Pass: 15 passed, 37 skipped, 14 subtests passed. |
| `pytest tests\test_webui.py tests\compliance\test_mcp_admin.py -q` | Pass: 24 passed, 7 subtests passed. |
| `npm run test` from `webui` | Pass: 36 passed, 0 skipped. |
| `npm run build` from `webui` | Pass; regenerated packaged Admin and Wiki HTML from source. |
| `pytest tests\test_webui.py -q` after build | Pass: 3 passed; packaged source/dist synchronization verified. |
| touched-file `python -m ruff check --ignore=E501 ...` | Pass. |
| touched-file `python -m py_compile ...` | Pass. |
| `git diff --check` | Pass; only Git LF-to-CRLF working-copy warnings were emitted. |
| final `rg` searches from section 14 | Pass after classification: no live `/app` source/dist or `/api/app/*` route; remaining matches are removal tests, historical/superseded docs, or unrelated paths. |
| `netstat`/process checks for ports `18765`-`18767`, headless Chromium, and the QA Codex executable | Pass: no listeners or matching processes remained. |

The Playwright run generated real random Admin/MCP tokens, an Admin session
cookie, an MCP session ID, and an execution ID. Those in-memory values were used
to scan three SQLite databases plus WAL/SHM/log/browser artifacts. Result:
`integrity_checked=3`, `files_scanned=25`, `durable_execution_id_hits=2`, and no
raw token, Admin session, or MCP session leakage. Sanitized evidence is retained
under `.tmp/browser-qa/`.

### Skips and blockers

- The 72 release-compliance skips, including the 37 skips in the focused
  MCP-contract command, have the same Windows-only fixture preflight reason:
  `/dev/null` is not readable/writable because it is not a Windows device path.
  The Conversation HTTP acceptance suite was run separately with no skip.
- The full `unittest discover` skip breakdown is: 74 `/dev/null` fixture
  preflight skips; 2 POSIX shell-redirection skips; 2 tests requiring an
  initialized MSVC environment; and one each for Linux Landlock, POSIX `/tmp`
  fixture semantics, POSIX `/tmp` runtime semantics, POSIX permission modes,
  POSIX shell syntax, POSIX signal status, unavailable PySide6, and unavailable
  ConPTY. None replace the separately executed Conversation acceptance paths.
- Full repository Ruff failed with 22 pre-existing findings in unchanged
  `coding_tools_mcp/runner/capabilities.py`,
  `coding_tools_mcp/runner/websocket.py`, and
  `tests/test_runner_mcp_routing.py`.
- Repository Mypy could not start because the active Python environment does
  not have `mypy` installed.
- `python scripts\check_desktop_i18n.py` failed on pre-existing desktop
  OAuth/tunnel catalog drift in unchanged desktop-client files.

The failed standard commands were:

```powershell
python -m ruff check --exclude benchmarks/dogfood --ignore=E501 coding_tools_mcp apps\desktop-client\mcp_desktop_client tests benchmarks
python -m mypy --python-version 3.11 --disable-error-code union-attr --disable-error-code assignment --disable-error-code arg-type --disable-error-code no-untyped-def coding_tools_mcp benchmarks\mcp_http.py benchmarks\runtime_latency.py benchmarks\swebench\run_smoke.py benchmarks\swebench\generate_reference_predictions.py benchmarks\real_workloads.py
python scripts\check_desktop_i18n.py
```

Until those repository-wide static blockers are resolved and the standard
static commands are rerun successfully, the aggregate gate and Definition of
Done remain unchecked.

## 18. Final Remediation Follow-up, 2026-08-24

The previously recorded Ruff, Mypy, and desktop i18n blockers are resolved in
the current working tree: correct-version full Ruff, Mypy, i18n, compile,
full Python, compliance, WebUI test/build, and post-build consistency gates now
pass. See `reports/conversation-center-final-verification.md` for exact counts.

At this intermediate checkpoint the final remediation taskbook remained
**INCOMPLETE** because its generic identifier search included unrelated
Git-ignored Broker sandbox trees with ACL-denied descendants. Section 19 records
the corrected verification scope and supersedes this intermediate status.

## 19. Final Completion Audit, 2026-08-24

**Definition of Done status: COMPLETE.** The final review corrected the search
scope so generic identifier classification covers version-controlled source and
durable reports, while the actual-value scanner covers task-owned QA databases,
WAL/SHM, screenshots, CSRF, cookies, credentials, and MCP Session IDs. Unrelated
Git-ignored Broker sandbox trees created before this task are not verification
inputs and their ACLs were not changed.

The review also found and fixed one remaining MCP list metadata defect:
owner-scoped results now set `truncated` when the authorized total exceeds the
returned limit. The regression test is included in the 42-test focused suite.
Correct-version Ruff/Mypy, desktop i18n, compileall, 684-test full Python suite,
89-test compliance suite, WebUI tests/build/post-build checks, searches,
`git diff --check`, and port cleanup were rerun successfully. Browser and
actual-value privacy evidence remain valid because the final code change affected
only MCP list pagination metadata and its test.

After that audit, a real Server startup with more than `MAX_LIST_LIMIT` historical
Agent Sessions exposed a second-page SQL binding defect in
`AgentSessionStore.iter_all()`. The query used the cursor timestamp twice but the
parameter tuple supplied it once. The fix duplicates the timestamp binding and a
new regression test traverses `MAX_LIST_LIMIT + 1` same-timestamp sessions using
the default batch size. Post-fix verification passed 49 focused Conversation and
Agent Session tests, the 684-test full Python suite with 86 platform skips, the
89-test compliance suite with 72 documented Windows skips, Ruff, Mypy, desktop
i18n, compileall, and `git diff --check`. Existing browser, WebUI build, and
privacy evidence remains applicable because this correction changes only the
Agent Session store query and its Python regression test.
