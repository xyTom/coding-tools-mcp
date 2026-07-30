# Phase 06 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `01fd2b6`
- Final implementation HEAD before handoff: `7a5ae53`
- Handoff commit: filled by the next agent from `git log`
- Phase 07 remains pending and was not started.

## Ordered Integration Commits

1. `f00c7e7 feat(oauth): add explicit workspace bindings`
2. `6533a7e feat(oauth): expose validated bearer identity`
3. `7a5ae53 feat(workspace): bind MCP sessions to catalog workspaces`

## Scope Completed

- Added OAuth Store schema v4 with explicit `workspace_id` columns on Clients and Grants.
- Kept migrated pre-v4 rows unbound (`NULL`) instead of inferring from Client names, redirect URIs, or metadata.
- Added explicit Client-to-Workspace mapping and copied it into each new Grant, freezing the authorization Workspace for that Grant.
- Added `OAuthIdentity(client_id, grant_id, workspace_id, jti)` only after JWT and Store-state validation.
- Passed that identity through `AuthorizationContext` into HTTP initialize and Runtime creation.
- Added frozen `WorkspaceBinding` and a Catalog-backed resolver for OAuth, static bearer, noauth, and stdio.
- Constructed every HTTP Runtime from its bound root, including project instructions and all path/cwd/process/output state.
- Required subsequent POST and DELETE requests to match the Session authorization method, Client, Grant, and Workspace.
- Added validated `oauth_client_workspace_bindings` settings and `CODING_TOOLS_MCP_OAUTH_WORKSPACE_ID` for a preregistered Client.
- Preserved one-Workspace compatibility by binding old unbound Clients only when exactly one enabled Workspace exists.
- Replaced the Phase 06 contract skip with executable isolation tests.

## Session and Workspace Semantics

- OAuth Workspace selection occurs at HTTP initialize/runtime-factory creation.
- A Runtime binding is immutable until the MCP Session closes.
- Ordinary tools cannot switch Workspace roots.
- `read_file`, path resolution, `default_cwd`, process sessions, retained output, and project instructions use the bound Workspace.
- Different Sessions own distinct Runtime objects, cwd values, process dictionaries, retained-output dictionaries, and project contexts.
- Reusing Session A with Agent B's bearer identity returns HTTP 403. DELETE/session termination has the same check.
- Missing, unknown, disabled, or unbound OAuth mappings fail closed.
- Disabling a Workspace affects future resolutions. Existing Sessions retain their frozen Runtime/root until closed; new Sessions return 503.
- stdio resolves the Catalog default and carries no fabricated OAuth identity.

## Mapping and Migration Decisions

- `oauth_clients.workspace_id` controls future Grant creation.
- `oauth_grants.workspace_id` is copied at Grant creation and is not rewritten when the Client mapping changes.
- A Client without a Workspace mapping cannot create an Authorization Grant.
- With exactly one enabled Workspace, startup migrates old unbound Clients to that sole default.
- With multiple enabled Workspaces, DCR Clients remain unbound until an explicit mapping exists.
- `oauth_client_workspace_bindings` is a restart field and must reference enabled Catalog entries.
- Unknown Client IDs in persisted mappings fail startup instead of being ignored.

## Files Changed from Phase 05

- `coding_tools_mcp/oauth.py`
- `coding_tools_mcp/oauth_store.py`
- `coding_tools_mcp/server.py`
- `coding_tools_mcp/settings_definition.py`
- `coding_tools_mcp/transport_http.py`
- `coding_tools_mcp/workspace_binding.py`
- `docs/integration-contract-v0.2.2.md`
- `tests/test_integration_contract_v022.py`
- `tests/test_oauth_integration.py`
- `tests/test_oauth_store.py`
- `tests/test_settings_foundation.py`
- `tests/test_workspace_session_binding.py`

No Admin, WebUI, desktop, tool-registry, Refresh Token issuance transaction, or Phase 07 Gateway code was changed.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.test_oauth_store tests.test_oauth_integration tests.test_oauth_refresh tests.test_oauth_signing tests.test_oauth_fail_closed tests.compliance.test_oauth_persistence tests.test_workspace_session_binding tests.test_settings_foundation tests.test_integration_contract_v022` | 0 | Ran 51 tests; all passed; no Phase 06 skip remains. |
| `.\.venv\Scripts\python.exe -m unittest -v tests.test_workspace_session_binding` | 0 | Four production-composition/session tests passed, including two OAuth Workspaces, cwd/path/project isolation, 403 context mismatch, DELETE protection, and disabled-Workspace behavior. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite runtime-semantics` | 0 | Windows runner exited 0; four bodies skipped by the known `/dev/null` preflight. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite security` | 0 | Windows runner exited 0; 15 bodies skipped by the same preflight. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite e2e` | 0 | Windows runner exited 0; six bodies skipped by the same preflight. |
| Ruff over all changed Phase 06 source/tests | 0 | All checks passed. |
| `py_compile` over changed Runtime/Workspace source/tests | 0 | All modules compiled. |
| Mypy over `workspace_binding.py`, `settings_definition.py`, and `transport_http.py` | 0 | No issues found. |
| Mypy over `server.py` with documented upstream baseline error codes disabled | 0 | No additional issues found. |
| high-risk credential scan | 0 | No PAT, private-key, long bearer, or JWT pattern found. |
| SQL scan over the Phase 06 `server.py` diff | 0 | No SQL in handlers or Runtime composition. |
| `TOOL_REGISTRY` diff scan | 0 | No tool-catalog changes. |

## Known Platform Baseline

- Native Windows compliance fixtures still skip when their shared preflight requires `/dev/null`.
- The new Phase 06 HTTP isolation suite executes real requests on Windows and passed independently.
- Final Linux CI must execute runtime-semantics, security, and e2e bodies before release.

## Remaining Risks

- Workspace mappings currently come from server settings or the preregistered Client environment variable; no Admin/WebUI endpoint was added.
- Existing Sessions intentionally retain a frozen Workspace after a Catalog disable operation; operators must terminate them for immediate eviction.
- Phase 05's Refresh rotation/access-token insertion cross-transaction availability risk remains unchanged.
- The Server Secret Vault format still requires the separate Phase 03 security review.

## Phase 07 Preconditions

- Start only from a clean worktree with Phase 06 marked complete.
- Preserve immutable per-Session Workspace binding and authorization-context matching.
- Gateway routing must not merge cwd, process sessions, retained output, or project context across child runtimes.
- Do not add ordinary Workspace-switching tools or infer mappings from OAuth presentation metadata.

## Secret Check

- No real credentials, OAuth databases, Vault files, bearer tokens, refresh tokens, or signing secrets were committed.
- All test credentials and token material are synthetic and confined to test execution.
