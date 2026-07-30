# Phase 09 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `c4abd33ad84b356922462e415e322b0301080423`
- Implementation commit: `c4250057b63aac0e939cb3978bce29cfcaa26d1f`
- Handoff commit: filled by the next agent from `git log`
- Phase 10 remains pending and was not started.

## Scope Completed

- Added Workspace-partitioned SQLite persistence for chat conversations, messages, durable context entries, and imported Codex session metadata.
- Added a non-Admin `WorkspaceTranscriptService` whose Workspace ID/root are fixed at construction and which exposes no global delete or cross-Workspace query path.
- Added bounded Codex session discovery/import for JSONL, JSON, and Markdown files under registered Workspace-relative roots.
- Added a Workspace-scoped CLI for message/context recording, paginated summaries, and explicit detail reads.
- Added dedicated-Admin service routes for global summaries, explicit details, bounded scan/import, and stable-ID idempotent deletion.
- Added Phase 09 integration-contract records and backend API documentation.
- Added real Windows-focused tests for GB18030, CRLF, exclusive file locks, partial JSONL, symlink escape, and oversized transcripts.

## Workspace and Identity Boundary

- Every persistence table and primary key includes `workspace_id`.
- Conversation, message, context, imported-session, query, cache, and deletion keys are Workspace-partitioned.
- Identical conversation/message IDs can coexist in different Workspaces without collision.
- Imported candidate IDs derive from Workspace ID plus Workspace-relative path.
- Imported message IDs are prefixed with the candidate ID so separate files that reuse source IDs remain distinct.
- The ordinary `WorkspaceTranscriptService` is constructed from one validated Workspace scope and cannot be redirected by caller arguments.
- Cross-Workspace listing, import, clear, and deletion remain behind the dedicated Phase 08 Admin authentication boundary.
- Unknown or disabled Workspaces fail closed through the validated Workspace Catalog.

## Scanner Boundary

Each scan fixes and validates limits before reading:

- maximum directory depth
- maximum candidate-file count
- maximum bytes per file
- maximum total bytes read
- maximum parsed messages
- bounded in-memory parse cache

Scan roots must be non-empty relative paths inside the selected registered Workspace. Absolute roots and `..` are rejected. Directory traversal does not follow symlinks; resolved paths are checked against the Workspace root so symlink/reparse-point escapes are excluded.

Supported decoding is UTF-8/UTF-8 BOM, UTF-16 BOM, and GB18030. Invalid encoding, locked/unreadable files, oversized files, malformed JSON records, and truncated/partially written JSONL are reported as per-file or per-line errors. Other candidates and valid lines continue to be returned.

## Persistence and API Semantics

- Database file: stable server config directory `transcripts.sqlite3`.
- SQLite migrations are versioned, transactional, repeatable on reopen, and fail closed on a newer schema.
- List endpoints return bounded summaries and pagination; full message/context content is returned only by the explicit conversation-detail endpoint.
- Message and context recording rejects excessive item counts and oversized content.
- Delete operations use `(workspace_id, stable_id)`, are idempotent, and report actual affected counts.
- Deleting an imported session also deletes its dedicated imported conversation/messages/context and reports each count.
- Clearing a Workspace affects only that Workspace and reports conversation/message/context/session counts.

Admin endpoints added:

- `GET /admin/api/chat/conversations`
- `GET /admin/api/chat/conversations/{workspace_id}/{conversation_id}`
- `POST /admin/api/chat/conversations/{workspace_id}/{conversation_id}/messages`
- `POST /admin/api/chat/conversations/{workspace_id}/{conversation_id}/context`
- `DELETE /admin/api/chat/messages/{workspace_id}/{message_id}`
- `DELETE /admin/api/chat/context/{workspace_id}/{context_id}`
- `DELETE /admin/api/chat/conversations/{workspace_id}/{conversation_id}`
- `POST /admin/api/chat/workspaces/{workspace_id}/clear`
- `POST /admin/api/codex/sessions/scan`
- `POST /admin/api/codex/sessions/import`
- `GET /admin/api/codex/sessions`
- `DELETE /admin/api/codex/sessions/{workspace_id}/{session_id}`

