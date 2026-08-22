# Conversation Center / Session Continuity Integration Taskbook

> Status: handoff plan for follow-up sessions
>
> Project: `coding-tools-mcp`
>
> Primary product decision: **remove `/app` as a product entry point. `/admin` is the single WebUI entry.**
>
> Architecture reference: WebCodex (`yyjeqhc/webcodex`) session model, client-window identity, durable task/session binding, bounded continuation feedback, and handoff brief concepts.

## 1. Goal

Unify the currently separate WebUI concepts of **Chat Conversations** and **Agent Sessions** into one Conversation Center inside `/admin`, while preserving the correct separation of storage responsibilities underneath.

At completion:

- `/admin` is the only WebUI management entry.
- `/app` and `/api/app/*` no longer exist as independent product/authentication surfaces.
- A user sees one `Conversation` object in the WebUI.
- Transcript/message history, context evidence, agent execution state, backend thread state, and client-window bindings remain separate internal concerns.
- ChatGPT/MCP clients can automatically continue the correct conversation by an exact, privacy-preserving client-window binding.
- Different ChatGPT windows must never be merged merely because they share a credential, principal, workspace, or repository.
- Server restart must not destroy the durable relationship between a known client window and its Conversation.
- When the original transport identity cannot be recovered, continuation is explicit (`list`/`resume`), never guessed.
- Continuation state is reconstructed from bounded structured evidence, not by attempting to recreate hidden model context.

## 2. Non-goals / hard boundaries

Do **not**:

- replace `TranscriptStore` and `AgentSessionStore` with one giant table;
- make `Mcp-Session-Id` equal to `conversation_id`;
- persist raw `Mcp-Session-Id`, bearer tokens, OAuth tokens, passwords, CSRF secrets, GPT conversation IDs, or browser cookies in the Conversation database;
- fall back from a missing client-window identity to "the most recent conversation for this user/workspace";
- replay arbitrary shell output, file bodies, grep bodies, model hidden state, or unbounded tool results as continuation context;
- require a user to open `/admin` before ChatGPT/MCP can establish or continue a Conversation;
- keep a second `/app` password/session system after migration;
- overwrite unrelated existing OAuth work in `coding_tools_mcp/oauth.py`, `coding_tools_mcp/oauth_store.py`, or `tests/test_oauth_integration.py` unless a task explicitly requires it.

## 3. Existing baseline that must be understood before editing

The working tree already contains uncommitted work from the preceding session. Treat it as input, not necessarily as final architecture.

Relevant existing work includes:

- `AgentSessionStore` now has `conversation_id` and durable execution fields.
- `AgentSessionService.bind_conversation(...)` exists.
- `OperatorAPIService` currently links Agent Sessions to `TranscriptStore`, records user/assistant messages, and projects transcript title/preview/message count into Agent Session summaries.
- `TranscriptStore` already contains:
  - `chat_conversations`
  - `chat_messages`
  - `chat_context_entries`
  - `imported_sessions`
- `/app` currently has a newly added browser-session exchange using `OperatorSessionStore`, HttpOnly cookies, and CSRF. This solved refresh reauthentication but is now superseded by the product decision to remove `/app` entirely.
- `transport_http.py` already has an in-memory `HTTPSessionManager` for MCP transport sessions. Keep transport lifetime separate from durable Conversation lifetime.

Current uncommitted files observed before this taskbook was created include:

```text
coding_tools_mcp/agent_session_store.py
coding_tools_mcp/agent_sessions.py
coding_tools_mcp/oauth.py                         # unrelated/pre-existing: preserve
coding_tools_mcp/oauth_store.py                   # unrelated/pre-existing: preserve
coding_tools_mcp/operator_api.py
coding_tools_mcp/server.py
coding_tools_mcp/webui_dist/admin.html
coding_tools_mcp/webui_dist/app.html
tests/test_oauth_integration.py                   # unrelated/pre-existing: preserve
tests/test_operator_api.py
webui/src/app.html
webui/src/app/api-client.js
webui/src/app/app.js
webui/src/i18n.js
webui/tests/app-model.test.mjs
coding_tools_mcp/operator_sessions.py             # new/untracked
```

