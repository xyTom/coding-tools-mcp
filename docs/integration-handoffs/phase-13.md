# Phase 13 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `cab3cb9ecf696b545be0d8ed2ebfd18bacb72340`
- Implementation commits:
  - `89b967f` — `fix(runtime): harden Windows process handling`
  - `6cd8982` — `test(runtime): make helper regressions portable on Windows`
  - `37f7146` — `test(release): keep integration tree unreleased before phase 14`
- Handoff commit: filled by the next agent from `git log`
- Phase 14 remains pending and was not started.

## Scope

Phase 13 added no product capability. It executed the full regression, packaging,
manual-smoke, and security-audit plan, then fixed only defects exposed by those
gates.

The Phase 05 supplemental invariant remained mandatory throughout validation:
Refresh rotation, replacement, access-token `jti` metadata, family updates, and
issuance audits commit in one SQLite transaction. A failed exchange leaves the
original Refresh Token retryable.

## Defects Fixed

### Windows process-tree termination

The full Windows suite exposed that `shell=True` launches through `cmd.exe`.
Force-killing only the `Popen` wrapper could leave a child process alive and
holding the Workspace current directory, even though the Runtime reported the
wrapper as killed.

The Runtime now:

- preserves the existing graceful `CTRL_BREAK_EVENT` attempt;
- uses `taskkill /PID <pid> /T /F` for Windows forced termination;
- waits for the wrapper handle after successful tree termination;
- falls back to the previous single-process termination path if `taskkill` is
  unavailable or fails.

The real Windows force-kill smoke was run three consecutive times and each run
released the temporary Workspace successfully.

### Windows static typing portability

The Windows typeshed does not expose POSIX-only `os.openpty` or `os.O_CLOEXEC`.
Runtime capability checks now use:

- `getattr(os, "openpty", None)` for POSIX PTY creation;
- `getattr(os, "O_CLOEXEC", 0)` for Landlock path descriptors.

POSIX behavior and the Windows `TTY_UNSUPPORTED` contract are unchanged.

### Cross-platform Runtime regression tests

Runtime helper tests now use the current Python interpreter for portable sleep,
cwd, stdout/stderr, and truncation fixtures instead of assuming POSIX commands
such as `sleep`, `pwd`, shell heredocs, or POSIX `printf` exist on Windows.
Coverage was preserved for:

- active process limits;
- running-session next actions;
- independent stdout/stderr paging;
- output truncation and continuation;
- command-environment filtering;
- Git helper environment behavior;
- default cwd and Workspace path boundaries;
- heredoc command-policy scanning;
- Windows process termination.

### Release-state validation

The strict release validator continues to reject a CHANGELOG containing
`## Unreleased`. The current integration-tree test now explicitly expects that
rejection because Phase 14 owns version selection and release metadata. Phase 13
did not change package versions, remove `Unreleased`, tag, publish, or release.

## Python Gates

### Static analysis

| Gate | Result |
| --- | --- |
| Full Ruff command from the Phase 13 plan | exit 0 |
| Full Mypy command from the Phase 13 plan | exit 0; 31 source files |
| `git diff --check` | exit 0 |

### Full unittest discovery

Executed in an isolated Windows test environment with explicit HOME, APPDATA,
LOCALAPPDATA, SystemRoot, WinDir, and ComSpec so the runner did not fail before
reaching product code.

```text
Ran 311 tests in 102.769s
OK (skipped=83)
```

Skip classification:

- 72 tests are the documented native-Windows Phase 01 `/dev/null` compliance
  fixture baseline:
  - MCP contract: 37
  - tool golden: 9
  - security: 15
  - runtime semantics: 4
  - e2e: 6
  - dogfood: 1
- The remaining 11 are platform/optional tests for POSIX signal, PTY, Landlock,
  or an MSVC environment that was not initialized in this shell.
- No new or unexplained skip class was introduced.

### Compliance runners

| Suite | Result |
| --- | --- |
| `mcp-contract` | exit 0; 37 skipped by Windows `/dev/null` preflight |
| `tool-golden` | exit 0; 9 skipped by Windows `/dev/null` preflight |
| `security` | exit 0; 15 skipped by Windows `/dev/null` preflight |
| `runtime-semantics` | exit 0; 4 skipped by Windows `/dev/null` preflight |
| `e2e` | exit 0; 6 skipped by Windows `/dev/null` preflight |
| `dogfood` | exit 0; 1 skipped by Windows `/dev/null` preflight |
| `docs-required` | 6/6 passed |
| `schema-drift` | 8/8 passed |