An actual HTTP test verifies that an ordinary MCP bearer receives 401 when attempting a chat deletion; only the dedicated Admin credential is accepted.

## Telemetry and Logging Boundary

- `transcript.py`, `codex_sessions.py`, and `chat_cli.py` contain no telemetry import or event emission.
- Chat content, transcript paths, commands, model responses, and summaries are not added to telemetry.
- Discovery summaries exclude message bodies; content appears only after explicit import/detail operations.
- No plaintext chat fixture, database, or transcript artifact was committed.

## Files Changed

- `coding_tools_mcp/transcript.py`
- `coding_tools_mcp/codex_sessions.py`
- `coding_tools_mcp/chat_cli.py`
- `coding_tools_mcp/admin.py`
- `coding_tools_mcp/server.py` (Admin persistence construction only)
- `docs/admin-api.md`
- `docs/chat-persistence.md`
- `docs/integration-contract-v0.2.2.md`
- `tests/compliance/test_chat_persistence.py`
- `tests/compliance/test_mcp_admin.py`
- `tests/test_integration_contract_v022.py`

No WebUI frontend assets, OAuth Store schema, OAuth/Refresh implementation, Settings validator/store, Secret Vault format, Gateway implementation, tool registry, or lockfile were changed.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.compliance.test_chat_persistence tests.test_integration_contract_v022` | 0 | Ran 26 tests; all passed. |
| Phase 03–09 focused regression suite | 0 | Ran 92 tests; all passed. |
| Ruff over Phase 09 source/tests | 0 | All checks passed. |
| `py_compile` over Phase 09 source/tests | 0 | All modules compiled. |
| Mypy over `transcript.py`, `codex_sessions.py`, `chat_cli.py`, and `admin.py` | 0 | No issues found. |
| Mypy over `server.py` with documented upstream baseline codes disabled | 0 | No additional issues found. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite security` | 0 | Native Windows runner exited 0; 15 bodies skipped by the known `/dev/null` preflight. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite mcp-contract` | 0 | Native Windows runner exited 0; 37 bodies skipped by the same preflight. |
| `git diff --check` and staged whitespace check | 0 | No whitespace errors. |
| scope/telemetry/credential/SQL/Gateway/tool-registry scans | 0 | All forbidden or sensitive matches were zero. |

## Real Windows Coverage

The focused tests executed successfully on the native Windows host and cover:

- GB18030 input and UTF-8 output
- CRLF JSONL
- partially written final JSONL records
- exclusive `CreateFileW` file locking
- invalid encoding
- oversized files
- symlink escape
- database reopen

## Known Platform Baseline

- The shared native-Windows security and MCP-contract runners continue to skip when their fixture requires `/dev/null`.
- Phase 09's focused Windows tests execute independently and passed.
- Final integrated Linux CI must execute the skipped compliance bodies before release.

## Remaining Risks and Deferred Work

- Phase 09 intentionally adds backend persistence and service/API surfaces only. The full WebUI consuming these APIs remains Phase 10.
- The CLI is available as `python -m coding_tools_mcp.chat_cli`; no new package script or lockfile change was introduced.
- SQLite data-at-rest encryption is not introduced in this phase; filesystem access control remains an operator responsibility.
- At Phase 09 completion, Phase 05's Refresh rotation/access-token insertion cross-transaction availability risk remained unchanged. It was resolved after Phase 12 by the supplemental atomic Refresh exchange fix.

## Phase 10 Preconditions

- Start from a clean worktree with Phase 09 marked complete.
- The WebUI must consume the stable summary/detail and Workspace-keyed Admin API rather than reading SQLite fields directly.
- Do not weaken dedicated Admin authentication, Workspace partitioning, scan limits, or explicit-detail content boundaries.
- Do not add Gateway hot reload or modify OAuth/Refresh persistence while building frontend assets.

## Secret Check

- No real chat transcript, user content, credential, database, Vault file, or private key was committed.
- Test content and credentials are synthetic canaries only.
