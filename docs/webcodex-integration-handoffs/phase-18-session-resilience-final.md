# Phase 18 / Session Resilience Final Integration Handoff

## Status

- Result: complete; RM12 aggregate and audit gates passed, and RM13 issued the final release handoff
- Branch: `integration/webcodex-runtime-platform`
- Canonical worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Baseline HEAD before integrated working-tree changes: `252c2f0b5508e6b6c23a330e3417943960249eca`
- Source Runner workstream included commits through `dac186e`
- Canonical implementation commit: `d8c1cc855f71303a525da288a6c8e895a76a10b8`.

## Session / Upstream State Changes

### Inbound HTTP Sessions

- Configurable global capacity, per-identity capacity, idle TTL, and initialization concurrency.
- Active request lease prevents delete/prune/shutdown from closing an in-use Runtime.
- Monotonic idle/LRU bookkeeping avoids full-table per-request prune.
- Capacity rejection is structured and carries retry guidance.
- Close operations perform Runtime I/O outside the manager global lock.

### HTTP upstream client

- Explicit transport states: new, ready, backing-off, suspect, closed.
- Bounded shared initialization gate and failure circuit/backoff.
- 404/410 invalid session clears the stale remote session id; a later call reinitializes.
- 401/403 does not enter an automatic reconnect loop.
- HTTP DELETE is bounded/idempotent; 404/410 is treated as already closed.
- Remote close failure does not prevent local resource release.

### Mutating replay answer

**No.** There is no automatic replay of the current ambiguous `tools/call`. 502/503/504, timeout, reset, or disconnect after possible execution records the failure/backoff state but leaves the current call count at one. Reinitialization only affects a later call.

## Immutable Upstream Catalog

- One server/config revision produces an immutable `UpstreamCatalogTemplate`.
- Runtime construction copies frozen catalog/search metadata without opening every live upstream client.
- First use initializes only the requested alias.
- Concurrent first-use for one Runtime/alias is single-flight.
- Live client, ResultStore, bearer/session state, and `Mcp-Session-Id` are never shared across Runtime/principal boundaries.
- Runtime reconnect cannot change the published tool catalog; `listChanged=false` remains truthful.

## Remote Runner Integration

### Workspace / Data Plane

- Workspace Catalog supports `target=local|runner` and optional `runner_id`.
- Runner root remains opaque string data in the Control Plane.
- Local Runtime/Admin filesystem helpers fail closed for runner-target Workspaces.
- No local fallback exists when the original Runner is unavailable.

### Production transport

- Runner initiates an outbound `wss://` WebSocket to `/runner/ws`.
- A small stdlib RFC6455 framing layer avoids introducing an additional WebSocket dependency.
- The first Runner frame is authenticated with a Runner-specific credential, separate from MCP OAuth/static bearer and Admin credentials.
- `RunnerPeer` receives `rpc_request`, dispatches through `RunnerMcpRouter`, and returns `rpc_response`.
- Control Plane synchronous HTTP/Operator code schedules Runner RPC on the transport's owner event loop; it never `asyncio.run()`s a WebSocket transport on another loop.
- Credential provisioning/rotation/revoke is Admin-only. GET returns fingerprint metadata; the plaintext credential is returned only by the issue/rotate response.

### Routed capabilities

- Agent backend lifecycle / bounded events.
- Semantic LSP operations.
- Structured Validation.
- Bounded workspace fingerprint metadata for cross-window stale-context detection.
- Remote MCP Runtime/session creation, call, DELETE/close.

The route/capability protocols are coarse. There is no remote filesystem object API and no `RemotePath` abstraction.

### Close / reconnect

- MCP DELETE is routed to the Runner that created the remote Runtime.
- Duplicate DELETE is idempotent.
- Wrong principal/workspace/runner is rejected before remote close.
- Offline close stays pending/unreachable instead of creating a replacement local Runtime.
- Reconnect inventory reconciliation restores matching routes or marks mismatches/lost sessions; orphans/pending closes are closed on the original Runner.
- Runner shutdown propagates through Runtime close, including upstream DELETE.
- Control Plane server shutdown proactively closes attached Runner WebSockets with a bounded synchronous bridge.

