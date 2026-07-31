# Phase 04 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Implementation commit: `16032c41fb0032c164d648927f48c553b47b1c7b`
- Handoff commit: filled by next agent from git log
- Started from: `1f30376c520ae0ad8308f1dfbbb28e5648d2f5f2`

## Scope Completed

- Ported a standalone SQLite OAuth authorization store without connecting it to
  `oauth.py`, `server.py`, Runtime, HTTP endpoints, Admin, or WebUI.
- Added metadata tables for Clients, Grants, Access Tokens, Refresh Token
  Families, Refresh Tokens, Signing Keys, and Audit Events.
- Added forward-only schema migrations through version 3 with explicit SQLite
  transactions, repeatable reopen behavior, and rollback on migration failure.
- Persisted the Client metadata required by the upstream registry adapter in
  Phase 05: multiple exact redirect URIs, token endpoint authentication method,
  and an optional client-secret SHA-256 digest.
- Added focused Store tests and replaced the Phase 04 contract-test skip with an
  actual reopen and idempotent-migration test.

## Files Changed

- `coding_tools_mcp/oauth_store.py`: adds standalone transactional OAuth
  metadata persistence, revocation, signing-key lifecycle, refresh-token
  rotation and reuse detection, and audit records.
- `tests/test_oauth_store.py`: adds focused schema, lifecycle, secret-boundary,
  concurrency, reopen, migration, replay, and failure-rollback tests.
- `tests/test_integration_contract_v022.py`: removes the Phase 04 skip and binds
  the contract to an actual persistent Store reopen.
- `docs/integration-handoffs/STATUS.md`: marks Phase 04 complete.
- `docs/integration-handoffs/phase-04.md`: records this handoff.

## Decisions Applied

- The Store contains authorization metadata only. It never stores an access
  token, bearer token, refresh token, client secret, or signing secret in
  plaintext.
- Access tokens are represented by their `jti`; refresh tokens are represented
  only by HMAC-SHA256 digests keyed with the supplied server-side pepper.
- Signing keys contain `kid`, lifecycle state, fingerprint, algorithm, and a
  Secret Vault reference only; key material remains outside SQLite.
- Client secret authentication persists only a SHA-256 digest. Public clients
  reject a digest, while confidential clients require one.
- Schema migrations execute inside `BEGIN IMMEDIATE` transactions. A failed
  migration leaves both schema and `PRAGMA user_version` unchanged.
- Refresh rotation executes under an immediate transaction. Reuse of a replaced
  token revokes the whole family and records one high-severity audit event.
- Client, Grant, Access Token, Refresh Family, and Signing Key revocations are
  idempotent and preserve their original timestamp and reason.
- Phase 04 does not change the upstream OAuth protocol, registry behavior,
  advertised grant types, token format, bearer validation, or HTTP handlers.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.test_oauth_store tests.test_integration_contract_v022` | 0 | Ran 21 tests: 20 passed; only the Phase 06 Workspace-binding test remains explicitly skipped. |
| `.\.venv\Scripts\python.exe -m ruff check --ignore=E501 coding_tools_mcp/oauth_store.py tests/test_oauth_store.py tests/test_integration_contract_v022.py` | 0 | All checks passed. |
| `.\.venv\Scripts\python.exe -m mypy --python-version 3.11 coding_tools_mcp/oauth_store.py` | 0 | No issues found. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite security` | 0 | Runner exited 0; 15 tests skipped by the native-Windows `/dev/null` fixture preflight, matching the documented Phase 01 baseline. |
| `git diff --check` and `git diff --cached --check` | 0 | No whitespace errors. |
| exact forbidden-import scan over Store and focused tests | 0 | No dependency on Runtime, `server.py`, Admin, WebUI, or HTTP server classes. |
| high-risk credential pattern scan over Store and focused tests | 0 | No GitHub PAT, private-key marker, bearer/JWT canary, or real credential pattern found. |

## Known Baseline Failures

- Native Windows security compliance bodies remain skipped by the upstream
  `/dev/null` fixture preflight. This is the Phase 01 platform baseline and is
  unrelated to the standalone SQLite Store.

## Remaining Risks

- The Store is deliberately not used by the current OAuth protocol or bearer
  validation. Until Phase 05, upstream OAuth behavior remains process-local.
- Phase 05 must adapt the upstream `OAuthClientRegistry` rather than exposing SQL
  to handlers. It must preserve exact redirect binding, DCR narrowing, PKCE,
  client authentication methods, and the v0.2.2 token TTL limits.
- Refresh grant advertisement must remain unchanged until the token endpoint
  branch is implemented and tested in Phase 05.

## Next Phase Preconditions

- Start from this handoff commit with a clean integration worktree.
- Read only the Phase 05 plan, this handoff, `oauth.py`, `oauth_store.py`,
  `secret_vault.py`, and the explicitly listed OAuth handler symbols and tests.
- Connect the Store through registry/config adapters; do not scatter SQL across
  `server.py` handlers.
- Store unavailability must fail closed and must not silently fall back to a
  permissive process-local authorization decision.
- Preserve the current advertised constants until the matching refresh-token
  endpoint branch exists and passes focused tests.

## Secret Check

- No credentials, OAuth database files, bearer tokens, refresh tokens, client
  secrets, signing secrets, or real Secret Vault files were added.
- Test credentials are generated in temporary directories and are checked not
  to appear in SQLite database, WAL, SHM, audit, or returned metadata bytes.
