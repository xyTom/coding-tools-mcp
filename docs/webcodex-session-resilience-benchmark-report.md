# WebCodex Session Resilience / Phase 22 Benchmark Report

## Scope

This report records the release-gate measurements used for the integrated WebCodex Runtime Platform and the Phase 18 Session Resilience supplement. The measurements are deliberately separated by boundary so that lightweight Session-manager results are not presented as full Runtime or Codex process costs.

- Host: Windows 11 `10.0.28000`, Python 3.12.0, 32 logical CPUs.
- Network: loopback only; no public network was used.
- Credentials: synthetic only; no real OAuth, Admin, Runner, transcript, or user data was used.
- Mutating ambiguous calls: **never automatically replayed**.
- Real Codex process latency: **not benchmarked here**. Agent measurements use the durable AgentSession service, SQLite, and an in-process benchmark backend.
- Linux Landlock performance/enforcement was not measured on this Windows host.

The reproducible harness is `scripts/benchmark_webcodex_release.py`.

## Core Runtime and Catalog

Ten fresh local Runtime samples were measured with no configured upstream servers.

| Measurement | p50 | p95 | Max | Bytes |
| --- | ---: | ---: | ---: | ---: |
| Runtime initialize | 0.0904 ms | 1.0472 ms | 1.0472 ms | 752 |
| `tools/list` | 0.0960 ms | 1.6727 ms | 1.6727 ms | 25,056 |

`tools/list` remained the stable Core catalog; schema-drift tests independently verify that the 25-tool contract and annotations did not change.

### Full Runtime working-set samples

These points use real `Runtime` objects and Windows process working set.

| Active Runtime objects | Run 1 working set | Run 2 working set |
| ---: | ---: | ---: |
| 0 | 49,090,560 B | 48,963,584 B |
| 100 | 50,421,760 B | 50,196,480 B |
| 128 | 50,909,184 B | 50,876,416 B |
| 500 | 57,311,232 B | 57,200,640 B |

Both runs used `python scripts\benchmark_webcodex_release.py --section runtime --runtime-counts 0,100,128,500` with RSS method `windows-working-set` on Python 3.12.0 / Windows 11 `10.0.28000` / 32 logical CPUs. Raw JSON is saved under `.tmp/agent-integration/RM10/` (gitignored). Each run completed in roughly 56–57 seconds of wall clock for the whole runtime section.

The observed incremental slope was about **15,976.6 B per Runtime** in run 1 (range 13,312–17,408) and **17,871.0 B per Runtime** in run 2 (range 12,328.96–24,283.43). The two runs differ because RSS includes allocator/process noise; the reported range is preferred over a single exact cost. This is only an allocator/process-level estimate; a live upstream client, running command, Codex process, LSP process, or large result store can dominate it.

The 500-full-Runtime measurement is **capacity evidence only**; it does not change the production default of 128 and is not a recommendation to raise it.

## HTTP Session Capacity / Lookup / Soak

The Session-manager benchmark isolates `HTTPSessionManager` lifecycle and lookup using bounded lightweight closable session runtimes. Full Runtime working-set cost is measured separately above; upstream lifecycle/fault behavior is covered by the resilience/fault tests.

### 10× capacity create/delete soak

Configured capacity: **128**.

- Cycles: **1,280 create + DELETE**.
- Final active sessions: **0**.
- `created_total`: **1,280**.
- `deleted_total`: **1,280**.
- Working-set delta across the soak: **+4,096 B**.
- No monotonic Session registry growth was observed.
- A 129th concurrent session was rejected with `http_session_capacity` and `retry_after_seconds=1`.
- An already admitted Session remained available after the excess request was rejected.

### Session-manager working set

| Installed lightweight Sessions | Process working set |
| ---: | ---: |
| 0 | 43,499,520 B |
| 100 | 43,524,096 B |
| 128 | 43,532,288 B |
| 500 | 43,671,552 B |

These are **not** full Runtime RSS points. They demonstrate bounded Session-manager metadata cost through 500 installed records.

### Lease / ordinary lookup latency

Each point contains 200 lease samples against an installed Session.

| Installed Sessions | p50 | p95 | Max |
| ---: | ---: | ---: | ---: |
| 1 | 0.0083 ms | 0.0095 ms | 0.0410 ms |
| 100 | 0.0082 ms | 0.0088 ms | 0.0296 ms |
| 128 | 0.0082 ms | 0.0087 ms | 0.0309 ms |
| 500 | 0.0081 ms | 0.0092 ms | 0.0941 ms |

There is no evidence here of per-request O(N) prune behavior. TTL pruning remains monotonic/LRU and active leases prevent delete/prune/shutdown from closing a Runtime that is in use.

