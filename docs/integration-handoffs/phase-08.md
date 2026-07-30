# Phase 08 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `0a7eadb4c94b8ec84a94a5c5cd162ef2eec91e3e`
- Implementation commit: `19287837e2e44578b5c3149171a44f50c384d998`
- Handoff commit: filled by the next agent from `git log`
- Phase 09 remains pending and was not started.

## Scope Completed

- Added a backend-only authenticated Admin API under `/admin/api`.
- Added a dedicated Admin credential boundary that is independent from ordinary MCP bearer and OAuth access-token authentication.
- Added service-backed Settings, Secret Vault, OAuth, Workspace, and Gateway management routes without SQL in HTTP handlers.
- Added response redaction for client-secret digests, token material, signing-key references, Vault values, and upstream credentials.
- Added separate active, persisted, pending-restart, and revision views for settings.
- Added revision/baseline conflict checks for Settings and Gateway writes.
- Added exact-ID idempotent OAuth management actions with affected counts and audit-event IDs for real transitions.
- Added Workspace add, disable, default, and known-ID check operations using the Phase 03 Workspace Catalog validation.
- Added Gateway validation and persistence with restart-only semantics; no dynamic reload/start/stop path was added.
- Connected Gateway `secret_ref` resolution to the server Secret Vault for control, stdio, and every HTTP Session Runtime.
- Unified startup, persisted Settings, Admin validation, and HTTP CORS checks on `normalize_allowed_origins`.
- Added a backend-only `/admin` information page; no frontend application assets were ported.

## Authentication Boundary

- The Admin API is disabled unless a dedicated Admin token is configured through:
  - `--admin-token`
  - `CODING_TOOLS_MCP_ADMIN_TOKEN`
  - persisted `admin_token_secret_ref` resolved from the server Secret Vault
- Admin clients may present that dedicated token through `Authorization: Bearer` or `X-Admin-Token`.
- An ordinary MCP bearer token is not accepted as Admin authentication merely because it authorizes `/mcp`.
- `/admin`, read endpoints, and write endpoints all enforce the same Admin check.
- Admin CORS uses the same active allowed-origin snapshot as the MCP endpoint.

## Settings Semantics

- `GET /admin/api/settings` returns:
  - `active`: immutable startup values currently used by the process
  - `persisted`: latest saved settings
  - `pending_restart`: fields where persisted and active values differ
  - `persisted_revision`: canonical SHA-256 revision for stale-write protection
- Secret references are represented only as configured/unconfigured metadata.
- Settings writes require `expected_revision`; stale pages receive HTTP 409 / `stale_revision`.
- Settings validation and writes reuse `normalize_startup_settings_with_warnings`.
- Allowed origins are normalized by the shared Settings validator and take effect only after restart when changed.

## Gateway Semantics

- Gateway status separates the active Runtime snapshot from the redacted persisted configuration.
- Gateway writes require the current persisted revision and atomically replace `mcp-servers.json`.
- A saved Gateway change returns `restart_required: true` when it differs from the startup snapshot.
- No Admin route calls or exposes upstream start, stop, reload, or Runtime mutation.
- Existing Runtime/Session `tools/list` snapshots remain immutable.
- Sensitive literal environment variables and sensitive headers are rejected from persisted Gateway config.
- `secret_ref` entries must resolve through the server Secret Vault before save and before Runtime creation.
- Missing Vault configuration, wrong Vault key, or unknown reference fails closed.

## OAuth Management

The service exposes redacted list operations for:

- Clients
- Grants
- Access Token metadata by `jti`
- Refresh Token Families
- Signing Keys by `kid`
- Audit Events

Supported state actions use exact IDs:

- Client enable/disable
- Grant revoke
- Access Token revoke
- Refresh Family revoke
- Signing Key activate/retire/revoke

Actions are idempotent. Responses return `found`, `affected_count`, and the new `audit_event_id` only when a real transition generated an event. A revoked Signing Key cannot be reported as reactivated.

## Workspace Management

