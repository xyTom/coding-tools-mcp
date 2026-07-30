# Phase 05 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `1aae212`
- Final implementation HEAD before handoff: `24c50a6`
- Handoff commit: filled by the next agent from `git log`
- Phase 06 remains pending and was not started.

## Ordered Integration Commits

1. `6297e4a feat(oauth): add persistent client registry adapter`
2. `f2dbcd4 feat(oauth): persist dynamic client registration`
3. `394a686 feat(oauth): persist authorization grants`
4. `c01f4d0 feat(oauth): persist access-token revocation state`
5. `5032d85 feat(oauth): fail closed on bearer state lookup`
6. `3fe588e feat(oauth): restore refresh-token rotation`
7. `3837bbe feat(oauth): restore signing-key lifecycle`
8. `10a727c test(oauth): add persistence compliance coverage`
9. `4779034 test(oauth): stabilize sqlite cleanup on Windows`
10. `24c50a6 fix(oauth): reject requests when persistence is unavailable`

## Scope Completed

- Added a Store-backed `OAuthClientRegistry` adapter without placing SQL in HTTP handlers.
- Persisted preregistered and dynamically registered public/confidential clients.
- Persisted Authorization Grants before authorization codes are issued.
- Added tracked JWT identity with `jti`, `kid`, `client_id`, and `grant_id`.
- Bound bearer validation to active Client, Grant, Access Token, and Signing Key state.
- Implemented Refresh Token grant, rotation, restart persistence, and reuse-family revocation.
- Integrated active/retired/revoked Signing Key lifecycle with Vault-only secret material.
- Required persistent Store and Secret Vault for OAuth startup and request processing.
- Updated OAuth metadata, DCR narrowing, token dispatch, and the integration contract to use the shared grant/response constants.

## Protocol and Security Decisions

- `OAUTH_GRANT_TYPES_SUPPORTED` now contains `authorization_code` and `refresh_token` only after both endpoint branches and focused tests exist.
- `OAUTH_RESPONSE_TYPES_SUPPORTED` remains `code` only.
- Redirect URI matching remains exact.
- PKCE remains mandatory S256 with the upstream verifier/challenge validation.
- Client authentication remains bound to the registered `none`, `client_secret_post`, or `client_secret_basic` method.
- DCR returns a confidential client secret only once; SQLite stores only its digest.
- Access tokens are never persisted; only `jti` and non-secret metadata are stored.
- Refresh tokens are stored only as peppered HMAC-SHA256 digests.
- Signing secrets are stored only in Secret Vault. SQLite stores per-key Vault references and fingerprints.
- Store/Vault failure rejects startup or returns an explicit fail-closed response. No in-memory fallback is used.
- OAuth handlers call registry/service functions and contain no SQL statements.

## Files Changed from Phase 04

- `coding_tools_mcp/oauth.py`
- `coding_tools_mcp/server.py` (OAuth composition and OAuth handler boundaries only)
- `docs/integration-contract-v0.2.2.md`
- `tests/compliance/test_mcp_contract.py`
- `tests/compliance/test_oauth_persistence.py`
- `tests/test_integration_contract_v022.py`
- `tests/test_oauth_fail_closed.py`
- `tests/test_oauth_integration.py`
- `tests/test_oauth_refresh.py`
- `tests/test_oauth_signing.py`
- `tests/test_oauth_store.py` (Windows SQLite cleanup stabilization only)

No Admin, WebUI, desktop, tool-registry, workspace-binding, or transport architecture files were changed.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.test_oauth_fail_closed tests.test_oauth_signing tests.test_oauth_refresh tests.test_oauth_integration tests.test_oauth_store tests.test_integration_contract_v022 tests.compliance.test_oauth_persistence` | 0 | Ran 31 tests: 30 passed; only the Phase 06 contract test remains skipped. |
| `.\.venv\Scripts\python.exe -m unittest tests.compliance.test_oauth_persistence` | 0 | One integrated restart/refresh/key-revocation compliance test passed. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite mcp-contract` | 0 | Native Windows runner exited 0; 37 bodies skipped by the known upstream `/dev/null` preflight. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite security` | 0 | Native Windows runner exited 0; 15 bodies skipped by the same known preflight. |
| Ruff over changed OAuth source/tests | 0 | All checks passed. |
| `py_compile` over changed OAuth source/tests | 0 | All modules compiled. |
| Mypy over `oauth.py` and `oauth_store.py` | 0 | No issues found. |
| `git diff --check` / staged checks for every commit | 0 | No whitespace errors. |
| high-risk credential scan over all Phase 05 changed files | 0 | No PAT, private-key, long bearer, or JWT pattern found. |
| SQL keyword scan over the Phase 05 `server.py` diff | 0 | No handler/composition SQL found. |

`uv run --frozen` was also used for the complete OAuth suite. It did not change `uv.lock`; UV rebuilt the editable package in the environment and the final frozen suite passed after Windows SQLite cleanup stabilization.

## Known Platform Baseline

- Native Windows `mcp-contract` and `security` compliance bodies remain skipped because the upstream fixture requires `/dev/null`.
- This is the Phase 01 platform baseline and is not a Phase 05 regression.
- Final integrated Linux CI must execute those bodies before release.

## Remaining Risks at Phase 05 Completion

- Authorization codes remain intentionally short-lived and process-local.
- Signing-key rotation/revocation is integrated as a service but has no Admin or WebUI endpoint in this phase.
- At the original Phase 05 handoff, Refresh rotation and access-token metadata insertion were individually transactional but not one cross-operation SQLite transaction. This historical risk was resolved after Phase 12 by the supplemental Refresh atomicity fix: rotation, replacement, access-token metadata, family updates, and audits now commit or roll back together.
- The OAuth Server Secret Vault cryptographic format still requires the dedicated security review already recorded in Phase 03.

## Phase 06 Preconditions

- Phase 06 must focus only on immutable Agent-to-Workspace binding at HTTP initialize/runtime-factory creation.
- Do not add ordinary MCP tools for switching Workspace.
- Do not change the OAuth grant, refresh, key, or Store behavior completed here unless a Phase 06 binding test exposes a direct integration defect.
- Invalid, unknown, disabled, or mismatched Workspace mapping must fail closed.

## Secret Check

- No real credentials, OAuth databases, Vault files, bearer tokens, refresh tokens, or signing secrets were committed.
- All committed test credentials are synthetic and are not persisted as plaintext artifacts.
