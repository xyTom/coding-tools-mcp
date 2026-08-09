# Phase 22 Release Validation Handoff

## Status

- Result: `d8c1cc8` is the canonical implementation baseline; Phase 22 release status remains **in remediation** until all RM12 aggregate/audit gates pass and RM13 issues the final handoff. No release-complete claim is made yet.
- Branch: `integration/webcodex-runtime-platform`
- Baseline HEAD: `252c2f0b5508e6b6c23a330e3417943960249eca`
- Implementation commit: `d8c1cc855f71303a525da288a6c8e895a76a10b8`.

## Functional Validation Matrix

### Core MCP

- MCP contract / schema drift / golden tools / Workspace binding: exit 0.
- Core public schema remains the stable 25-tool surface.
- `tools.listChanged=false` remains truthful.
- Strict JSON transport: exit 0 in the Operator/Admin security group.
- Runtime helper / exec semantics / Inspect configuration: exit 0.
- Upstream gateway / lifecycle: exit 0.
- Full upstream resilience + lazy catalog + HTTP Session resilience aggregate: exit 0.

Some contract fixture cases are skipped on this Windows validation host because POSIX `/dev/null`/signal fixtures are unavailable. The grouped suites themselves exit 0; skipped platform fixtures are not counted as passed behavior on Windows.

### Final integration rerun

The final canonical working-tree rerun on 2026-08-09 completed with these explicit gates:

| Gate | Result |
| --- | --- |
| Runner WebSocket + remote capabilities/MCP + HTTP Session + upstream resilience/lazy catalog aggregate | exit 0 |
| Core MCP / strict JSON / schema drift / tool golden / runtime semantics / upstream compliance group | exit 0 (`skipped=50`, platform/fixture conditional) |
| Admin / OAuth persistence / security / chat / Operator / Agent Platform group | exit 0 (`skipped=15`, platform/fixture conditional) |
| Runtime helpers / required docs / compliance report / e2e / Windows smoke / support / dogfood groups | exit 0 (`skipped=19`, platform/fixture conditional) |
| OAuth fail-closed | exit 0 |
| OAuth integration + refresh | exit 0 |
| OAuth signing + store | exit 0 |
| tracked top-level Desktop / v0.2.2 contract / packaging / release / settings / telemetry / WebUI / Workspace binding group | exit 0 (`skipped=1`, PySide6 conditional) |
| new ExecutionBackend / Operator / Semantic / Validation / WorkspaceHost group | exit 0 |
| WebUI Node tests + production build | exit 0 |
| final schema drift + v0.2.2 integration contract + MCP contract quick gate | exit 0 (`skipped=37`, POSIX fixture conditional) |
| required-docs compliance after final handoff update | exit 0 |
| common secret-prefix/private-key literal scan over the canonical worktree | 0 matches |
| `git diff --check` (working tree) | exit 0 | Working-tree check only; `main...HEAD` range check is re-run in RM12. |

One attempted monolithic compliance run and two aggregate top-level/OAuth commands exceeded the coding-tools command/session window. They are not counted as passing evidence; every tracked compliance module and affected top-level area was rerun in shorter groups above with explicit exit 0 results.

### OAuth / Admin / Operator / secrets

- Dedicated Admin token boundary: exit 0.
- Ordinary MCP bearer cannot mutate Admin settings or issue Runner credentials.
- Runner credential lifecycle is Admin-only; metadata GET does not return plaintext credential.
- Operator API uses MCP/OAuth principal and does not fall back to Admin credential.
- Workspace/principal/session isolation, strict JSON, Origin checks, and no-root projections: exit 0.
- Remote Admin filesystem scope fails closed instead of interpreting a runner root locally.

### Agent Session / Web App

- AgentSessionStore create/reopen/version guard, create/resume/close, unavailable backend, approvals, interrupts, event drain: exit 0.
- Local and remote repo fingerprint drift, explicit instruction drift, cross-window durable attachment, and deterministic handoff: exit 0.
- Operator WebUI Node tests: exit 0.
- Production WebUI build: exit 0.
- Backend text is rendered via DOM text nodes/textContent; bearer is kept in page memory and not placed in URLs or Admin storage.

### Runner

- credential issue/authenticate/revoke;
- registry heartbeat/duplicate/replay handling;
- authenticated production `/runner/ws` RFC6455 Upgrade;
- real `RunnerPeer` request/response dispatch;
- synchronous Control Plane bridge on transport owner loop;
- remote Agent/Semantic/Validation/fingerprint;
- remote MCP initialize/list/call/delete;
- Runner unavailable retryable/no local fallback;
- reconnect/job/MCP inventory reconciliation;
- bounded Control Plane shutdown;
- original-Runner close affinity.

