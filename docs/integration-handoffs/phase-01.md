# Phase 01 Handoff

## Status

- Result: blocked
- Integration branch: `integration/upstream-v0.2.2`
- Implementation commit: none
- Handoff commit: filled by next agent from `git log`
- Started from: `311c1f2529d0f047ad2a8b68db6bf92dbb93d6bc`

## Scope Completed

- Recorded the user's 2026-07-30 confirmation that the previously exposed GitHub PAT was revoked or rotated.
- Fetched `origin` refs and tags, then verified both `origin/main` and `v0.2.2` resolve to the locked upstream commit.
- Created `.worktrees/upstream-v0.2.2-integration` on `integration/upstream-v0.2.2` from exact upstream commit `311c1f2`.
- Cherry-picked only the standalone execution-plan commit and the Phase 00 handoff commit.
- Did not cherry-pick the local WIP checkpoint or any local feature commit.
- Created the short integration status table.
- Installed the upstream development environment with `uv sync --extra dev`.
- Ran the first required upstream baseline gate and stopped immediately when it failed, as required by the execution protocol.

## Files Changed

- `docs/upstream-v0.2.2-integration-agent-execution-plan.md`: cherry-picked as integration-branch commit `2251ff8`.
- `docs/integration-handoffs/phase-00.md`: cherry-picked as integration-branch commit `9630f93`.
- `docs/integration-handoffs/STATUS.md`: records Phase 00 as complete and Phase 01 as blocked.
- `docs/integration-handoffs/phase-01.md`: records the upstream baseline failure and retry requirements.

## Decisions Applied

- The user's explicit PAT revocation/rotation confirmation satisfies the remaining Phase 00 precondition; the historical Phase 00 handoff was not rewritten.
- The local WIP checkpoint `ac59871` was not brought into the integration branch.
- No upstream source, test, workflow, lockfile, or report file is included in the handoff diff.
- `uv sync --extra dev` updated the stale upstream `uv.lock` in the worktree; that generated change was inspected and restored to upstream HEAD before handoff.
- The remaining Phase 01 commands were not run after the unittest failure because the global protocol requires stopping after a critical command fails.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `git status --short --branch` in the primary worktree | 0 | Clean on `wip/pre-upstream-v0.2.2` at `8063290`. |
| `git worktree list --porcelain` | 0 | Only the primary worktree existed before Phase 01 creation. |
| `git fetch origin --prune --tags` | not captured | The async command completed without a retained exit record; both required refs were verified immediately afterward. |
| `git rev-parse origin/main` | 0 | Returned `311c1f2529d0f047ad2a8b68db6bf92dbb93d6bc`. |
| `git rev-parse v0.2.2` | 0 | Returned `311c1f2529d0f047ad2a8b68db6bf92dbb93d6bc`. |
| `git worktree add ".worktrees\upstream-v0.2.2-integration" -b "integration/upstream-v0.2.2" origin/main` | not captured | Checkout completed; the worktree became clean on the expected branch and exact HEAD. |
| `git rev-parse HEAD` in the integration worktree | 0 | Returned exact upstream commit `311c1f2529d0f047ad2a8b68db6bf92dbb93d6bc` before document cherry-picks. |
| `git cherry-pick a23f6c87ae856bc63529a791824793b362e8987a` | 0 | Created integration-branch plan commit `2251ff8`. |
| `git cherry-pick 806329073da99c8aa9032076939463bf640dea76` | 0 | Created integration-branch Phase 00 handoff commit `9630f93`. |
| `uv sync --extra dev` | not captured | Environment creation completed; `.venv` exists and subsequent `uv run` used Python 3.11.14. The generated `uv.lock` drift was not retained. |
| `uv run python --version` | 0 | Returned `Python 3.11.14`. |
| `uv run python -m unittest discover -s tests -p "test_*.py"` | 1 | Ran 192 tests in 28.455 seconds: 7 failures, 10 errors, 83 skipped. |
| `git restore --worktree uv.lock` | 0 | Removed the dependency-sync lockfile drift created during this Phase. |

## Known Baseline Failures

- Core unittest baseline is red on this Windows/Codex execution environment: `FAILED (failures=7, errors=10, skipped=83)`.
- Several errors are caused by the execution environment omitting normal Windows process variables or a resolvable home directory: `%ComSpec%`, `%SystemRoot%`, and home-directory lookup failures.
- Windows temporary-directory cleanup hit open-handle errors (`WinError 32`) in runtime helper tests.
- Several assertions assume POSIX output or paths in this run, including LF instead of CRLF and `/etc/resolv.conf` in Landlock roots.
- Command execution tests observed Windows shell quoting/output differences, causing nonzero command exits and truncated stderr previews.
- Additional failures include active-process-limit behavior and environment-key casing (`Path` versus the inherited environment produced in this execution context).
- The following required Phase 01 gates were not run after the first critical failure: ruff, `mcp-contract`, `tool-golden`, and npm launcher tests.

## Remaining Risks

- Phase 02 must not start while the pure-upstream baseline is red.
- The current result may combine genuine upstream Windows baseline defects with restrictions imposed by the Codex MCP command environment; those causes have not yet been separated.
- Upstream `pyproject.toml` and checked-in `uv.lock` are not synchronized for version `0.2.2` and the dev `PyYAML` dependency; `uv sync --extra dev` therefore wants to rewrite the lockfile. No lockfile change was retained.
- The integration branch contains only documentation commits beyond upstream `v0.2.2`; no product implementation has started.

## Next Phase Preconditions

- Retry Phase 01 baseline in a supported clean environment that provides normal Windows variables (`ComSpec`, `SystemRoot`, `USERPROFILE`/home) or in the upstream Linux CI environment.
- Do not modify upstream source merely to force the baseline green; first determine which failures reproduce outside the constrained Codex execution environment.
- Run the remaining Phase 01 gates only after the core unittest command exits 0, or after the execution plan is explicitly amended to accept a documented upstream baseline failure.
- Keep Phase 02 marked pending until Phase 01 is complete.

## Secret Check

- No credentials, OAuth databases, bearer tokens, signing secrets, or vault files were added.
- The user explicitly confirmed the previously exposed PAT was revoked or rotated.