Before changing these files, inspect the actual current diff. Do not assume the list above is still complete.

## 4. Target model

The product-level primary object is `Conversation`.

```text
Conversation
├─ identity / UI summary
│  ├─ conversation_id
│  ├─ workspace_id
│  ├─ title
│  ├─ preview
│  ├─ source
│  ├─ message_count
│  ├─ created_at
│  └─ updated_at
│
├─ Transcript (0..N messages)
│  └─ TranscriptStore.chat_messages
│
├─ Context Evidence (0..N entries)
│  └─ TranscriptStore.chat_context_entries
│
├─ Agent Executions (0..N)
│  └─ AgentSessionStore.agent_sessions
│      ├─ agent_session_id
│      ├─ backend_kind
│      ├─ backend_thread_id
│      ├─ status
│      ├─ repo_fingerprint
│      └─ last_turn_id
│
└─ Client Bindings (0..N)
   └─ durable exact-hash binding store
      ├─ binding_key/hash
      ├─ conversation_id
      ├─ transport_kind
      └─ updated_at
```

Important cardinality rule:

> One Conversation may have zero, one, or multiple Agent Sessions over its lifetime.

Do not implement `Conversation == AgentSession`.

## 5. WebCodex concepts to reuse

Reuse the **ideas and invariants**, not repository-specific names or code.

### 5.1 Client-window identity abstraction

WebCodex normalizes transport-specific window identity (MCP session, GPT Actions conversation header, browser window cookie) into one internal client-window abstraction.

For this project, introduce an equivalent concept such as:

```text
ClientWindowIdentity
├─ transport_kind: mcp | webui | actions | other-supported-client
└─ window_key: domain-separated SHA-256 of the opaque transport identifier
```

Requirements:

- use domain separation, e.g. `coding-tools.client-window.v1`;
- include transport kind in the digest input;
- never persist raw transport identifiers;
- never derive continuity solely from credential identity.

### 5.2 Exact durable binding

Introduce a lightweight durable binding whose key includes all identity dimensions that must match for automatic continuation.

Recommended logical key material:

```text
principal identity
+ transport kind
+ hashed client-window identity
+ workspace identity
+ canonical repository/workspace fingerprint
```

The durable database stores only a hash of the exact composite key and the target `conversation_id` plus bounded metadata.

Exact match:

```text
same principal
+ same client window
+ same workspace/repo scope
=> resume bound Conversation
```

No exact match:

```text
=> create a new Conversation OR require explicit resume
```

Forbidden fallback:

```text
same credential / same principal / same workspace
=> most recent Conversation
```

### 5.3 Transport session is not Conversation

Keep `HTTPSessionManager` as a transport/session-lifetime manager.

The relation should be:

```text
Mcp-Session-Id
   │
   └─ derive ClientWindowIdentity
          │
          └─ exact durable Conversation binding
                 │
                 └─ conversation_id
```

`Mcp-Session-Id` must not be used as a durable Conversation primary key.

### 5.4 Bounded evidence ledger

Use existing `chat_context_entries` for bounded structured evidence instead of creating an unbounded scratchpad database.

Candidate `kind` values:

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

Persist facts, not large payloads. Prefer project-relative paths and compact metadata.

Do not persist through this layer:

- complete file bodies;
- complete shell stdout/stderr;
- raw grep/search results;
- arbitrary tool result JSON;
- hidden reasoning/model state;
- secrets or credentials.

### 5.5 Pure continuation projection

Implement `continuation_feedback` (name may differ) as a **read-only projection** of already durable state.

It must not, merely by being requested:

- execute a command;
- read repository files;
- call Git;
- call a runner;
- call another LLM;
- modify a Conversation;
- rescan the project.

Suggested output:

```json
{
  "status": "available",
  "previous_instruction": "...",
  "exploration": {"paths": []},
  "changes": {"paths": []},
  "validation": {"status": "passed|failed|unknown", "failures": []},
  "jobs": {"running": 0, "recovering": 0},
  "approvals": {"pending": 0},
  "suggested_actions": []
}
```

Keep every collection and text field bounded.

### 5.6 Bounded handoff brief