Runner/resilience grouped suites exit 0.

### Semantic / Validation

- Full Semantic Agent test suite restored: 6 tests, exit 0.
- Definitions/references are Workspace-scoped and bounded; diagnostics text bounded; missing LSP is explicit unavailable.
- Structured Validation recipes and LocalWorkspaceHost Runtime execution boundary: exit 0.
- Recipes do not install dependencies; Cargo/Go use offline behavior.

### Inspect

- Runtime/Inspect test group exits 0.
- Windows validation confirms truthful `inspect_enforced=false` when Landlock is unavailable.
- The Linux-only Landlock behavior test is skipped on this host. Phase 15 implementation contains the Linux read-only Workspace/runtime-scratch split, but this handoff does **not** claim a live Linux run occurred here.

### Desktop / mobile onboarding

- Desktop connectivity suite exits 0; one PySide6-specific UI test is skipped where PySide6 is not installed.
- Cloudflare/FRP/External URL provider boundaries, public noauth rejection, secret redaction, Operator/MCP/Admin URL separation, and Runner state injection are covered.

## Performance / Capacity Evidence

See `docs/webcodex-session-resilience-benchmark-report.md` and `scripts/benchmark_webcodex_release.py`.

Key release values on the Windows validation host:

- Runtime initialize p50: 0.0904 ms; `tools/list` p50: 0.0960 ms; tool-list payload 25,056 bytes.
- Full Runtime working-set sampled at 0/100/128/500 in two RM10 runs; incremental slopes approximately 15.98 KiB/Runtime (run 1) and 17.87 KiB/Runtime (run 2), allocated/process noise included.
- HTTP Session-manager 1280 create/delete soak: active 0; created=deleted=1280; +4 KiB observed working-set delta.
- Lease p95 remains about 0.009 ms at 1/100/128/500 lightweight installed Session records.
- AgentSessionService+SQLite benchmark: create p50 25.47 ms, resume p50 1.16 ms; explicitly not real Codex process/model latency.
- fake-LSP cold 176.98 ms; warm p50 2.79 ms.
- real loopback Runner WebSocket RPC p50 3.13 ms, p95 6.39 ms; reconnect 34.26 ms.

RM10 measured 500 full Runtime objects twice on the Windows validation host; both runs completed with explicit exit 0 and are capacity evidence only. The production default of 128 is unchanged.

## Migration / Rollback

Documented in `docs/webcodex-migration-rollback.md`:

- DB/settings/vault backup guidance;
- AgentSession DB schema version 1 and newer-version fail-closed behavior;
- local Workspace Catalog backward compatibility and runner target migration;
- Runner opt-in/disable/revoke path;
- Operator App does not replace local MCP-only mode;
- local MCP-only rollback path;
- inspect-mode and upstream lazy-catalog rollback considerations.

## Troubleshooting

`docs/webcodex-runner-troubleshooting.md` distinguishes:

- global Session capacity;
- per-identity quota;
- initialization pressure;
- upstream backoff/circuit;
- Runner unavailable and close-pending;
- Runner credential provisioning/rotation;
- Inspect not enforced;
- Operator event-stream disconnect.

## Phase 21 Decision

Phase 21 is optional by product contract. It is **not enabled** in this integration. No workflow/OpenAPI facade and no second MCP surface were added. This avoids expanding the stable Core 25-tool contract simply to mark an optional phase complete.

## Security / Workspace / Temp Compliance

- Canonical worktree remains inside project `.worktrees/`.
- No real token, Runner credential, transcript DB, OAuth DB, or user data is committed.
- Benchmark/tests use temporary/synthetic state and loopback only.
- Remote root remains opaque in the Control Plane and is rejected by local filesystem scope helpers.
- Ambiguous mutating calls are never automatically replayed.

## Release Notes / Limitations

1. Linux Inspect enforcement should still be exercised in Linux CI/release infrastructure; current host is Windows.
2. PySide6-specific desktop UI execution is skipped when PySide6 is absent, while non-UI connectivity behavior is tested.
3. Real Codex App Server/model latency and third-party LSP memory/startup are deployment-specific and not represented by synthetic service benchmarks.
4. Runner CLI currently has no Runner-side upstream gateway configuration option; this does not affect remote Core local tools, Agent, Semantic, Validation, or MCP lifecycle routing.
5. 500 full Runtime objects were measured twice on the Windows validation host by RM10; this remains capacity evidence only, does not raise the production default of 128, and is not a claim about other platforms or workloads.
