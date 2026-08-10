# Coding Tools MCP Runtime / Runner Troubleshooting

## HTTP Session admission

The Streamable HTTP server uses three independent admission limits. Do not treat all 503 responses as the same problem.

### `http_session_capacity`

The global active/creating Session cap is full. The response includes `retry_after_seconds` / `Retry-After`.

- Close MCP Sessions with `DELETE /mcp` when they are no longer used.
- Existing admitted Sessions remain available; do not delete them merely to service a new request.
- Do not restart the server as the ordinary recovery mechanism.
- Increase the configured global limit only after measuring actual Runtime/process memory and initialization cost.

### `http_session_identity_quota`

One verified identity reached its share of the Session capacity while other identities may still have room.

- Close stale Sessions for that identity.
- Keep per-identity limits below or equal to the global limit.
- Do not share live Sessions across principals to bypass this limit.

### `http_session_initialization_limit`

Too many Runtime initializations are already in progress. This is a burst/backpressure condition.

- Respect `Retry-After` and retry the **new initialize request** later.
- Do not replay an already ambiguous mutating tool call.

## Upstream MCP backoff and circuit state

`UPSTREAM_BACKING_OFF` or circuit-probe errors mean the shared redacted resilience gate is delaying a new connection attempt after 5xx/timeout/disconnect failures.

- Wait for the reported retry window.
- Only transport/session reinitialization is retried automatically.
- The current ambiguous `tools/call`, especially a mutating call, is never replayed automatically.
- 401/403 auth failures enter a suspect/non-looping state; fix credentials/configuration instead of waiting for reconnect churn.
- 404/410 unknown session clears the stale remote session id; only the next call initializes a replacement session.

The frozen Runtime tool catalog is not refreshed during reconnect. A new server/config revision is required to publish a new catalog.

## Runner unavailable

Remote Workspace operations return a retryable Runner-unavailable error when their original Runner is disconnected.

- The Control Plane never resolves the Runner root on its own filesystem.
- There is no local Runtime fallback for a runner-target Workspace.
- Agent, Semantic, Validation, MCP, and workspace fingerprint operations remain bound to the advertised Runner/workspace.
- Reconnect with the same Runner id and appropriate instance semantics. MCP inventory reconciliation restores or marks routes lost; it does not silently bind an unknown process/session.

### MCP close while Runner is offline

The route becomes `close_pending`/unreachable and retains its original Runner affinity. On reconnect, inventory reconciliation closes matching pending/orphan sessions before the route target is discarded. Duplicate DELETE is idempotent.

## Runner credential provisioning and rotation

Runner credentials are distinct from MCP OAuth/static bearer and the Admin token.

Admin-only endpoints:

- `POST /admin/api/runners/{runner_id}/credential`: issue/rotate and return the plaintext credential once for provisioning.
- `GET /admin/api/runners/{runner_id}/credential`: return only configured state and fingerprint.
- `DELETE /admin/api/runners/{runner_id}/credential`: revoke.

The Runner process should receive the credential through `CODING_TOOLS_MCP_RUNNER_CREDENTIAL` or a protected `--credential-file`; there is intentionally no plaintext credential command-line option.

Production Runner connections must use authenticated `wss://`.

- Insecure `ws://` is allowed only for local development, tests, and the loopback hop in front of a tunnel.
- Loopback `ws://` requires the explicit `--allow-insecure-ws` flag.
- Allowed hosts are `127.0.0.1`, `::1`, and `localhost`; non-loopback `ws://` fails closed even with the flag.
- The exception is not a public deployment recommendation and does not weaken Runner credential requirements.

If a credential is revoked, new Runner enrollment fails closed. Rotate the credential through Admin, update the Runner secret source, then reconnect.

## Inspect mode

`execution_fs_mode=inspect` is separate from permission mode. The server reports whether read-only filesystem enforcement is actually active.

- Linux with supported Landlock: Workspace writes are denied while Runtime scratch remains writable.
- Unsupported platforms, including the current Windows validation host: `inspect_enforced=false` is reported. Do not interpret the inspect request as an enforced sandbox there.

Use an external sandbox when OS-level enforcement is unavailable.

## Operator App disconnected state

The browser uses authenticated fetch-based SSE and a durable Agent Session id.

- Stream loss does not mean the durable Agent Session or Runner job was deleted.
- Reopen/reselect the Session to attach from the last bounded cursor.
- Do not resubmit the previous turn merely because the page refreshed.
- Admin credentials are not a fallback Operator credential.

## Diagnostic distinctions

| Symptom | Meaning | Normal action |
| --- | --- | --- |
| HTTP Session capacity/quota | Control Plane admission pressure | close stale Sessions / retry after backoff |
| Upstream backing off | one configured upstream transport is unhealthy | wait/fix upstream; no mutating replay |
| Runner unavailable | remote Data Plane disconnected | reconnect the original Runner |
| remote close pending | close intent retained while Runner unavailable | allow reconciliation on reconnect |
| semantic unavailable | local/remote language server capability unavailable | install/configure supported LSP on the Data Plane |
| validation unavailable | recipe/toolchain capability unavailable | install/configure toolchain; backend does not auto-install |
| inspect not enforced | OS confinement unavailable | use external sandbox / supported Linux Landlock |

These conditions should not require a project-process restart in the normal close/retry path.