Provide a compact `handoff_brief` for a new ChatGPT window, a replacement backend thread, or another agent implementation.

Include only:

- root/latest instruction;
- changed paths;
- recent explored paths;
- validation state;
- unresolved failures/blockers;
- active/recovering jobs;
- next actions;
- progress state.

Target hard serialized size limit: **8192 bytes** unless an existing project convention dictates a smaller limit.

The handoff brief is not a transcript replay and must not claim to reconstruct hidden model context.

## 6. Product/API decision: remove `/app`

`/app` is no longer a supported user-facing management entry.

Final desired routing:

```text
/admin                  -> single WebUI management entry
/api/admin/*            -> admin/control API used by the WebUI
/mcp                    -> MCP client endpoint
```

Remove or migrate:

```text
/app
/api/app/*
webui/src/app.html
webui/src/app/**
webui/tests/app-model.test.mjs
coding_tools_mcp/webui_dist/app.html
OperatorSessionStore if it becomes unused
```

Do not leave an independent `/app` authentication/session subsystem after migration.

If compatibility behavior is considered, prefer an explicit removal/404/410 after verifying no internal code relies on `/app`. Do not preserve `/app` as a second functional UI. A redirect to `/admin` should only be kept if the maintainer explicitly chooses transitional bookmark compatibility; it is not part of the target architecture.

## 7. Authentication decision

The Conversation Center inside `/admin` uses the existing `/admin` authentication/authorization model.

Requirements:

- no `Operator` password/token distinct from Admin;
- no separate browser login just for Conversation management;
- no raw bearer token persisted in browser storage;
- state-changing Admin API operations retain appropriate CSRF/origin protection where the current Admin security model requires it;
- MCP authorization remains independent from browser UI authentication;
- opening `/admin` is never required for MCP continuity.

If `coding_tools_mcp/operator_sessions.py` is only needed for `/app`, remove it after the Admin migration and delete its now-obsolete tests/paths.

## 8. Unified Conversation API

Create a unified application/service layer rather than making the UI join multiple stores itself.

Suggested service name:

```text
ConversationService
```

Responsibilities:

- list Conversation summaries from `TranscriptStore`;
- attach current/latest Agent execution projection from `AgentSessionStore`;
- expose Conversation detail;
- expose messages and context evidence;
- expose zero-or-more Agent executions;
- create/start an Agent execution for a Conversation;
- resume/close an execution;
- send a turn through the active execution;
- expose continuation feedback and handoff brief;
- enforce workspace/principal authorization once, centrally.

Candidate Admin API shape:

```text
GET    /api/admin/conversations
POST   /api/admin/conversations
GET    /api/admin/conversations/{conversation_id}
DELETE /api/admin/conversations/{conversation_id}          # only if existing policy allows

GET    /api/admin/conversations/{conversation_id}/messages
GET    /api/admin/conversations/{conversation_id}/context

GET    /api/admin/conversations/{conversation_id}/executions
POST   /api/admin/conversations/{conversation_id}/executions
GET    /api/admin/conversations/{conversation_id}/executions/{session_id}
POST   /api/admin/conversations/{conversation_id}/turns
POST   /api/admin/conversations/{conversation_id}/resume
POST   /api/admin/conversations/{conversation_id}/close

GET    /api/admin/conversations/{conversation_id}/continuation
GET    /api/admin/conversations/{conversation_id}/handoff
```

Exact path names may follow current Admin router conventions; consistency with existing APIs is more important than these literal examples.

Do not expose `backend_thread_id` in broad list projections unless the existing security/UI model explicitly requires it. Treat it as execution detail/internal state.

## 9. Conversation summary projection

The Admin WebUI list should use one summary projection containing both human conversation context and current execution state.

Recommended shape:

```json
{
  "conversation_id": "...",
  "workspace_id": "...",
  "title": "...",
  "preview": "...",
  "message_count": 0,
  "created_at": 0,
  "updated_at": 0,
  "source": "...",
  "execution": {
    "session_id": "...",
    "backend_kind": "codex",
    "status": "ready",
    "last_turn_id": "...",
    "updated_at": 0
  },
  "progress": {
    "changed_path_count": 0,
    "validation_status": "unknown",
    "unresolved_failure_count": 0,
    "active_job_count": 0
  }
}
```

