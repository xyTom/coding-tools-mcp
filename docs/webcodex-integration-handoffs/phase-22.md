# Phase 22 Release Validation Handoff

## Status

- Result: complete; `d8c1cc8` is the canonical implementation baseline and RM13 records the final release handoff after RM12 gates passed.
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

### RM12 final aggregate rerun

The exact RM12 gates were rerun on candidate commit `81072da`:

| Exact command/group | Exit | Result |
| --- | ---: | --- |
| `python -m unittest tests.compliance.test_tool_golden tests.compliance.test_schema_drift tests.compliance.test_mcp_contract` | 0 | 54 tests; 46 skipped: 9 tool-golden and 37 MCP-contract Windows `/dev/null` fixture skips; schema drift 8 passed |
| In-process Runtime catalog check | 0 | Registry 25; exposed list 25; `listChanged=False` |
| Runner/resilience aggregate from RM12.2 | 0 | 198 tests; 1 PySide6 skip; no failures or unhandled thread exception |
| Security/persistence aggregate from RM12.3 | 0 | 65 tests; 1 Windows POSIX skip |
| Ubuntu WSL `python3 -m unittest tests.compliance.test_chat_persistence.ChatPersistenceTests.test_startup_settings_transcript_and_sqlite_sidecars_are_private` | 0 | 1 passed; real settings/config/transcript/database/WAL/SHM/journal mode evidence |
| Ubuntu WSL `python3 -m unittest tests.compliance.test_chat_persistence` | 0 | 17 tests; 1 Windows file-sharing skip |
| `git diff --check` | 0 | Passed |
| `git diff --check main...HEAD` | 0 | Passed |
| `git diff --name-status main...HEAD` | 0 | Passed |

Platform skips are recorded as skips, not silently counted as passed behavior.
The Linux POSIX permission result is supplied by Ubuntu WSL; Windows skips do
not substitute for it.

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
- Ubuntu WSL independently verified transcript/config/SQLite sidecar POSIX modes for candidate commit `81072da`; no real user data was used.
- RM13A post-review regressions cover detached HTTP/Runner shutdown completion and URL userinfo redaction on candidate commit `73888d5`.

## Release Notes / Limitations

1. Linux Inspect enforcement should still be exercised in Linux CI/release infrastructure; current host is Windows.
2. PySide6-specific desktop UI execution is skipped when PySide6 is absent, while non-UI connectivity behavior is tested.
3. Real Codex App Server/model latency and third-party LSP memory/startup are deployment-specific and not represented by synthetic service benchmarks.
4. Runner CLI supports Runner-local upstream gateway configuration through `--upstream-config`; configuration is explicit and is not implicitly copied from the Control Plane.
5. 500 full Runtime objects were measured twice on the Windows validation host by RM10; this remains capacity evidence only, does not raise the production default of 128, and is not a claim about other platforms or workloads.