The first six suites still require final Linux CI execution of their test bodies;
Phase 13 does not misreport those Windows skips as executed coverage.

## JavaScript and Packaging Gates

| Gate | Result |
| --- | --- |
| `npm --prefix webui test` | 15/15 passed |
| `npm --prefix webui run build` | generated the self-contained Admin page |
| `npm --prefix npm/coding-tools-mcp test` | 4/4 passed |
| npm launcher pack dry-run | exit 0 |

The npm dry-run package remained version `0.1.0`, pending Phase 14 policy, and
contained only:

```text
LICENSE
README.md
bin/coding-tools-mcp.js
package.json
```

No tarball was published and no release operation was performed. Rebuilding the
WebUI left the committed source/dist state clean.

## Manual Acceptance Matrix

1. **stdio initialize + read-only call:** real stdio subprocess initialized with
   protocol `2025-11-25`, exposed 20 fixed local tools, and returned
   `stdio-ok\n` from `read_file`.
2. **HTTP initialize / Session / DELETE boundary:** covered by the Workspace
   Session integration and HTTP tests; cross-identity requests and DELETE were
   rejected.
3. **Two-Workspace isolation:** OAuth Sessions retained immutable, separate cwd,
   process, output, path, and Workspace state.
4. **DCR + PKCE:** registration, authorization redirect, code exchange, and
   persistent rebuild path passed.
5. **Client/Grant/Signing Key lifecycle:** disable, revoke, rotation, retirement,
   and fail-closed validation passed.
6. **Refresh rotation and replay rejection:** atomic failure injection returned
   HTTP 503, the same original token then retried successfully, and later replay
   revoked/rejected the family.
7. **Admin login / Settings / pending restart:** dedicated Admin authentication,
   revision conflicts, active/persisted separation, and restart-only Gateway
   saves passed.
8. **WebUI Workspace editing:** source/dist, DOM safety, labels, stale revision,
   exact ID actions, and draft preservation passed.
9. **Upstream Gateway:** namespace, schema/result preservation, structured errors,
   immutable snapshots, allowlists, and separate Runtime state passed.
10. **Desktop profile/runtime safety:** profile storage, identity persistence,
    environment-only bearer transfer, tunnel cleanup, and PID-reuse protection
    passed with synthetic test data only.

The focused acceptance modules ran 47 tests and all passed, in addition to the
real stdio smoke.

## Security Audit

- High-risk token/private-key/AWS credential pattern matches in the integration
  diff: 0.
- Committed OAuth DB, transcript DB, Vault, WAL/SHM, or secret JSON artifacts: 0.
- Remote URLs containing userinfo credentials: 0.
- Live `tool_profile` control paths: 0. The six source matches are only the
  explicit migration path that removes the legacy key and emits
  `legacy_tool_profile_ignored`.
- Gateway dynamic lifecycle operations: 0. The two matches are Admin responses
  declaring `dynamic_reload: false`.
- Telemetry privacy/schema tests: all 16 passed; payloads contain no paths,
  Workspace/Agent identities, commands, content, transcripts, or model output.
- Ordinary MCP/OAuth bearer credentials do not grant Admin authority.
- fake readonly remains an annotation compatibility override, not a security
  control.
- Admin destructive APIs remain dedicated-Admin authenticated and ID-scoped.

Git integrity:

- `git fsck` exited 0 with no corrupt object reports.
- Unreachable objects remain from the integration/WIP/checkpoint history and were
  not pruned during Phase 13.
- `wip/pre-upstream-v0.2.2` exists.
- `backup/local-before-v0.2.2` exists.

## Final Focused Invariants

The final committed-HEAD focused run covered Refresh rollback/retry, Client
mismatch non-consumption, Refresh replay, Windows process-tree cleanup, release
metadata strictness, and telemetry privacy:

```text
Ran 27 tests
OK
```

## Phase 14 Preconditions

- Start from the clean Phase 13 handoff HEAD.
- Run the skipped compliance bodies in Linux CI before release approval.
- Phase 14 must obtain explicit version policy confirmation before changing
  Python/npm/Desktop versions or replacing `Unreleased` with a release heading.
- Do not weaken strict release metadata checks to make the current integration
  tree appear releasable.
- Preserve the atomic Refresh exchange invariant and Windows process-tree kill
  regression.
- Do not delete WIP/backup refs until the release audit and rollback decision are
  complete.

## Secret Check

No real bearer token, Refresh Token, Client secret, Admin token, Vault key,
signing key, private key, OAuth database, transcript, or private Workspace content
was committed. Test credentials and identifiers are synthetic.
