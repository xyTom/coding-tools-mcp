# WebCodex Runtime Platform Integration Status

Canonical branch: `integration/webcodex-runtime-platform`
Canonical worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`

The integrated implementation baseline is `d8c1cc855f71303a525da288a6c8e895a76a10b8`.
Strict remediation continues through `dccef08cd8a0085bdd484a4741be9848faca301e`;
the final user-document and merged release-gate candidate is `140f47d6f8cc3af505b9fc91905e067b077a0272`.

| Phase | Status | Implementation | Handoff / Evidence | Notes |
| --- | --- | --- | --- | --- |
| 00 | complete | `198e7b9` | `phase-00.md` | canonical worktree / baseline |
| 01 | complete | `d8c1cc8` | `../webcodex-runtime-integration-contract.md`, ADR 0004 | product/runtime boundaries reconciled |
| 02 | complete | `d8c1cc8` | Phase 22 grouped contract/golden/schema results | characterization protects Core MCP semantics |
| 03 | complete | `d8c1cc8` | `coding_tools_mcp/processes.py`, Phase 22 | process/session primitives separated from Runtime composition |
| 04 | complete | `d8c1cc8` | `tests/test_execution_backend.py` | `ExecutionBackend` + `LocalExecutionBackend`; Core tools unchanged |
| 05 | complete | `d8c1cc8` (source `3390988`) | Agent Platform tests | durable `AgentSessionStore` |
| 06 | complete | `d8c1cc8` (source `3390988`) | Agent Platform tests | Codex App Server backend |
| 07 | complete | `d8c1cc8` (source `3390988`) | Agent Platform tests | `AgentSessionService` |
| 08 | complete | `d8c1cc8` | `phase-22.md` | Operator auth/API distinct from Admin auth |
| 09 | complete | `d8c1cc8` | WebUI Node/build tests | `/app` workspace/session workbench |
| 10 | complete | `d8c1cc8` | Operator/WebUI tests | fetch-SSE, approval, interrupt, bounded events |
| 11 | complete | `d8c1cc8` | Operator + Runner capability tests | local/remote repo fingerprint + durable cross-window attachment |
| 12 | complete | `d8c1cc8` | deterministic handoff tests | runtime/durable facts, bounded projection, no root/thread secret leakage |
| 13 | complete | `d8c1cc8` (source `aacb951`) | `tests/test_semantic_backend.py` | original full 6-test Semantic suite restored and green |
| 14 | complete | `d8c1cc8` | `tests/test_validation_backend.py` | fixed structured recipes; no dependency auto-install |
| 15 | complete | `d8c1cc8` | Runtime helper suite | Windows truthful capability test green; Linux Landlock behavior remains platform-conditional |
| 16 | complete | `d8c1cc8` | WorkspaceHost/catalog/binding tests | local/runner targets; remote root opaque; no local fallback |
| 17 | complete | `d8c1cc8` (source Runner workstream) | Runner transport/WebSocket tests | credential/registry + production outbound Runner peer and `/runner/ws` |
| 18 | complete | `dccef08` | `phase-18-session-resilience-final.md`, `phase-18-session-resilience-retrospective.md`, `integration-remediation/RM13-final-release-handoff.md`, `integration-remediation/RM13A-post-review-remediation.md`, `integration-remediation/RM13B-concurrent-shutdown-and-runner-id-reuse.md` | RM00–RM13B findings closed; final Integration rerun records exact gates and residual platform limits |
| 19 | complete | `d8c1cc8` (source `2e705d7`) | Runner job reconciliation tests | recovering/running/completed/failed/cancelled/lost reconciliation |
| 20 | complete | `d8c1cc8` | Desktop tests | tunnel providers, external URL, mobile onboarding, Runner status |
| 21 | not enabled (optional) | none by design | `phase-22.md` | no second MCP/OpenAPI facade; stable Core 25-tool surface preserved |
| 22 | complete | `140f47d` | `phase-22.md`, `integration-remediation/RM12B-linux-posix-evidence.md`, `integration-remediation/RM13B-concurrent-shutdown-and-runner-id-reuse.md` | Final merged Core, Runner/resilience, and security/persistence aggregates passed |

## Current Release Evidence

- `docs/webcodex-session-resilience-benchmark-report.md`
- `docs/webcodex-runner-troubleshooting.md`
- `docs/webcodex-migration-rollback.md`
- `docs/webcodex-integration-handoffs/phase-18-session-resilience-final.md`
- `docs/webcodex-integration-handoffs/phase-18-session-resilience-retrospective.md`
- `docs/webcodex-integration-handoffs/phase-22.md`
- `docs/webcodex-integration-handoffs/integration-remediation/RM12B-linux-posix-evidence.md`
- `docs/webcodex-integration-handoffs/integration-remediation/RM13-final-release-handoff.md`
- `docs/webcodex-integration-handoffs/integration-remediation/RM13A-post-review-remediation.md`
- `docs/webcodex-integration-handoffs/integration-remediation/RM13B-concurrent-shutdown-and-runner-id-reuse.md`
- `docs/coding-tools-mcp-agent-runner-guide.md`
- `docs/coding-tools-mcp-agent-runner-guide.zh-CN.md`

## Non-claims

- Two 500-full-Runtime measurements were completed on the Windows integration host (RM10); the 500 point is capacity evidence only and the production default of 128 is unchanged.
- Real Codex/model latency is not represented by the synthetic Agent persistence benchmark.
- Linux Landlock enforcement was not executed on the Windows integration host.
- Linux POSIX transcript/config/SQLite sidecar permissions were independently verified in Ubuntu WSL for candidate commit `81072da`.
- Phase 21 optional facade is intentionally absent.
