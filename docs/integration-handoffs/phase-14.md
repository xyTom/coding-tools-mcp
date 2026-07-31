# Phase 14 Handoff

## Status

- Result: complete
- Final branch: `main`
- Started from: `cd51ee0297c6e14c2df8eacd844aaea28efff933`
- Validated integration candidate: `6551ff96f23c58c2d4f0c899f9338cd7cbd43772`
- Main merge commit before this handoff: `463b0a5e29b090dead1610be7377744091e7b70d`
- Preparation commits:
  - `a55d216` - `build(deps): refresh integration lock metadata`
  - `ab2c931` - `ci(compliance): allow setup-node under Landlock`
  - `0f31303` - `test(compliance): stabilize Windows release regressions`
  - `8e5a375` - `chore(release): prepare 0.3.0.dev0 candidate`
- Handoff commit: resolve from `git log -1`.
- Version, telemetry, push, and main-merge decisions are resolved.

## Resolved Decisions

### Version

The approved fork development release is:

| Surface | Value |
| --- | --- |
| Python package | `0.3.0.dev0` |
| `coding_tools_mcp.__version__` | `0.3.0.dev0` |
| npm launcher | `0.3.0-dev.0` |
| Git tag expected by the validator | `v0.3.0.dev0` |
| Desktop client | bundled in the Python distribution; no independent version field |
| CHANGELOG heading | `0.3.0.dev0 - 2026-07-31` |

The integration baseline remains upstream `0.2.2`; `0.3.0.dev0` is the fork's
first integrated development release.

The release validator accepts only the exact development mapping:

```text
Python X.Y.Z.devN <-> npm X.Y.Z-dev.N
```

Stable Python versions retain the previous rule that rejects prerelease npm
versions.

### Telemetry

Telemetry keeps the upstream default and the already documented privacy schema.
No production telemetry code, event, field, endpoint, or default was changed in
Phase 14.

## Local Release Candidate Preparation

The following local-only release metadata is complete:

- Python, module, npm, lock, CHANGELOG, documentation, and integration-contract
  versions are aligned.
- `## Unreleased` was replaced by the dated development-release heading.
- npm prereleases publish under the `next` dist-tag; stable launchers continue to
  use `latest`.
- `.devN` GitHub releases are marked as prereleases.
- The npm `0.3.0-dev.0` launcher automatically pins Python `0.3.0.dev0` by
  default; `CODING_TOOLS_MCP_VERSION` remains an explicit override.
- Stable npm launchers remain unpinned by default so later server-only releases
  can be discovered without republishing the launcher.
- The release workflow's npm publish job directly depends on the validation job,
  so the validated dist-tag output is available at publish time.

## Earlier Phase 14 Preparation

Two pre-authorization release defects were fixed before version selection:

1. `uv.lock` drift:
   - local package version corrected from `0.2.0` to `0.2.2` at that stage;
   - missing `PyYAML>=6.0` development dependency restored.
2. GitHub Actions setup-node/Landlock conflict:
   - the workflow resolves the setup-node installation root;
   - it exports that root through `CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS` before
     compliance execution;
   - workflow-order regression coverage prevents the setup from moving after the
     test gates.

The lock now correctly records the final local package version `0.3.0.dev0`.

## Linux Validation

The exact committed Runtime source at `ab2c931` was exported into a temporary
WSL2 Ubuntu filesystem and tested with CPython 3.11.15 and Node 22.23.2.

All 72 compliance bodies skipped by the native Windows `/dev/null` baseline were
executed under the Linux kernel:

| Suite | Result |
| --- | --- |
| `mcp-contract` | 37/37 passed |
| `tool-golden` | 9/9 passed |
| `security` | 15/15 passed |
| `runtime-semantics` | 4/4 passed |
| `e2e` | 6/6 passed |
| `dogfood` | 1/1 passed |
| **Total** | **72/72 passed** |

The commits after `ab2c931` change release metadata, packaging workflow,
launcher behavior, documentation, and tests. They do not change MCP Runtime,
OAuth, Workspace, Gateway, Admin, chat, Desktop, or telemetry production logic,
apart from the package `__version__` constant.

This is real Linux execution, but not a GitHub-hosted Actions result.
Authoritative fork CI remains unavailable until the branch is pushed.

## Windows Validation

The final local worktree was run with isolated HOME, USERPROFILE, APPDATA,
LOCALAPPDATA, TEMP, and TMP directories:

```text
Ran 315 tests in 73.360s
OK (skipped=83)
```

The 83 skips retain the Phase 13 classification. The 72 compliance tests among
them have the separate successful Linux execution above.

Static and focused gates:

| Gate | Result |
| --- | --- |
| full Ruff | passed |
| full Mypy | passed; 32 source files |
| `uv lock --check` | passed |
| release validator for `v0.3.0.dev0` | passed |
| release/packaging/contract/docs/schema focused run | 41/41 passed |
| WebUI model and DOM tests | 15/15 passed |
| final security invariant run | 49/49 passed |
| Chat Persistence module | 15/15 passed |
| npm launcher | 5/5 passed |
| `git diff --check` | passed |
| release workflow YAML parse | passed |

Windows test stabilization remains assertion-preserving:

- Admin temporary-directory cleanup retries only Windows sharing/access cleanup
  failures.
- OAuth bearer fail-closed test retries only transient connection abort/reset;
  status and token-redaction assertions are unchanged.
- Conversation list tests now assert the implemented summary contract: bounded
  preview is allowed, while message/context collections and full content remain
  absent until the detail call.

## Artifact Validation

### Python

Local build produced and then removed:

```text
coding_tools_mcp-0.3.0.dev0-py3-none-any.whl
coding_tools_mcp-0.3.0.dev0.tar.gz
```

The wheel was inspected and installed into a fresh Python 3.11 environment.
Validation confirmed:

- installed `coding_tools_mcp.__version__ == 0.3.0.dev0`;
- `coding-tools-mcp --help` succeeds;
- both server and Desktop entry points are present;
- Admin WebUI package data is present;
- Desktop Python modules and Simplified Chinese `.qm`/`.ts` locale files are
  present.

### npm

The final dry-run package is:

```text
coding-tools-mcp@0.3.0-dev.0
coding-tools-mcp-0.3.0-dev.0.tgz
```

Its file list remains exactly:

```text
LICENSE
README.md
bin/coding-tools-mcp.js
package.json
```

No wheel, sdist, npm tarball, database, Vault, or credential artifact remains in
the worktree.

## Security and External-Write Audit

- High-risk token/private-key/AWS credential matches in Phase 14 changes: 0.
- Remote URLs containing userinfo credentials: 0.
- Production telemetry changes: 0.
- OAuth/Transcript DB, Vault, WAL/SHM, or secret JSON files committed: 0.
- Tag at the local preparation HEAD: none.
- Atomic push completed to `onestao`; `origin` was not written.
- PyPI publish: not performed.
- npm publish: not performed.
- GitHub Release: not created.
- Cloudflare deployment did not run: the workflow failed at the credential preflight before deployment.
- Local and `onestao/main` now point to the validated merge history.
- WIP or backup refs: not deleted.

## Remote Push, Main Merge, and Fork CI

The user explicitly authorized both the remote push and merge to `main`.

The integration branch and `main` were updated atomically on the `onestao`
remote:

```text
onestao/integration/upstream-v0.2.2 = 6551ff96f23c58c2d4f0c899f9338cd7cbd43772
onestao/main                         = 463b0a5e29b090dead1610be7377744091e7b70d
```

A conventional merge was intentionally not used because it would have restored
obsolete pre-integration Admin, transcript, WebUI, and `tool_profile` files. The
final merge commit has two parents:

```text
first parent:  a23f6c87ae856bc63529a791824793b362e8987a
second parent: 6551ff96f23c58c2d4f0c899f9338cd7cbd43772
```

Its tree is exactly the validated integration tree:

```text
1f9302109bbb66531e66f6e0c9326dcf087ddc6d
```

The pre-merge local `main` is retained at:

```text
backup/main-before-v0.3.0.dev0 = a23f6c87ae856bc63529a791824793b362e8987a
```

GitHub-hosted Actions for exact SHA `463b0a5e29b090dead1610be7377744091e7b70d`:

| Workflow | Result | Run ID |
| --- | --- | --- |
| `compliance` | success, including Windows MSVC smoke | `30623082019` |
| `docker-image` | success | `30623082014` |
| `deploy-sandbox-control` | failed at `Require Cloudflare credentials`; no deployment occurred | `30623082011` |

The sandbox-control failure is an environment/configuration failure before any
Cloudflare write. It does not invalidate the Runtime, packaging, or release
candidate evidence.

No tag was created. PyPI, npm, and GitHub Release publishing were not triggered.
The release workflow remains tag-only, so `0.3.0.dev0` is merged but not
published.

## Rollback and Protected References

Protected refs remain present:

- `wip/pre-upstream-v0.2.2`
- `backup/local-before-v0.2.2`

To abandon all Phase 14 work, reset the integration branch to:

```text
cd51ee0297c6e14c2df8eacd844aaea28efff933
```

To retain dependency/CI preparation but abandon the selected development
release, reset to the prior blocked handoff:

```text
f142dd73822fdabca6a0aba8bfb4dc8fa78fcd02
```

Do not delete WIP or backup refs until the user accepts the final release and
rollback strategy.
