# Phase 05 Supplemental Handoff: Atomic Refresh Exchange

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Implementation commit: `31add858fd035adec9415f6e00dfcfa71bf9257b`
- Started from: `3b71183`
- Scope: resolve the Phase 05 Refresh rotation/access-token metadata cross-transaction availability risk only
- Phase 13: not started

## Risk Resolved

The original Refresh Token exchange committed two independent SQLite transactions:

1. rotate the Refresh Token, create its replacement, consume the old token, and update the family;
2. persist the newly signed Access Token `jti` metadata.

If step 1 committed and step 2 failed, the endpoint returned no bearer token but the Client had already lost its usable Refresh Token and had to reauthorize.

The exchange now prepares the JWT without exposing it, then commits all persistent exchange state in one `BEGIN IMMEDIATE` transaction:

- replacement Refresh Token digest;
- old Refresh Token `used_at`, `revoked_at`, and replacement linkage;
- Refresh family `last_used_at`;
- Access Token `jti`, Client, Grant, signing-key, scope, mode, issue, and expiry metadata;
- `refresh_token_rotated` audit;
- `access_token_issued` audit.

Any database or audit failure rolls back the complete transaction. No replacement token or Access Token metadata remains, and the original Refresh Token is still usable for a later retry.

## Implementation Details

### Store

`coding_tools_mcp/oauth_store.py` now provides:

- `RefreshTokenBinding` for a read-only Client/Grant/scope lookup before JWT preparation;
- `RefreshTokenClientMismatchError` for a bound-client mismatch before mutation;
- `_record_access_token_in_transaction()` shared by ordinary Access Token issuance and Refresh exchange;
- `_refresh_token_row()` and `_rotate_refresh_token_in_transaction()` shared by the existing rotation API and the atomic exchange API;
- `rotate_refresh_token_and_record_access_token()` as the single transaction boundary for Refresh exchange.

The existing standalone `rotate_refresh_token()` behavior remains available and keeps its reuse-family revocation semantics.

### OAuth service

`coding_tools_mcp/oauth.py` now separates JWT preparation from persistence through `AccessTokenIssue` and `_prepare_access_token()`.

- Authorization Code issuance still uses `create_access_token()` and its normal Access Token transaction.
- Refresh exchange prepares the JWT, then calls the atomic Store exchange.
- A JWT prepared for a failed transaction is neither returned nor persisted.
- Client binding is checked before rotation, so an authenticated wrong Client cannot consume a valid Refresh Token.
- The HTTP fail-closed response remains the existing `503` OAuth `server_error` contract.

## Security and Concurrency Semantics

- Refresh Token plaintext is still never stored; only the peppered digest is persisted.
- Access Token plaintext is still never stored; only `jti` metadata is persisted.
- The OAuth Store schema remains version `4`; no migration or new column was required.
- A successful exchange consumes the old token exactly once.
- Replay of an already committed old token still revokes the Refresh family.
- Concurrent exchange behavior retains the existing reuse-detection policy.
- Store failure has no in-memory fallback.

## Tests Added

- Store failure injection at the `access_token_issued` audit verifies complete rollback:
  - original token remains unused and unrevoked;
  - no replacement row remains;
  - no Access Token metadata remains;
  - neither issuance audit remains;
  - retrying the same original token succeeds after the failure is removed.
- Wrong-client failure verifies no token consumption or Access Token metadata.
- HTTP failure injection verifies:
  - first exchange returns HTTP `503` / `server_error`;
  - the same original Refresh Token succeeds with HTTP `200` after recovery;
  - later replay retains the existing `invalid_grant` and family-revocation behavior.
- Required-doc coverage now locks the statement that the original Refresh Token remains retryable after a failed atomic exchange.

## Validation

| Command / check | Result |
| --- | --- |
| focused rollback, wrong-client, restart/reuse HTTP tests | 3 tests passed |
| Phase 03–06 OAuth/Workspace/integration regression | 43 tests passed |
| final OAuth + Workspace + contract + docs/schema regression | 57 tests passed |
| Ruff over changed source/tests | passed |
| `py_compile` over changed source/tests | passed |
| Mypy over `oauth.py` and `oauth_store.py` | no issues |
| `docs-required` | 6 tests passed |
| `schema-drift` | 8 tests passed |
| OAuth schema-diff scan | 0 matches; schema remains v4 |
| unrelated Workspace/Gateway/Admin/WebUI scope scan | 0 files |
| high-risk credential scan | 0 matches |
| `git diff --check` | passed |

## Files Changed

- `coding_tools_mcp/oauth.py`
- `coding_tools_mcp/oauth_store.py`
- `tests/test_oauth_store.py`
- `tests/test_oauth_refresh.py`
- `tests/compliance/test_docs_required.py`
- `CHANGELOG.md`
- `docs/integration-contract-v0.2.2.md`
- `docs/runtime-contract-v0.2.md`
- `docs/migration-v0.1-to-v0.2.2.md`
- Phase 05–12 historical handoffs, updated to distinguish their original state from the later resolution

## Not Changed

- OAuth SQLite schema or migration framework
- Authorization Code process-local lifecycle
- signing-key lifecycle
- Workspace binding or Session authorization context
- Gateway, Admin API, Admin WebUI, Desktop, npm launcher, or tool registry
- Phase 13 or Phase 14

## Remaining Risks

- Authorization Codes remain intentionally short-lived and process-local.
- The OAuth and server Secret Vault cryptographic formats still require the dedicated security review recorded by earlier phases.
- Full release validation still requires the Linux gate matrix described by the integration plan.

## Next Phase Preconditions

- Start Phase 13 only from a clean worktree after this supplemental handoff is committed.
- Preserve the atomic Refresh exchange invariant during fault injection and concurrency review.
- Do not split replacement, family update, Access Token metadata, or issuance audits across independent transactions again.
