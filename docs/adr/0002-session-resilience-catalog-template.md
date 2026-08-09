# ADR 0002: Session Resilience and Upstream Catalog Ownership

- Status: Accepted
- Date: 2026-08-08

## Context

The stable-catalog broker contract requires every published Runtime to expose an
immutable upstream tool snapshot. Eagerly initializing every enabled upstream
client for every Runtime amplifies remote sessions as inbound MCP concurrency
grows. Remote Runner routing adds another lifecycle boundary and makes implicit
local fallback unsafe.

## Decision

The server/config revision owns an immutable `UpstreamCatalogTemplate` containing
only sanitized/frozen tool definitions, catalog/search metadata and client factory
descriptors. It contains no live `Mcp-Session-Id`, principal token, mutable client,
or ResultStore.

Each Runtime owns its immutable view, its ResultStore, and lazily created live
clients/session identifiers. Live session state is not shared across principals,
workspaces, Runtimes or Runners. A server-level resilience coordinator may share
bounded redacted failure throttling, initialization concurrency state, and
read-only aggregate transport telemetry (counts and state labels only). It must
not retain live client/session/transport objects, session identifiers, tokens,
or ResultStores.

Inbound HTTP MCP sessions have explicit global, per-identity and initialization
limits. Active leases prevent delete/prune/shutdown from closing a Runtime that is
in use. Ambiguous mutating calls are never automatically replayed.

Remote MCP close remains affined to the Runner that created the Runtime. An
offline Runner produces retryable unavailable/close-pending state and never a
replacement local Runtime.

## Consequences

- `tools.listChanged=false` remains true.
- Published Runtime catalogs do not drift during transport recovery.
- Remote upstream sessions are created lazily and remain Runtime-scoped.
- Capacity pressure never silently evicts active work.
- Runner close reconciliation retains original-Runner affinity.