- Workspace add/default/disable operations use a full Settings revision check.
- The resulting catalog is validated through `WorkspaceCatalog` and Settings normalization.
- Nested roots, duplicate roots/IDs, filesystem roots, missing directories, disabled defaults, and invalid identifiers remain rejected.
- Workspace check accepts only a catalog Workspace ID. It does not accept or resolve an arbitrary caller-supplied path.
- Persisted mapping changes affect new Runtime creation after restart; existing Session bindings remain immutable.

## Response and Secret Boundary

- Client-secret digests are removed from Admin responses.
- Bearer and Refresh Token plaintext is never returned or persisted by Admin.
- Signing-key `secret_ref` values are removed from list responses.
- Secret Vault values are never returned; the API exposes only secret names and configured state to authenticated administrators.
- Gateway `secret_ref` and `env_ref` objects are returned only as source/configured metadata.
- Internal backing-service failures use generic redacted HTTP error messages.
- No real credential or Vault file was committed.

## Files Changed

- `coding_tools_mcp/admin.py`
- `coding_tools_mcp/webui.py` (backend-only HTML entry skeleton)
- `coding_tools_mcp/server.py` (Admin composition/routing, shared CORS, Gateway Vault resolver)
- `docs/admin-api.md`
- `docs/integration-contract-v0.2.2.md`
- `tests/compliance/test_mcp_admin.py`
- `tests/test_integration_contract_v022.py`

No OAuth Store schema, Refresh transaction, Admin frontend asset bundle, tool registry, desktop, or Phase 09 persistence files were changed.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.compliance.test_mcp_admin tests.test_integration_contract_v022` | 0 | Ran 20 tests; all passed. |
| Phase 03–08 focused regression suite | 0 | Ran 76 tests; all passed. |
| Ruff over all Phase 08 source/tests | 0 | All checks passed. |
| `py_compile` over all Phase 08 source/tests | 0 | All modules compiled. |
| Mypy over `admin.py` and `webui.py` | 0 | No issues found. |
| Mypy over `server.py` with the documented upstream baseline codes disabled | 0 | No additional issues found. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite security` | 0 | Native Windows runner exited 0; 15 bodies skipped by the known `/dev/null` preflight. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite mcp-contract` | 0 | Native Windows runner exited 0; 37 bodies skipped by the same preflight. |
| `git diff --check` and staged whitespace check | 0 | No whitespace errors. |
| high-risk credential scan | 0 | No PAT, private key, long Bearer, or JWT pattern found. |
| SQL scan of Admin service and added Server lines | 0 | No SQL found. |
| dynamic Gateway lifecycle scan | 0 | No start/stop/reload control path found. |
| forbidden-scope and `TOOL_REGISTRY` scans | 0 | No OAuth Store/schema, Refresh, lockfile, frontend bundle, or tool-registry definition changes. |

## Known Platform Baseline

- Native Windows compliance runners continue to skip when their shared fixture requires `/dev/null`.
- Phase 08's real HTTP authentication and management tests execute on Windows and passed independently.
- Final integrated Linux CI must execute the security and MCP contract test bodies before release.

## Remaining Risks

- Phase 08 provides the authenticated backend API and only a minimal information page; a full Admin frontend remains a later phase/product task.
- Admin token rotation is operator-driven through environment/settings/Secret Vault configuration and restart; no online token-rotation endpoint was added.
- Secret names are visible to authenticated administrators, but values remain non-readable.
- Gateway and Settings changes deliberately require restart; this preserves Phase 07 snapshot invariants.
- At Phase 08 completion, Phase 05's Refresh rotation/access-token insertion cross-transaction availability risk remained unchanged. It was resolved after Phase 12 by the supplemental atomic Refresh exchange fix.
- The server Secret Vault cryptographic format still requires the separate security review recorded in Phase 03.

## Phase 09 Preconditions

- Start only from a clean worktree with Phase 08 marked complete.
- Do not weaken dedicated Admin authentication or response redaction.
- Do not introduce Gateway hot reload while adding transcript/session persistence.
- Keep OAuth Store schema and Refresh issuance changes out of Phase 09 unless explicitly assigned.
- Preserve immutable OAuth Identity, Workspace, and Gateway bindings for existing Sessions.

## Secret Check

- No real Admin token, OAuth credential, Gateway credential, database, or Vault file was committed.
- Test credentials are synthetic canaries and are never returned as persisted artifacts.