## Agent Session Persistence

Boundary: `AgentSessionService` + SQLite + an in-process benchmark backend + a stable injected repo fingerprint. This measures durable service/database overhead, **not Codex App Server process startup or model latency**.

| Measurement | Samples | p50 | p95 | Max |
| --- | ---: | ---: | ---: | ---: |
| create durable Agent Session | 100 | 25.4661 ms | 31.8921 ms | 38.2356 ms |
| resume durable Agent Session | 25 | 1.1595 ms | 1.2764 ms | 1.2851 ms |
| submit turn + first in-process event | 25 | 16.7199 ms | 21.0983 ms | 23.3269 ms |

- SQLite size after 100 Sessions: **65,536 B**.
- Crude file-size/session estimate at that point: **655.36 B/Session**.

Repo-fingerprint correctness, local/remote HEAD drift, instruction drift, approval, event cursor, and deterministic handoff are covered by the Agent/Operator correctness suites rather than this timing harness.

## Semantic / LSP

Boundary: repository fake-LSP fixture over the production `LspSemanticBackend` JSON-RPC transport.

- First `document_symbols` / process start: **176.9826 ms**.
- Warm query p50: **2.7872 ms**.
- Warm query p95: **4.7597 ms**.
- Warm max: **6.0611 ms**.

Real language servers may have materially different startup and memory costs.

## Remote Runner Transport

Boundary: real loopback HTTP Upgrade + production RFC6455 framing + production `RunnerWebSocketTransport` / `RunnerPeer`, using an in-process fake Runner Runtime for the final Runtime action.

- 100 Runner RPCs: p50 **3.1263 ms**, p95 **6.3878 ms**, max **18.0648 ms**.
- Disconnect/reconnect to a new Runner instance: **34.2647 ms**.
- Control Plane active shutdown is separately tested against a real loopback WebSocket and must complete within the 3-second test bound.

## Fault and Concurrency Evidence

The automated suites cover:

- 502 / 503 / 504 initialization and call faults;
- timeout, disconnect/reset, and unknown-session 404/410;
- auth 401/403 without reconnect loops;
- ambiguous mutating `tools/call` remote call count remains exactly one;
- shared initialization gate across 100 Runtime clients;
- half-open circuit permits one probe;
- deterministic clock/random/sleeper injection;
- close/reinitialize race cannot resurrect a closed client;
- lazy first-use alias initialization and concurrent single-flight;
- Runtime-to-Runtime live client / ResultStore / `Mcp-Session-Id` isolation;
- active request leases during delete/prune/shutdown;
- global/per-identity/initializing admission limits;
- Runner disconnect/reconnect and MCP inventory reconciliation;
- original-Runner close affinity, duplicate close idempotency, wrong principal/workspace/runner rejection;
- orphan close after reconnect and unavailable Runner retryable behavior;
- real RFC6455 Runner authentication and RPC dispatch;
- wrong Runner credential fails before route attachment;
- bounded Control Plane Runner shutdown.

## Capacity Recommendation

Keep the integrated defaults for the release candidate:

- total Streamable HTTP Sessions: **128**;
- per-identity Sessions: **64**;
- simultaneous Session initializations: **16**;
- idle TTL: **3600 seconds**.

Rationale:

1. 128 is exercised by full Runtime working-set measurement and the 10× lifecycle soak.
2. 500 full Runtime objects were successfully measured twice, but the 500 point is capacity evidence only; it is not evidence to raise the total Runtime default to 500.
3. Per-identity 64 preserves fairness under a 128 global cap.
4. Initialization 16 bounds burst cost from project context, upstream initialization, and other Runtime construction work even when Session metadata itself is cheap.

Deployments may tune these values after measuring their actual language servers, upstream MCP servers, commands, Codex processes, and workload mix. Capacity errors are explicit and retryable; increasing a limit is not required to recover from correctly closed Sessions.

## Known Measurement Limits

- Windows working set is process-wide and includes allocator noise.
- The environment did not provide Linux Landlock.
- Real Codex App Server/model latency is not included.
- Real third-party LSP startup is not included.
- No external-network RTT is included in Runner numbers.
- 500 full Runtime objects were measured twice on this Windows validation host only; other platforms and hosts may differ.

No comparative claim against WebCodex, Codex, or another product should be made from these measurements.

## Release Handoff References

- `docs/webcodex-integration-handoffs/integration-remediation/RM10-full-runtime-capacity.md`
- `docs/webcodex-integration-handoffs/integration-remediation/RM12B-linux-posix-evidence.md`
- `docs/webcodex-integration-handoffs/integration-remediation/RM13-final-release-handoff.md`
