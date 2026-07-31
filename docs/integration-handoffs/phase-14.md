# Phase 14 Handoff

## Status

- Result: blocked
- Branch: `integration/upstream-v0.2.2`
- Started from: `cd51ee0297c6e14c2df8eacd844aaea28efff933`
- Preparation commits:
  - `a55d216` - `build(deps): refresh integration lock metadata`
  - `ab2c931` - `ci(compliance): allow setup-node under Landlock`
- Handoff commit: this document's commit; resolve with `git log -1`.
- Phase 14 is not complete because version, telemetry, and push decisions have not been authorized.

## Work Completed Before the Authorization Boundary

No release version, release notes, tag, registry artifact, branch switch, or remote ref was changed.

Two release-preparation defects were fixed:

1. `uv.lock` had drifted from `pyproject.toml`:
   - the local package was locked as `0.2.0` instead of `0.2.2`;
   - the `PyYAML>=6.0` development dependency was absent.
2. GitHub Actions setup-node installs Node outside the default Landlock read roots:
   - `npm test` reproduced exit 126 with `LANDLOCK_READ_ROOT_BLOCKED`;
   - the compliance workflow now resolves the setup-node installation root and exports it through `CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS`;
   - a regression test checks that this happens after setup-node and before the test gates.

## Linux Validation

The exact committed source tree at `ab2c931` was exported with `git archive` into a temporary WSL2 Ubuntu filesystem and tested with:

- kernel: WSL2 Linux `6.18.33.1-microsoft-standard-WSL2`
- CPython `3.11.15`
- Node `22.23.2`
- npm `10.9.8`
- uv `0.12.0`

The Node archive checksum was verified. Tools, Python, virtual environments, and source snapshots were created only under WSL `/tmp`; no WSL system package or repository file was modified.

All 72 compliance test bodies skipped by the native Windows baseline were executed under Linux:

| Suite | Result |
| --- | --- |
| `mcp-contract` | 37/37 passed |
| `tool-golden` | 9/9 passed |
| `security` | 15/15 passed |
| `runtime-semantics` | 4/4 passed |
| `e2e` | 6/6 passed |
| `dogfood` | 1/1 passed |
| **Total** | **72/72 passed** |

The setup-node/Landlock environment was reproduced using the same dynamic root calculation committed in `.github/workflows/compliance.yml`.

This is real Linux execution, but not a GitHub-hosted Actions run. Authoritative fork CI remains pending until a push is authorized.

## Windows Validation

The full Windows suite was rerun with isolated `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TEMP`, `TMP`, `SystemRoot`, `WinDir`, and `ComSpec`:

```text
Ran 312 tests in 75.192s
OK (skipped=83)
```

The 83 Windows skips retain the Phase 13 classification. The 72 compliance bodies among them now have separate successful Linux evidence.

Additional gates:

| Gate | Result |
| --- | --- |
| `uv lock --check` | passed |
| Ruff on changed Python test | passed |
| `docs-required` | 6/6 passed |
| `schema-drift` | 8/8 passed |
| release + Phase 11 packaging tests | 11/11 passed |
| workflow YAML parse | passed |
| dispatch input consistency | passed |
| `git diff --check` | passed |

The strict release validator still rejects the tree because `CHANGELOG.md` retains `## Unreleased`. This is intentional until the version decision is authorized.

## Version Decision Required

Current metadata remains unchanged:

| Surface | Current value |
| --- | --- |
| Python `pyproject.toml` | `0.2.2` |
| `coding_tools_mcp.__version__` | `0.2.2` |
| npm launcher | `0.1.0` |
| Desktop client | bundled in Python distribution; no independent version field |
| CHANGELOG | `## Unreleased` remains |

Recommended fork integration version: `0.3.0.dev0`, as specified by the execution plan.

Explicit approval of `0.3.0.dev0` or another PEP 440 version is required before changing Python, npm/Desktop, CHANGELOG, or release validation metadata.

## Telemetry Decision Required

The integration still preserves the upstream telemetry default and the Phase 13 privacy schema. No telemetry product-policy change was made.

The user must choose whether the fork keeps the upstream default or adopts a different policy in a separately reviewed change.

## Push Decision Required

No remote write was performed. Explicit authorization is required for:

```text
integration/upstream-v0.2.2 -> onestao
```

If authorized, `onestao` is the only planned push target. `origin` must not be written.

## Release and External-Write Audit

Not performed:

- no Python/npm/Desktop version change;
- no removal of `Unreleased`;
- no tag;
- no PyPI or npm publish;
- no GitHub Release;
- no Cloudflare deployment;
- no push;
- no local `main` replacement;
- no WIP or backup deletion.

## Feature Preservation Summary

Phase 13 plus the Linux execution verifies the integrated contracts for MCP protocol and sessions, atomic patching, bounded runtime output, Windows process-tree termination, OAuth DCR/PKCE and persistence, Refresh rotation/replay handling, Settings and Workspace Catalog, Admin authentication, restart-only Gateway settings, upstream Gateway isolation, transcript/chat/Codex persistence, WebUI, Desktop, npm launcher, telemetry privacy, packaging, docs, and schema drift.

No live `tool_profile` control path or dynamic Gateway lifecycle operation was restored.

## Rollback and Protected References

Protected refs remain present:

- `wip/pre-upstream-v0.2.2`
- `backup/local-before-v0.2.2`

To abandon only the Phase 14 preparation commits before push, reset the integration branch to:

```text
cd51ee0297c6e14c2df8eacd844aaea28efff933
```

Do not delete WIP or backup refs until the user accepts the final release and rollback strategy.

## Decisions Required to Resume

Phase 14 resumes only after all three decisions are provided:

1. fork version;
2. telemetry product policy;
3. whether push to `onestao` is authorized.