`execution` may be null for imported/history-only conversations.

## 10. Admin WebUI target

Replace duplicated Chat Conversation / Agent Session concepts with one `Conversations` section in `/admin`.

List row/card should show, when available:

- title;
- workspace/project;
- preview;
- last activity;
- message count;
- execution/backend status;
- compact progress indicators (changed files, validation, blockers/jobs).

Conversation detail should group information instead of presenting separate products:

```text
Conversation
├─ Messages
├─ Work / Progress
│  ├─ continuation summary
│  ├─ changed/explored paths
│  ├─ validation/failures
│  └─ jobs/approvals
├─ Agent Execution
│  ├─ backend
│  ├─ status
│  └─ resume/close/send controls as allowed
└─ Handoff
   └─ bounded handoff brief
```

Imported conversations with no Agent execution must still render normally.

Newly created Agent executions with no messages yet must still have a Conversation shell.

## 11. MCP automatic continuity

Implement continuity at the MCP path, independent of the WebUI.

On an authenticated MCP `tools/call` with a valid MCP session identity:

1. derive a privacy-preserving `ClientWindowIdentity` from `Mcp-Session-Id`;
2. determine authenticated principal/workspace/repository scope;
3. compute exact binding key;
4. look up bound `conversation_id`;
5. if bound and authorized, use that Conversation;
6. if no exact binding exists, create/bind a new Conversation only when the invoked workflow requires a Conversation, or leave unbound for stateless tools if appropriate;
7. never choose a recent Conversation based on credential/workspace proximity.

If the MCP transport session is gone and exact window identity cannot be reproduced, use explicit tools/API for recovery, for example:

```text
conversation_list
conversation_resume
```

Exact public tool names should fit the existing tool catalog and exposure policy. Do not add broad tools without checking Broker/fixed-tool exposure constraints.

## 12. Binding storage

Implement a small durable store, either a new focused SQLite file/store or a carefully scoped table in an existing durable database.

Suggested logical record:

```text
binding_digest TEXT PRIMARY KEY
conversation_id TEXT NOT NULL
workspace_id TEXT NOT NULL
principal_scope_digest TEXT NOT NULL or otherwise non-secret stable scope
transport_kind TEXT NOT NULL
repo_scope_digest TEXT NOT NULL
created_at REAL NOT NULL
updated_at REAL NOT NULL
```

Security requirements:

- raw bearer/OAuth material never enters the table;
- raw `Mcp-Session-Id` never enters the table;
- raw GPT conversation/window IDs never enter the table;
- raw browser cookies never enter the table;
- repository scope should use existing canonical fingerprinting rather than storing unnecessary absolute paths in the binding key;
- authorization is revalidated when a binding is used; possession of a hash/key alone is not authorization.

Add migration/version tests.

## 13. Evidence recording

Build a bounded recorder around `TranscriptStore.record_context(...)`.

Candidate sources:

- accepted user/task instruction;
- successful file read/navigation/search -> relative explored path only;
- successful edit/apply operation -> changed path only plus compact operation metadata;
- validation/check completion -> check identity + pass/fail + compact failure identity;
- durable/recoverable job state transitions;
- approval/blocking state;
- explicit checkpoint/handoff data.

Deduplicate repeated entries when practical. Maintain ordering and bounded history so repeated reads do not grow forever.

Avoid recording secrets or arbitrary tool arguments/results.

## 14. Continuation projection rules

Projection should prefer deterministic facts in this order:

1. latest explicit task/user instruction;
2. latest execution status;
3. unresolved failure/blocker facts;
4. current/recent validation facts;
5. changed paths;
6. recent explored paths;
7. active/recovering job facts;
8. bounded suggested next actions derived by deterministic rules.

Suggested actions should be rule-based, e.g.:

```text
failed validation exists -> rerun/fix relevant validation
pending approval exists   -> resolve approval
active job exists         -> observe existing job; do not duplicate execution
no validation after edits -> validate changed work
otherwise                 -> continue latest instruction
```

Do not make continuation projection dependent on an LLM call.

## 15. Explicit resume semantics

