# Phase 12 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `1f53d5ce0f71110ddd5473c39c5173efa7fd2522`
- Implementation commit: `ae3eed46bb0d610adceb31576feeed50c601c9de`
- Handoff commit: filled by the next agent from `git log`
- Phase 13 remains pending and was not started.

## Scope Completed

- Rewrote the English and Simplified Chinese READMEs around the integrated
  v0.2.2 behavior instead of the pre-integration single-Workspace/stateless
  OAuth description.
- Added an `Unreleased` CHANGELOG section for persistent OAuth, immutable
  Workspace binding, Gateway snapshots, Admin API/WebUI, chat/session
  persistence, packaging integration, security boundaries, and the then-retained
  Refresh issuance availability risk. That risk was resolved by the subsequent
  supplemental atomic Refresh exchange fix.
- Added `docs/migration-v0.1-to-v0.2.2.md` with explicit backup, migration,
  reauthorization, signing-key, Gateway, Admin, Desktop-separation, and full
  snapshot rollback guidance.
- Updated the normative Runtime and tools/schema documents for Session identity,
  Workspace binding, persistent OAuth, upstream namespaces, real
  `listChanged: false`, and `UPSTREAM_TOOL_COLLISION`.
- Updated Remote MCP, MCP client configuration, telemetry, and chat persistence
  documentation to the final Phase 03–11 contracts.
- Expanded `docs-required` coverage so the integrated boundaries cannot silently
  disappear from user/operator documentation.
- Kept `docs/profile-v0.1.md` absent. Historical `tool_profile` values are
  described only as migration inputs.

## Final Documented Contract

### MCP and Session semantics

- Primary MCP protocol: `2025-11-25`.
- Explicit compatibility: `2025-06-18`.
- Every successful HTTP `initialize` creates an independent Runtime.
- Authorization context, Workspace binding, cwd, processes, retained output,
  project instructions, runtime directories, and Gateway snapshot remain
  Session-local and immutable.
- Subsequent requests must use the returned `Mcp-Session-Id`, negotiated protocol
  version, and matching authorization context.
- stdio uses the explicit/default Workspace and does not invent an OAuth Agent.

### Fixed catalog and permission behavior

- The default local catalog has 20 tools; `view_image` remains the sole optional
  capability gate.
- Permission modes change command policy, never `tools/list`.
- Legacy `tool_profile` is removed on migration with
  `legacy_tool_profile_ignored` and has no runtime, CLI, UI, launcher, or Gateway
  control path.
- `--dangerously-fake-readonly-annotations` changes exposure hints only. It does
  not hide tools, prevent execution, or create a security boundary.

### OAuth persistence

- Clients, Grants, access-token `jti` metadata, refresh-token families,
  signing-key metadata, and audit events persist in `oauth.sqlite3`.
- DCR requests are narrowed to `authorization_code`, `refresh_token`, and
  response type `code`.
- Authorization codes remain short-lived and process-local; Client registration
  and authorization state persist.
- Refresh tokens rotate and reused old tokens revoke the family.
- Client secrets persist only as digests, refresh tokens only as peppered hashes,
  and signing material only in the encrypted OAuth Secret Vault.
- Store/Vault/key failure remains fail-closed with no in-memory fallback.

### Agent-to-Workspace binding

- Bearer validation yields explicit `client_id`, `grant_id`, `workspace_id`, and
  `jti` context after JWT and Store-state checks.
- One enabled Workspace permits migration of old unbound Clients to the sole
  default; multiple Workspaces require an explicit mapping.
- Grants freeze the Workspace selected at authorization time.
- Disabled or unknown Workspaces reject new Sessions. Existing Sessions retain
  their frozen binding until close.

### Gateway and Admin

- Upstream tools are discovered before Runtime initialization and exposed under
  `{alias}__{remote_name}`.
- Local names remain reserved; namespace collisions fail closed with
  `UPSTREAM_TOOL_COLLISION`.
- Existing Runtime catalogs never change dynamically; Admin Gateway writes are
  persistence-plus-restart only.
- Upstream tools are remote capabilities and are not described as protected by
  local Workspace file confinement.
- `/admin` and `/admin/api` require a dedicated Admin token. Ordinary MCP bearer
  and OAuth credentials are not Admin authority.
- Settings/Gateway writes remain revision checked; stale `409` cannot silently
  overwrite newer persisted state.

### Telemetry

The document now lists the exact closed event set:

- `session_start`
- `tool_error` (max 20 per session)
- `tool_summary`
- `session_end`

It distinguishes the random telemetry-session identifier and MCP `clientInfo`
label/version from OAuth Client/Agent IDs and `Mcp-Session-Id`. Paths,
Workspace/OAuth identity IDs, command/argument/content data, upstream
inputs/results, and chat/transcript content remain excluded.

