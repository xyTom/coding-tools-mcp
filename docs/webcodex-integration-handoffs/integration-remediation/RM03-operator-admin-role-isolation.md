# RM03 Handoff — Operator/Admin role isolation

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `ec4668bf0910530dd8cdf502f1ecc0d7295680c7`
- Implementation commit: `d442b29`

## Defect Closed

- An OAuth request whose validator returned a valid `OAuthIdentity` could use a Bearer credential equal to the configured Admin token and become an Operator principal.
- `_operator_principal()` now extracts the actual Bearer credential before the authorization-method branches and compares it with the configured Admin token using `secrets.compare_digest()`.
- An Admin-token collision returns `None` for every authorization method, so the Operator route responds with 401. Admin routes retain their separate Admin-token authorization path.

## Files Changed

- `coding_tools_mcp/server.py`: moved the Admin-token collision guard before the OAuth/static-bearer/no-auth principal branches.
- `tests/test_operator_api.py`: added a real loopback HTTP regression covering OAuth/Admin collision and ordinary OAuth workspace scoping; extended the test server helper to configure OAuth.

## Invariants Preserved

- Admin token is not accepted as an Operator principal: yes
- OAuth identity workspace isolation: yes
- Ordinary static bearer default-workspace behavior: yes
- Admin route authorization: unchanged and covered by existing compliance tests
- Token value logged, returned, or included in error details: no
- Real credential, OAuth database, or external service used: no

## Validation

| Command | Exit code | Result |
| --- | ---: | --- |
| `python -m unittest tests.test_operator_api.OperatorHTTPAuthenticationTests.test_oauth_identity_cannot_use_shared_admin_bearer_and_is_workspace_scoped` before fix | 1 | Expected failure: collision request incorrectly returned HTTP 200. |
| Same targeted regression after fix | 0 | 1 passed; collision returned 401 and ordinary OAuth returned 200 scoped to `ws-other`. |
| `python -m unittest tests.test_operator_api tests.test_oauth_integration tests.compliance.test_mcp_admin` | 0 | 37 tests passed. |
| `git diff --check` | 0 | Passed. |

## Remaining Risks

- RM04–RM13 remain open; Phase 18/22 must remain `in remediation`.
- No real OAuth provider or production token was used; validator behavior was deterministically mocked at the HTTP seam.

## Next Card Preconditions

- Start RM04 from implementation/handoff state after this RM03 handoff commit.
- Keep the execution book untracked and modify only the RM04 allowlist.