## Fault / Capacity Validation

Automated suites cover:

- 502/503/504, timeout, disconnect/reset, 404/410;
- auth failures without reconnect loops;
- one-call ambiguous mutation guarantee;
- half-open circuit single probe;
- 100-client shared initialization gate;
- Runtime/client isolation and lazy catalog;
- lease/delete/prune/shutdown races;
- Runner disconnect/reconnect storm and inventory reconciliation;
- original Runner close affinity;
- real loopback RFC6455 authentication/RPC/invalid credential/bounded shutdown.

The benchmark report records:

- 1,280 create+DELETE cycles at a 128 configured capacity with `created_total=deleted_total=1280`, active=0, and +4 KiB observed working-set delta;
- flat Session lease p95 around 0.009 ms through 500 installed lightweight Session records;
- full Runtime working-set points at 0/100/128/500 active Runtimes, measured twice by RM10 (capacity evidence only);
- Runner loopback RPC p50/p95 and reconnect time;
- Agent persistence and fixture LSP timing boundaries.

See `docs/webcodex-session-resilience-benchmark-report.md`.

## Capacity Defaults

Release recommendation remains:

- total HTTP Sessions: 128;
- per identity: 64;
- concurrent initialization: 16;
- idle TTL: 3600 seconds.

The 500 Session-manager metadata point remains lightweight and is not evidence to raise the full Runtime cap. RM10 measured 500 full Runtime objects twice on the Windows validation host via `scripts/benchmark_webcodex_release.py`; that point is capacity evidence only and does not change the production default of 128.

## Secrets / Logging Review

- Runner credentials use a separate SecretVault namespace and fingerprint projection.
- Operator API cannot use Admin credential as fallback.
- Runner credential is not accepted as MCP credential.
- Route status does not expose auth digest, remote MCP Session id, or plaintext Runner credential.
- Remote workspace fingerprint does not expose remote root/file contents.
- Tunnel logging redacts secrets.

## Known Recovery Paths

Capacity/identity/initialization pressure returns explicit JSON-RPC backpressure with retry guidance; active request leases block delete/prune/shutdown from closing in-use Runtimes; stale idle Sessions can be closed to reclaim capacity; upstream failures back off without automatic reconnect loops; unavailable Runner routes recover through authenticated reconnect plus inventory reconciliation. RM12 aggregate and audit evidence for these recovery paths passed; recovery remains bounded and does not imply an unconditional restart guarantee.

## Evidence / Documentation

- `docs/adr/0002-session-resilience-catalog-template.md`
- `docs/adr/0004-webcodex-runtime-platform-boundaries.md`
- `docs/webcodex-session-resilience-benchmark-report.md`
- `docs/webcodex-runner-troubleshooting.md`
- `docs/webcodex-migration-rollback.md`
- `docs/webcodex-integration-handoffs/phase-18-session-resilience-retrospective.md` (real RS00–RS07 provenance)
- `docs/webcodex-integration-handoffs/integration-remediation/RM12B-linux-posix-evidence.md`
- `docs/webcodex-integration-handoffs/integration-remediation/RM13-final-release-handoff.md`
- tests: HTTP session resilience, upstream resilience/lazy catalog, Runner remote/MCP/capabilities/WebSocket, WorkspaceHost/binding.

## Remaining Deployment Limit

The standalone Runner CLI currently composes its local Core Runtime without a separate Runner-side upstream-config CLI option. Remote Core MCP, Agent, Semantic, Validation, fingerprint, and close/reconnect paths are implemented; deployments that need a Runner-specific upstream gateway configuration should treat that as a later explicit configuration feature rather than assuming Control Plane upstream configuration is copied remotely.
