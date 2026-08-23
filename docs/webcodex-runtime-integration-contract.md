# Coding Tools MCP Runtime Integration Contract

This document is the integration-level contract for the Coding Tools MCP Runtime Platform.
It complements the versioned MCP Runtime contract and ADRs; it does not replace
the stable Core MCP tool/schema contract.

## Immutable external contract

- Core MCP tool names, input schemas and annotations remain governed by the
  existing Runtime contract and schema-drift tests.
- `Runtime.initialize()` continues to advertise `tools.listChanged=false`.
- Upstream Broker discovery is frozen into a Runtime catalog template; reconnect
  may repair transport state but cannot mutate published tools.
- HTTP MCP sessions remain authorization-bound, independently closable Runtime
  instances with request leases and bounded admission.

## Lifecycle identities

| Object | Owner | Durable | Meaning |
| --- | --- | --- | --- |
| AgentSession | Control Plane | yes | user-visible durable coding session |
| backend thread | Data Plane | backend-dependent | live/resumable agent execution thread |
| McpRuntimeSession | MCP transport/Data Plane | no | one initialized MCP Runtime snapshot |
| ExecSession | Runtime Data Plane | bounded process lifetime | one spawned command stream |
| Runner Job | Runner Data Plane + reconciliation metadata | bounded | long process reconciled after disconnect |

No lifecycle ID is reused as another lifecycle's identity.

## Ownership boundaries

### Core Runtime

- fixed MCP registry and dispatch;
- workspace-confined file/patch/process/Git behavior;
- Runtime-scoped upstream/result state;
- permission policy and truthful execution capability reporting.

### Control Plane

- Operator identity and authorization projection;
- durable AgentSessionStore and session ownership;
- bounded event cursor/projection and deterministic handoff;
- Workspace catalog and local/Runner host selection;
- Runner registry/routing/reconciliation metadata.

### Workspace Data Plane

- `LocalExecutionBackend` and process execution;
- Codex App Server backend;
- LSP SemanticBackend;
- Structured ValidationBackend;
- local or Runner-owned MCP Runtime and jobs.

### Desktop / Connectivity

- tunnel provider lifecycle;
- safe public-origin/mobile onboarding projection;
- no public no-auth exposure;
- no ownership of Runtime/Runner protocol semantics.

## Workspace representation

Local catalog entries use a real resolved `Path`. Runner entries use:

```text
target = runner
runner_id = <stable runner id>
root = <opaque runner namespace string>
```

Control Plane code must never resolve or stat a Runner root locally. A missing or
offline Runner returns retryable unavailable and never causes local execution.

## Authentication boundaries

- The dedicated Admin token authorizes `/admin/api/*` only. Conversation
  management is part of this authenticated Admin surface.
- MCP bearer/OAuth/noauth authority authenticates `/mcp` as configured and does
  not grant Admin authority.
- Runner credentials authenticate Runner transport only.
- Secrets are never embedded in onboarding URLs, SSE URLs, handoff projections or
  Workspace summaries.

## Agent continuity and handoff

Conversation is the user-visible durable product object; AgentSession remains its
durable execution projection and persists explicit instructions and a bounded
repository fingerprint. MCP continuity uses an exact, privacy-preserving
client-window binding scoped to principal, transport, Workspace/repository, and
never falls back to the most recent Conversation for a credential. Explicit list
and resume tools are the recovery path when exact window identity is lost.
Continuation feedback and handoff output are deterministic projections of
durable evidence, not new model-generated summaries or hidden-context replay;
they exclude Workspace roots, backend thread IDs, credentials and full
transcripts.

## Validation and inspect execution

Structured validation uses fixed recipes and the existing Runtime execution
policy. It never installs dependencies; Cargo/Go validation is offline and Node
tests do not implicitly run package lifecycle scripts.

`execution_fs_mode=inspect` is independent of `permission_mode`. On Linux with
Landlock, the Workspace is read-only while Runtime scratch remains writable. On
unsupported platforms `inspect_enforced=false` is reported explicitly.

## Remote capability rule

`WorkspaceHost` is the only coarse host-selection boundary. Capabilities lacking a
remote Runner implementation return retryable unavailable. No integration layer is
allowed to reinterpret a remote root as a local path or silently fall back to a
local backend.
