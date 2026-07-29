# Phase 01 Unblock Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Product implementation commit: none
- Started from blocked handoff: `b29907ee2c67590acfee1877baad010244f76c78`
- Upstream baseline SHA: `311c1f2529d0f047ad2a8b68db6bf92dbb93d6bc`

## Why The Block Was Reclassified

The original Phase 01 plan incorrectly required the complete upstream unittest
discovery to pass in a native Windows Codex session. Upstream does not use that
as its Windows gate:

- the full compliance/unit job runs on `ubuntu-latest`;
- Windows runs only `tests.compliance.test_windows_msvc_smoke`;
- the v0.2.2 full test suite contains POSIX commands, LF expectations, `/etc/*`
  paths, and POSIX test fixtures that are not guarded for native Windows.

The original blocked handoff remains unchanged as historical evidence. This
handoff records the diagnosis and the platform-aware replacement gates.

## Diagnosis Evidence

The failure was reproduced in the clean integration worktree. A second full
Windows discovery ran 206 tests and ended with 7 failures, 4 errors, and 84
skips. Focused single-test runs demonstrated deterministic platform causes:

- command tests use `printf`, `sleep`, and `pwd`, none of which resolves as a
  native executable in this Windows environment;
- one test clears the process environment and retains only `PATH`, which removes
  `ComSpec` and `SystemRoot` before invoking `shell=True`;
- the core-environment test expects `Path` casing even though Windows
  `os.environ` normalizes the key to `PATH`;
- guard-root assertions unconditionally expect `/etc/resolv.conf`, `/usr`, and
  other POSIX roots;
- text fixtures written through Windows text mode produce CRLF while assertions
  require LF;
- Python `-c` and output-pagination fixtures rely on POSIX single-quote shell
  parsing;
- telemetry patches only `HOME`, while native Windows `Path.home()` resolves
  `USERPROFILE`;
- temporary-directory `WinError 32` failures are secondary cleanup errors after
  command/session tests fail or return before all handles are closed.

A focused probe confirmed that retaining the minimum Windows shell variables
allows `Runtime.exec_command({"cmd": "git --version"})` to exit 0, and patching
both `HOME` and `USERPROFILE` makes the telemetry install-id file appear in the
expected temporary directory.

## Exact-SHA GitHub Evidence

All evidence below is for
`311c1f2529d0f047ad2a8b68db6bf92dbb93d6bc`, not a stale checked-in report.

- Compliance workflow: <https://github.com/xyTom/coding-tools-mcp/actions/runs/30330831525>
  - conclusion: success
  - Linux lint, typecheck, unit discovery, protocol, integration, docs,
    schema-drift, npm launcher, dogfood, latency, and compliance report steps
    succeeded;
  - `windows-msvc-smoke` job succeeded.
- v0.2.2 release workflow: <https://github.com/xyTom/coding-tools-mcp/actions/runs/30331518539>
  - conclusion: success
  - `Evidence / compliance` succeeded;
  - `Evidence / windows-msvc-smoke` succeeded;
  - real-workloads, SWE-bench, distribution build, PyPI publish, and GitHub
    Release jobs succeeded or were intentionally skipped according to workflow
    conditions.

The checked-in `reports/compliance/latest.*` was not used as v0.2.2 evidence
because it identifies `e9c9acf...+dirty`, not the target SHA.

## Local Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.compliance.test_windows_msvc_smoke` | 0 | Ran 4 tests: OK, 2 skipped because the current shell does not have an initialized MSVC environment. The two general Windows process tests passed. |
| `.\.venv\Scripts\python.exe -m ruff check --exclude benchmarks/dogfood --ignore=E501 coding_tools_mcp apps/desktop-client/mcp_desktop_client tests benchmarks` | 0 | All checks passed. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite mcp-contract` | 0 | Runner exited 0; 37 tests were skipped because the compliance fixture preflight requires POSIX `/dev/null`. Exact-SHA Linux CI covers the gate. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite tool-golden` | 0 | Runner exited 0; 9 tests were skipped for the same `/dev/null` preflight. Exact-SHA Linux CI covers the gate. |
| `npm --prefix npm/coding-tools-mcp test` | 1 | One test passed and three failed because the test creates `#!/bin/sh` extensionless fake runners. Exact-SHA Linux CI npm launcher gate passed. This remains a required Linux/fixed-fixture gate for the final integrated candidate. |

## Plan Amendment

Phase 01 now uses platform-aware gates:

- Linux/WSL runs complete unittest discovery locally, or exact-SHA GitHub
  compliance/release evidence is accepted for the pristine upstream baseline;
- native Windows runs `test_windows_msvc_smoke` as the official local gate;
- full Windows unittest failures already proven to be POSIX-only are diagnostic,
  not blocking;
- stale checked-in reports cannot replace exact-SHA evidence;
- dependency setup and commands must avoid retaining automatic `uv.lock` drift;
- the POSIX-only npm fixture failure is non-blocking only for this pristine
  Windows baseline and is not waived for the final integration candidate.

## Files Changed

- `docs/upstream-v0.2.2-integration-agent-execution-plan.md`: makes Phase 01
  gates platform-aware and records the exact-SHA evidence rule.
- `docs/integration-handoffs/phase-01-unblock.md`: records diagnosis and
  replacement validation.
- `docs/integration-handoffs/STATUS.md`: changes Phase 01 from blocked to
  complete; Phase 02 remains pending.

## Remaining Risks

- Native Windows full-suite portability remains imperfect upstream. Do not
  mistake that diagnostic suite for the official Windows gate.
- Local mcp-contract and tool-golden commands skipped their test bodies on
  `/dev/null` preflight; exact-SHA Linux CI is the evidence for those bodies.
- npm launcher tests need Linux CI or cross-platform fixtures after integrated
  code changes; the v0.2.2 baseline success cannot validate future changes.
- `uv sync --extra dev` rewrites the stale checked-in `uv.lock`; no such drift
  is retained in the worktree.

## Next Phase Preconditions

- Read this handoff rather than treating the original blocked handoff as current.
- Confirm `STATUS.md` shows Phase 01 complete and Phase 02 pending.
- Confirm the integration worktree is clean before Phase 02 begins.
- Phase 02 must not modify upstream product behavior; it only establishes the
  integration contract and migration-decision tests described in the plan.

## Secret Check

- No credentials, OAuth databases, bearer tokens, signing secrets, or vault
  files were added.
- No product source, upstream test, workflow, lockfile, or report artifact was
  changed.