Explicit resume must transfer or create a client binding to an existing Conversation after authorization.

It must not:

- duplicate transcript history into a new Conversation unless explicitly requested as a fork;
- silently merge two Conversations;
- reuse a backend thread owned by another principal/workspace;
- trust a client-provided `conversation_id` without ownership/workspace checks.

Define separate operations if both are needed:

```text
resume -> continue same Conversation
fork   -> new Conversation seeded with a bounded handoff, not a DB-level identity merge
```

## 16. Migration from current `/app` implementation

The current `/app` changes contain reusable backend work. Migrate selectively.

Keep/reuse where appropriate:

- Agent Session <-> `conversation_id` binding;
- transcript recording of accepted user turns;
- transcript recording of completed assistant AgentMessage events;
- summary lookup optimization/bulk projection ideas;
- ownership/workspace validation;
- backend session lifecycle helpers.

Move/refactor:

- useful `OperatorAPIService` behavior into unified Conversation/Admin service boundaries;
- tests proving transcript persistence into Admin/Conversation API tests.

Delete after migration if unused:

- `webui/src/app.html`;
- `webui/src/app/api-client.js`;
- `webui/src/app/app.js`;
- `webui/tests/app-model.test.mjs`;
- generated `coding_tools_mcp/webui_dist/app.html`;
- `/app` server route;
- `/api/app/*` routes;
- `OperatorSessionStore` and `/app`-only cookie/CSRF plumbing;
- app-specific help/i18n strings.

Do not delete the transcript/session persistence improvements merely because they were first implemented behind `/app`.

## 17. Work plan table

| Phase | Priority | Work item | Main areas/files | Deliverable | Acceptance gate |
|---|---:|---|---|---|---|
| P0 | Critical | Baseline/diff audit | git status/diff, current tests | Inventory current uncommitted `/app`, transcript, Agent Session, OAuth changes | No unrelated changes overwritten; current baseline tests recorded |
| P1 | Critical | Define unified Conversation projection/service | `transcript.py`, `agent_session_store.py`, new/refactored service | Conversation summary/detail with optional execution | Imported transcript-only and execution-backed Conversations both list correctly |
| P2 | Critical | Move Agent Session operations into Admin API | `admin.py`, `operator_api.py` or replacement, `server.py` | `/api/admin/...` Conversation/execution endpoints | Create/list/detail/send/resume/close work without `/api/app/*` |
| P3 | Critical | Merge Admin WebUI views | `webui/src/admin*`, i18n, DOM/model tests | One Conversations section in `/admin` | No duplicate Chat Conversation vs Agent Session list |
| P4 | Critical | Remove `/app` and independent auth | `server.py`, app source/dist/tests, `operator_sessions.py` | `/app` product surface removed; Admin auth reused | `/admin` is only WebUI management entry; no Operator login/password remains |
| P5 | Critical | Add ClientWindowIdentity abstraction | new focused module + MCP handler integration | Domain-separated hashed transport window identity | Raw MCP/window IDs are not persisted; source kind changes digest |
| P6 | Critical | Add durable Conversation binding store | new store/migration/tests | exact binding digest -> Conversation | Restart-safe binding; principal/workspace/repo scope enforced; no credential-wide fallback |
| P7 | Critical | Wire MCP continuity | `server.py`/runtime/tool context + service | Same GPT/MCP window resumes correct Conversation automatically | Two windows on same credential/repo remain isolated; same exact binding resumes |
| P8 | High | Add explicit list/resume path | tool/API catalog respecting Broker exposure | Authorized explicit recovery when transport identity changes | Resume works; unauthorized IDs look not-found; no implicit guessing |
| P9 | High | Record bounded context evidence | `transcript.py` + tool/execution hooks | structured `chat_context_entries` | Paths/checks/failures recorded without large result bodies/secrets |
| P10 | High | Implement continuation feedback | Conversation service/projection tests | pure bounded continuation JSON | No I/O/tool/LLM side effect on projection; deterministic tests pass |
| P11 | High | Implement handoff brief | Conversation service/API/UI | <=8192-byte compact handoff | Oversized histories still produce bounded valid output |
| P12 | Medium | Incorporate durable job/approval status | runner/job/approval integration | active/recovering/blocking facts in continuation | Existing job is observed, not duplicated after continuation |
| P13 | Critical | Full migration cleanup/build | tests, generated WebUI, docs | dead `/app` code removed, dist rebuilt | no `/app` source/dist route; no dangling imports; clean static checks |
| P14 | Critical | Browser + MCP regression QA | Playwright/browser, HTTP MCP tests | end-to-end evidence | `/admin` Conversation Center works; reload/auth works; MCP isolation/restart continuity pass |