## Migration and Rollback Guidance

The new guide requires a clean shutdown and coordinated backup of:

- `server-settings.json`
- `mcp-servers.json`
- `oauth.sqlite3` and cleanly closed SQLite sidecars
- `oauth-secrets.json`
- `server-secrets.json`
- `transcripts.sqlite3` and cleanly closed SQLite sidecars
- the matching `CODING_TOOLS_MCP_SECRETS_KEY`
- the stable public OAuth origin

It explicitly states:

- old Workspace-local settings are not discovered recursively and require
  operator-reviewed migration into the stable user config directory;
- plaintext secret settings must not be copied into ordinary JSON;
- old stateless tokens may require one controlled reauthorization because they
  lack the persistent `kid`/Grant/Workspace/`jti` state;
- an older binary must not be started against newly migrated schemas;
- rollback must restore the complete matching Store/Vault/settings/Gateway/chat
  snapshot, not isolated OAuth DB or Vault files;
- retired signing keys must be preserved while valid tokens depend on them.

## Schema Drift Corrected

The pre-Phase 12 schema-drift runner exposed one real documentation defect:
`UPSTREAM_TOOL_COLLISION` existed in live Runtime code but was absent from the
Runtime contract error-code enumeration. The contract was corrected; the test
was not weakened.

The tools document also no longer claims a generic 16 KiB text-preview cap. It
now describes per-tool limits plus the emergency safety ceiling implemented by
the renderer.

## Files Changed

- `README.md`
- `README.zh-CN.md`
- `CHANGELOG.md`
- `docs/chat-persistence.md`
- `docs/mcp-client-config.md`
- `docs/migration-v0.1-to-v0.2.2.md`
- `docs/remote-mcp.md`
- `docs/runtime-contract-v0.2.md`
- `docs/telemetry.md`
- `docs/tools-and-schemas.md`
- `tests/compliance/test_docs_required.py`

No production Python, WebUI, Desktop, npm, Cloudflare, workflow, OAuth schema,
Gateway lifecycle, packaging configuration, `pyproject.toml`, or lockfile was
changed.

## Validation Performed

| Command/check | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite docs-required` | 0 | Ran 6 tests; all passed. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite schema-drift` | 0 | Ran 8 tests; all passed. |
| Isolated-HOME `.\.venv\Scripts\python.exe -m unittest tests.test_telemetry` | 0 | Ran 16 tests; all passed, including documentation/event-schema drift. |
| `.\.venv\Scripts\python.exe -m unittest tests.test_telemetry tests.compliance.test_docs_required tests.compliance.test_schema_drift tests.test_integration_contract_v022` | 0 | Ran 43 tests; all passed. |
| Ruff and `py_compile` over docs/schema test modules | 0 | Passed. |
| UTF-8 strict decode and replacement-character scan | 0 | All changed documents valid UTF-8; zero replacement characters. |
| Local Markdown link scan | 0 | Zero broken local links. |
| stale-current-claim scan | 0 | No current claim that refresh is unsupported, DCR is lost on restart, Workspace is globally shared, or Gateway hot reload exists. |
| `git diff --check` | 0 | No whitespace errors. |
| scope and credential scan | 0 | Zero production/packaging files, zero high-risk credential matches. |

The first standalone telemetry attempt inherited a Codex command environment
without a resolvable home directory, so `Path.home()` failed before event
creation. The final privacy/schema run used an isolated temporary HOME and
executed all 16 test bodies successfully. No production change was made for this
runner-environment issue.

## Known Retained Risks at Phase 12 Completion

- At Phase 12 completion, Phase 05 Refresh rotation and access-token metadata
  insertion still used separate transactions. The subsequent supplemental
  Refresh atomicity fix resolved this: all exchange writes and audits now share
  one transaction, and failure leaves the original token retryable.
- The server and OAuth Secret Vault cryptographic formats still require the
  security review already recorded by earlier phases.
- Final release validation should still run the full Linux gate matrix.

## Phase 13 Preconditions

- Start from a clean worktree with Phase 12 marked complete.
- Treat the current Runtime/Remote/Admin/Migration documents as the user-facing
  contract during full regression and security review.
- Do not reinterpret historical `tool_profile` examples as live configuration.
- Preserve the complete OAuth Store/Vault/key ring during release/rollback
  testing.
- Preserve the supplemental Refresh exchange atomicity invariant during Phase
  13 regression: no partial replacement, metadata, family timestamp, or audit
  may survive a failed exchange.

## Secret Check

- No real bearer token, refresh token, Client secret, Admin token, Vault master
  key, signing key, OAuth database, transcript, or private key was committed.
- All credential strings in documentation are placeholders.
