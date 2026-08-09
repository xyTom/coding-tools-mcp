# RM10 Handoff — 500 full Runtime and RS07 capacity evidence

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `abbf36d`
- Implementation commit: `85641f6`

## Defect Closed

- The release evidence previously had no successful 500-full-Runtime
  measurement; the old report explicitly stated that the 500 attempt exceeded
  the harness window and was terminated.
- RM10 ran `scripts/benchmark_webcodex_release.py --section runtime
  --runtime-counts 0,100,128,500` twice to completion. Both runs produced full
  `Runtime` RSS points at 0, 100, 128, and 500, exited 0, and exercised the
  script's `finally` Runtime close path.
- The report now records the actual run data: environment (Windows 11
  `10.0.28000`, Python 3.12.0, 32 logical CPUs), RSS method
  (`windows-working-set`), wall-clock duration, both RSS series, incremental
  slope estimates/ranges, and the stated allocator/process noise.
- The 500 point is recorded as capacity evidence only; the production default
  of 128 was not changed.
- No eager N×M upstream Session construction was involved: the runtime section
  builds `Runtime(root, transport="http")` objects with no upstream servers
  configured.

## Files Changed

- `docs/webcodex-session-resilience-benchmark-report.md`: replaced the old
  0/50/100/128 table and the "500 not measured" statement with the two actual
  0/100/128/500 full-Runtime series and updated the capacity rationale and
  measurement limits.
- `scripts/benchmark_webcodex_release.py`: no change was needed; the existing
  harness already produced the required points and closes all Runtimes in
  `finally`.

Raw JSON evidence is preserved in the gitignored controlled temp directory:
`.tmp/agent-integration/RM10/runtime-0-100-128-500.json` and
`.tmp/agent-integration/RM10/runtime-0-100-128-500-second.json`.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: no
- Public schema, Core 25 tools, DB schema, ADR, or replay semantics expanded: no

## Validation

| Command | Exit code | Passed / skipped | Notes |
| --- | ---: | --- | --- |
| `python scripts\benchmark_webcodex_release.py --section runtime --runtime-counts 0,100,128,500 \| Tee-Object -FilePath .tmp\agent-integration\RM10\runtime-0-100-128-500.json` | 0 | Run 1 complete | Four full Runtime RSS points present; JSON valid. |
| Same command to `runtime-0-100-128-500-second.json`; then `LASTEXITCODE=$LASTEXITCODE` | 0 | Run 2 complete | `LASTEXITCODE=0`; four full Runtime RSS points present. |
| `ConvertFrom-Json` on both raw JSON files | 0 | Valid | `sessions` 0/100/128/500 with `rss_method=windows-working-set`. |
| `python -m unittest tests.test_http_session_resilience tests.test_upstream_resilience tests.test_runner_mcp_routing` | 0 | 46 passed | RM10 targeted adjacent regressions. |
| `git diff --check` | 0 | Passed | LF will be replaced by CRLF warnings only. |

## Security Review

- Full Session ID/token/argv secret exposed: no
- Real credential/data used: no
- Only synthetic in-process Runtime objects and loopback/local resources were
  used; no public network access.

## Remaining Risks

- RSS is process-wide Windows working set; allocator noise explains the two-run
  slope difference. Raw JSON and this handoff record the range rather than
  claiming a single exact per-Runtime cost.
- Only this Windows validation host was measured; Linux Landlock behavior and
  memory characteristics remain platform-conditional.
- The 500 point is not a license to raise production defaults; the report keeps
  128 total / 64 per-identity / 16 initializations / 3600 s TTL unchanged.

## Next Card Preconditions

- RM11 must build the retrospective provenance matrix from actual `git log` /
  `git diff` evidence for RS00–RS07 (e.g., Runner workstream commits
  `c7b1563`, `0160376`, `86aaf12`, `8f1b59d`, `32edfa8`, `85375a1`,
  `0dcbde9`) and must not fabricate history or claim per-card handoffs existed.