## 18. Detailed acceptance tests

### A. Single WebUI entry

- `GET /admin` serves the management UI.
- The Admin UI contains Conversation management.
- No navigation points to `/app`.
- `/app` is not a functional second UI.
- `/api/app/*` is no longer required by any supported UI flow.
- No independent Operator password/token prompt remains.

### B. Conversation fusion

Seed three cases:

1. transcript-only imported Conversation;
2. Conversation with an active Codex Agent Session;
3. Agent execution shell with zero messages.

All must appear in one list without duplicate rows representing the same `conversation_id`.

### C. Multiple executions per Conversation

- Start Conversation C1 with AgentSession A1.
- Close/fail A1.
- Start AgentSession A2 for C1.
- C1 remains the same Conversation and shows both executions in detail/history.
- Summary projects the correct latest/current execution.

### D. Two ChatGPT/MCP windows do not cross-contaminate

Same principal, same OAuth/bearer credential, same workspace, same repository:

```text
window A -> Conversation A
window B -> Conversation B
```

Assertions:

- A's subsequent turn continues A;
- B's subsequent turn continues B;
- no "most recent Conversation" fallback occurs.

### E. Exact binding restart recovery

- Establish a Conversation binding.
- Restart the server without deleting durable databases.
- Re-present the exact supported client-window identity/scope.
- The same Conversation is recovered.
- Transport runtime/session may be new; Conversation identity stays stable.

### F. Missing/changed identity

- Missing `Mcp-Session-Id` or changed exact identity must not bind to a recent Conversation based on credential.
- Explicit authorized resume can rebind to an existing Conversation.

### G. Privacy/security

Search durable databases/logs and verify absence of:

- raw bearer token;
- raw OAuth token;
- raw `Mcp-Session-Id`;
- raw GPT conversation/window header value;
- browser session cookie;
- app password (which should no longer exist).

### H. Continuation purity

Request continuation repeatedly and verify:

- no filesystem read count changes caused by the projection itself;
- no command/job is launched;
- no Git command is launched;
- no runner call is issued;
- no new context row is written;
- output is deterministic for unchanged durable state.

### I. Boundedness

- many repeated explored paths are deduplicated/bounded;
- very long instructions are truncated/summarized by deterministic bounded rules;
- `handoff_brief` serialized output stays <= 8192 bytes;
- large shell/tool outputs do not appear in continuation evidence.

## 19. Suggested test files / coverage

Prefer extending focused existing suites instead of one giant integration test.

Likely coverage locations:

```text
tests/test_operator_api.py                   # migrate/rename scope as service changes
tests/compliance/test_agent_platform.py
tests/compliance/test_chat_persistence.py
tests/test_workspace_session_binding.py
tests/test_session_resilience_http_integration.py
tests/compliance/test_mcp_contract.py
webui/tests/*admin*                          # existing Admin model/DOM suites
```

Add focused new tests if appropriate, for example:

```text
tests/test_conversation_service.py
tests/test_conversation_binding_store.py
tests/test_conversation_continuity.py
tests/test_continuation_projection.py
```

## 20. Validation commands / gates

Use project-standard commands discovered from the repository. At minimum, the final execution session should run:

```text
focused Python tests for modified services/stores
MCP contract/session tests
chat persistence tests
WebUI model/DOM tests
WebUI production build
ruff/static checks for touched Python files
Python compile/import check if part of existing workflow
browser QA against an isolated local port
```

Do not modify or restart the user's main `127.0.0.1:8765` process merely to perform QA; use an isolated test port unless the user explicitly requests otherwise.

## 21. Browser QA scenarios

Using `/admin` only:

1. authenticate according to the existing Admin model;
2. open Conversations;
3. confirm transcript-only and Agent-backed entries are in one list;
4. open detail and verify Messages + Work/Progress + Execution;
5. create/start a Conversation execution if the UI supports it;
6. submit a turn and observe transcript update;
7. reload the page and confirm Admin login/session behavior remains correct;
8. verify no `/app` requests are generated;
9. verify browser console/page errors are zero;
10. verify navigation contains no `/app` link.

## 22. MCP end-to-end QA scenarios

At minimum:

```text
initialize A -> session/window A
initialize B -> session/window B

A: start/continue work in same workspace
B: start/continue work in same workspace

assert conversation_A != conversation_B

A second turn -> conversation_A
B second turn -> conversation_B
```

Then restart the service and exercise the supported exact-binding recovery path.

Also test explicit resume from a fresh/changed window identity.

## 23. Migration cleanup checklist

Before declaring complete, search the repository for all of:

```text
/app
/api/app
OperatorSessionStore
operator session
app-model
webui_dist/app.html
Chat Conversations
Agent Sessions
```

Each occurrence must either:

- be deleted;
- be migrated to the unified Conversation Center;
- or have a documented compatibility/test reason to remain.

Do not leave stale docs telling users to open `/app` or enter an Operator password.

## 24. Documentation updates

Update user/admin documentation so it consistently states:

- `/admin` is the management UI;
- ChatGPT connects through `/mcp`, not `/admin`;
- opening `/admin` is not required for GPT/MCP operation;
- Conversation continuity is based on exact client-window + principal + workspace/repo binding;
- missing transport identity requires explicit resume rather than implicit guessing;
- Conversation transcript and continuation evidence are related but different layers;
- handoff/continuation does not recreate hidden model context.

## 25. Definition of done

This work is complete only when all of the following are true:

- [ ] `/admin` is the only WebUI management entry.
- [ ] `/app` is removed as a functional entry.
- [ ] `/api/app/*` is no longer part of the supported WebUI architecture.
- [ ] Independent Operator authentication/session code is removed if no longer used.
- [ ] WebUI shows one Conversation list, not separate Chat Conversation and Agent Session products.
- [ ] A Conversation may exist without an Agent Session.
- [ ] A Conversation may own multiple Agent Sessions over time.
- [ ] MCP client-window identity is hashed/domain-separated before durable use.
- [ ] Raw transport/window identifiers and credentials are not persisted in Conversation bindings.
- [ ] Exact binding prevents two ChatGPT windows from sharing a Conversation accidentally.
- [ ] No credential-wide/recent-conversation fallback exists.
- [ ] Exact supported binding can survive server restart.
- [ ] Explicit resume exists for identity-loss cases where needed.
- [ ] `chat_context_entries` (or an equivalently bounded evidence layer) records useful structured work facts.
- [ ] continuation feedback is pure, bounded, deterministic, and side-effect free.
- [ ] handoff brief is bounded (target <= 8192 bytes).
- [ ] Existing transcript recording and Agent backend resume behavior remain functional.
- [ ] OAuth unrelated changes are preserved.
- [ ] Python, MCP, WebUI, build, static, and browser QA gates pass.
- [ ] Generated WebUI artifacts are rebuilt from sources.
- [ ] User documentation no longer references `/app` as an entry.

## 26. Implementation guidance for follow-up sessions

Prefer incremental commits/patch sets in this order:

```text
1. service/data projection
2. Admin API migration
3. Admin UI fusion
4. /app removal
5. client-window + binding persistence
6. MCP continuity wiring
7. evidence + continuation/handoff
8. cleanup/docs/full QA
```

Do not attempt a single rewrite of server routing, persistence, UI, and MCP continuity at once. Preserve passing gates after each phase so regressions are attributable.

When a follow-up session discovers that an existing abstraction already satisfies a task, reuse it rather than duplicating WebCodex terminology mechanically. The reference architecture is valuable for its invariants:

```text
exact identity, not guessing
durable work identity, not transport lifetime
bounded evidence, not transcript/model replay
one product-level conversation, multiple internal state layers
shared management/auth boundary, not a second application
```

Those invariants are the required outcome.

