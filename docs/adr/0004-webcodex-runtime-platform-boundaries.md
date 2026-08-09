# ADR 0004: WebCodex Runtime Platform boundaries

- Status: Accepted
- Date: 2026-08-09

## Context

Web/mobile Agent Sessions, local execution, semantic/validation capabilities and
remote Runners add product features around the existing fixed MCP Runtime. The
integration needs explicit ownership boundaries so those features cannot silently
change the stable MCP contract or turn remote namespaces into local filesystem
paths.

## Decision

1. **Control Plane and Data Plane are separate.** Control Plane owns identities,
   durable AgentSession metadata, routing and bounded projections. Workspace file,
   process, Codex, LSP and validation execution belongs to a Workspace Data Plane.
2. **AgentSession, McpRuntimeSession and ExecSession/Job are distinct lifecycles.**
   IDs are not interchangeable and closing one does not implicitly close another.
3. **Operator authorization is distinct from Admin authorization.** Admin tokens
   are never upgraded into ordinary Operator identity. A token accidentally reused
   for both roles fails closed on Operator routes.
4. **Remote workspaces use `WorkspaceHost`, not `RemotePath`.** A Runner root is an
   opaque Runner-namespace string. The Control Plane must not call `Path()`,
   `resolve()`, `is_dir()` or other local filesystem APIs on it.
5. **The Core MCP catalog remains fixed.** Product features are Operator/host
   capabilities and do not silently add tools to the existing 25-tool contract.
6. **Runtime and Broker snapshots remain immutable.** `tools.listChanged=false`
   remains authoritative for an initialized Runtime.
7. **Agent execution state is stored separately from transcripts.** Conversation
   IDs may be referenced, but message bodies are not copied into AgentSession
   metadata.
8. **Agent backend interfaces are product-neutral.** Codex App Server is the first
   implementation of `AgentSessionBackend`, not the domain model itself.
9. **Runner v1 transport is authenticated WebSocket over HTTPS.** QUIC is deferred.
   Runner credentials are distinct from MCP OAuth/bearer and Admin credentials.
   Plain `ws://` is an explicit development exception, not a production
   transport: it is accepted only with `--allow-insecure-ws` and only for
   loopback hosts `127.0.0.1`, `::1`, and `localhost` (local development,
   tests, and the loopback hop in front of a tunnel). Any non-loopback `ws://`
   fails closed even with the flag. The exception is not a public deployment
   recommendation and does not weaken Runner credential requirements.
10. **Integration worktrees and controlled temp state remain inside the project.**
    Production runtime scratch may use the existing external OS temp policy; Agent
    development worktrees/reference clones do not.

## Additional execution decisions

- `Runtime` stays the MCP-session composition root.
- `ExecutionBackend` is local-only; remote execution is selected by
  `WorkspaceHost`, not a `RemoteExecutionBackend` hidden under Runtime.
- `execution_fs_mode=inspect` is orthogonal to `permission_mode`. It is advertised
  as enforced only when the platform isolation primitive is actually active.
- Remote Runner unavailable is retryable unavailable. There is no local fallback.

## Consequences

Features can evolve independently while characterization/schema-drift tests guard
the existing MCP contract. Remote routing must be explicit at composition points,
and any capability that has not gained a Runner RPC implementation must report
unavailable rather than execute locally.
