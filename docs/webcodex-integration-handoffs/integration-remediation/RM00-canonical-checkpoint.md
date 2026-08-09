# RM00 Handoff — canonical checkpoint

## Status

- Result: complete with baseline environment failure recorded
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `577ef27999ee5bc95e4f2fceb135ed1daedf1d2f`
- Implementation commit: `de5fa8aa205a58fa5a2bb4ed1286b671eccd67d3`

## Defect Closed

- The existing 22-file strict-completion diff is independently checkpointed before P1 remediation.
- The remediation status is reopened honestly: Phase 18 and Phase 22 are `in remediation` until RM01–RM13 and final release gates complete.

## Files Changed

- The 22 files listed in execution-book section 1.2 were checkpointed unchanged as the implementation commit.
- `docs/webcodex-integration-handoffs/STATUS.md`: reopened Phase 18/22 status and retained the 500-runtime non-claim.
- This handoff records the checkpoint evidence and does not include the untracked execution book.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: not changed by RM00
- Main worktree modified: no

## Validation

| Command | Exit code | Passed / skipped | Notes |
| --- | ---: | --- | --- |
| `python -m unittest tests.test_operator_api tests.test_validation_backend tests.test_workspace_host tests.test_runner_mcp_routing tests.test_runner_capabilities tests.test_runner_websocket tests.test_runner_remote tests.test_upstream_resilience tests.test_upstream_lazy_catalog tests.compliance.test_agent_platform tests.test_desktop_client` | 1 | 115 passed / 1 skipped / 1 failed | Failure: `test_local_validation_runs_through_workspace_runtime_exec_boundary`; the Runtime subprocess boundary returned `WinError 5` for the current sandbox environment's `D:\YING\APPData\Roaming\npm` path. The same validation path exists at starting HEAD; no RM00 production change was made for this environment-specific failure. A thread traceback from the existing upstream initialization-close race was also emitted during the run and is recorded for RM01/RM05 follow-up. |
| `git diff --check` | 0 | pass | Working-tree diff was clean before the checkpoint commit. |
| `git diff --stat` | 0 | pass | 22 files, 1289 insertions and 71 deletions checkpointed. |

## Security Review

- Full Session ID/token/argv secret exposed: no evidence introduced by RM00
- Real credential/data used: no
- The execution book remains untracked and was not staged.

## Remaining Risks

- RM01 must fix HTTP status/body classification, stale Session clearing, and authoritative DELETE headers.
- RM02–RM10 remain open, including deep catalog immutability, auth separation, Runner/HTTP shutdown races, observability, HTTP integration coverage, POSIX permission evidence, WebSocket ADR wording, and the 500 full-Runtime measurement.
- RM12 release aggregate and RM13 final handoff must not be claimed complete.

## Next Card Preconditions

- Start RM01 from `de5fa8aa205a58fa5a2bb4ed1286b671eccd67d3`.
- Keep the execution book untracked unless explicitly requested as a product document.
- Add RM01 failing tests first and limit production changes to the RM01 allowlist.
