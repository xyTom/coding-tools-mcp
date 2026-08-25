# Admin API

Phase 08 exposes a backend-only management API under `/admin/api`. The API is disabled unless a dedicated Admin token is configured with `--admin-token`, `CODING_TOOLS_MCP_ADMIN_TOKEN`, or `admin_token_secret_ref` in the server Secret Vault.

Ordinary MCP bearer credentials and OAuth access tokens are never promoted to Admin authority. A browser exchanges a dedicated Admin token once at `POST /admin/api/session` for a server-side, HttpOnly Admin session and uses its CSRF token on writes. Non-browser management clients may use `Authorization: Bearer <dedicated-admin-token>` or `X-Admin-Token: <dedicated-admin-token>`.

All responses use `Cache-Control: no-store`, apply the same validated allowed-origin policy as the MCP endpoint, and redact secret material.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin/api/status` | Backend capability status. |
| GET | `/admin/api/settings` | Active, persisted, and pending-restart settings. |
| POST | `/admin/api/settings/validate` | Validate settings without writing. |
| PUT | `/admin/api/settings` | Save settings with `expected_revision`. |
| GET | `/admin/api/gateway` | Active status plus redacted persisted Gateway config. |
| PUT | `/admin/api/gateway` | Persist Gateway config with `expected_revision`; restart only. |
| PUT | `/admin/api/gateway/servers/{alias}` | Create or patch one server with `expected_revision`; hidden credential fields are preserved. |
| DELETE | `/admin/api/gateway/servers/{alias}` | Remove one persisted server with `expected_revision`; current Runtime is unchanged. |
| GET | `/admin/api/secrets` | List configured Secret Vault names without values. |
| PUT | `/admin/api/secrets/{name}` | Set one Vault value; the value is never returned. The reserved name `oauth/authorization-password` rotates the running OAuth authorization-page password immediately. |
| DELETE | `/admin/api/secrets/{name}` | Delete one Vault value idempotently. |
| GET | `/admin/api/workspaces` | List the persisted validated Workspace Catalog. |
| POST | `/admin/api/workspaces` | Add a Workspace with `expected_revision`. |
| POST | `/admin/api/workspaces/{id}/disable` | Disable a non-default Workspace. |
| POST | `/admin/api/workspaces/{id}/default` | Select an enabled default Workspace. |
| GET | `/admin/api/workspaces/{id}/check` | Check only a catalog Workspace ID; arbitrary paths are not accepted. |
| POST | `/admin/api/runners/{runner_id}/credential` | Issue or rotate a Runner credential; plaintext is returned only in this response. |
| GET | `/admin/api/runners/{runner_id}/credential` | Return configured state and a non-secret fingerprint only. |
| DELETE | `/admin/api/runners/{runner_id}/credential` | Revoke the Runner credential idempotently. |
| GET | `/admin/api/oauth/{collection}` | List redacted Clients, Grants, Tokens, Refresh Families, Signing Keys, or Audit Events. |
| POST | `/admin/api/oauth/{resource}/{id}/{action}` | Perform an exact-ID idempotent OAuth action. |
| PUT | `/admin/api/oauth/clients/{client_id}/authorization-password` | Set or rotate one Client's dedicated Authorize password; takes effect immediately. |
| DELETE | `/admin/api/oauth/clients/{client_id}/authorization-password` | Remove the Client override and immediately fall back to the global Authorize password. |
| PUT | `/admin/api/oauth/clients/{client_id}/workspaces` | Replace an active Client's Workspace allowlist using `workspace_ids`; subsequent OAuth authorizations use it immediately. |
| GET | `/admin/api/chat/conversations` | Paginated conversation summaries; optional registered `workspace_id`. |
| GET | `/admin/api/chat/conversations/{workspace_id}/{conversation_id}` | Explicit paginated message/context detail. |
| POST | `/admin/api/chat/conversations/{workspace_id}/{conversation_id}/messages` | Record messages in one registered Workspace. |
| POST | `/admin/api/chat/conversations/{workspace_id}/{conversation_id}/context` | Record durable context in one registered Workspace. |
| DELETE | `/admin/api/chat/messages/{workspace_id}/{message_id}` | Stable-ID idempotent message deletion. |
| DELETE | `/admin/api/chat/context/{workspace_id}/{context_id}` | Stable-ID idempotent context deletion. |
| DELETE | `/admin/api/chat/conversations/{workspace_id}/{conversation_id}` | Delete one Workspace-scoped conversation. |
| POST | `/admin/api/chat/workspaces/{workspace_id}/clear` | Clear chat/session persistence for one registered Workspace. |
| POST | `/admin/api/codex/sessions/scan` | Bounded scan of relative roots inside a registered Workspace. |
| POST | `/admin/api/codex/sessions/import` | Import explicitly selected candidate IDs. |
| GET | `/admin/api/codex/sessions` | Paginated imported-session summaries. |
| DELETE | `/admin/api/codex/sessions/{workspace_id}/{session_id}` | Stable-ID idempotent imported-session deletion. |
| GET | `/admin/api/conversations` | Unified Conversation Center summaries with bounded progress counts. |
| POST | `/admin/api/conversations` | Create a Conversation in a registered Workspace. |
| GET | `/admin/api/conversations/{workspace_id}/{conversation_id}` | Unified Conversation, execution, message, and context projection. |
| GET | `/admin/api/conversations/{workspace_id}/{conversation_id}/executions` | List executions attached to one Conversation. |
| POST | `/admin/api/conversations/{workspace_id}/{conversation_id}/executions` | Start an Agent execution for a Conversation. |
| POST | `/admin/api/conversations/{workspace_id}/{conversation_id}/turns` | Send a turn to the selected execution. |
| POST | `/admin/api/conversations/{workspace_id}/{conversation_id}/resume` | Resume a detached or recoverable execution. |
| POST | `/admin/api/conversations/{workspace_id}/{conversation_id}/close` | Close the selected execution. |
| POST | `/admin/api/conversations/{workspace_id}/{conversation_id}/approvals/{approval_id}` | Approve, deny, or cancel a pending approval. |
| POST | `/admin/api/conversations/{workspace_id}/{conversation_id}/validation` | Run a structured validation recipe and record bounded evidence. |
| GET | `/admin/api/conversations/{workspace_id}/{conversation_id}/continuation` | Read the deterministic latest-attempt continuation projection. |
| GET | `/admin/api/conversations/{workspace_id}/{conversation_id}/handoff` | Read the bounded handoff brief. |

OAuth collections are `clients`, `grants`, `tokens`, `refresh-families`, `signing-keys`, and `audit`. Supported actions are Client `enable`/`disable`, Grant/Token/Refresh Family `revoke`, and Signing Key `activate`/`retire`/`revoke`.

Runner credentials authenticate `/runner/ws` only. They are distinct from
ordinary MCP/OAuth credentials and the Admin token. Provision the one-time
plaintext through `CODING_TOOLS_MCP_RUNNER_CREDENTIAL` or a protected Runner
credential file; it is intentionally not accepted as a command-line value. See
the [Coding Tools MCP Agent and Runner guide](coding-tools-mcp-agent-runner-guide.md)
for the complete setup flow.

## Telemetry status

`GET /admin/api/status` reports the effective upstream telemetry mode and the documentation entry only:

```json
{
  "telemetry": {
    "mode": "on",
    "docs": "docs/telemetry.md"
  }
}
```

`mode` is `on`, `off`, or `debug` according to the same environment controls used by the runtime. The status response does not add paths, Workspace/Agent/Client IDs, commands, arguments, file contents, or telemetry event data. Phase 11 does not change the upstream v0.2.2 default policy; see `docs/telemetry.md` for the complete privacy schema and opt-out controls.

## Revision writes

Settings and Gateway writes require the revision returned by the corresponding GET response:

```json
{
  "expected_revision": "<sha256 revision>",
  "updates": {
    "port": 9000
  }
}
```

A stale revision returns HTTP 409 with `stale_revision`. The server does not merge an old page over newer persisted state.

## Restart semantics

Settings responses distinguish:

- `active`: the immutable startup snapshot currently in use.
- `persisted`: the latest saved configuration.
- `pending_restart`: fields whose persisted values differ from active values.

Gateway writes only update `mcp-servers.json`; they never start, stop, or reload an upstream server. Existing Runtime and Session tool snapshots remain unchanged and `restart_required` becomes true.

The per-server routes back the normal WebUI form and management cards. `enabled`
means “load for a newly constructed Runtime / after service restart,” not an
immediate lifecycle action. `expose_mode=broker` keeps filtered tools in the
Broker catalog and directly exposes only pinned tools; `direct` directly exposes
the complete filtered set.

## Secret boundary

Responses never include client-secret digests, bearer or refresh token plaintext, signing-key secret references, Vault values, or upstream credentials. Gateway `secret_ref` entries are accepted only when the server Secret Vault is enabled and the reference resolves. Startup and Admin validation fail closed otherwise.

`oauth/authorization-password` is a reserved Secret Vault name. A successful
`PUT` persists the replacement in the encrypted OAuth Vault and swaps the
thread-safe password used by the running `/oauth/authorize` handler before the
response returns. The old password therefore stops working immediately; access
tokens, refresh tokens, Grants, and signing keys are unchanged. This active
secret cannot be deleted through the Admin API and must be replaced instead.
Names containing `/` are sent as URL-encoded path segments.

OAuth Client records expose only an `authorize_login` status object with
`mode=client|global` and a boolean `configured`; no password or Vault reference
is returned. A Client-specific password is encrypted under a deterministic,
non-reversible Vault reference derived from the exact `client_id`. The
authorization handler checks the dedicated Client password first and falls back
to `oauth/authorization-password` when no override is configured. Rotating or
resetting an override is immediate and does not revoke Grants, access tokens,
refresh tokens, or signing keys.

OAuth Client records also expose a redacted `workspace_access` status containing
only allowed Workspace IDs. In a multi-Workspace server, dynamic registration
remains fail-closed and may create a Client with an empty allowlist. A correct
Authorize password for such a Client returns an actionable `409`. An Admin can
`PUT` `{"workspace_ids":["a","b"]}` to replace the allowlist without a restart.
When more than one Workspace is allowed, the Authorize page requires one choice
for that Grant. Later authorizations by the same Client may choose another
allowed Workspace. Existing Grants and Tokens retain their stored Workspace.

## Chat and session persistence

The legacy `/admin/api/chat/*` routes remain for transcript import and maintenance. Conversation Center uses the unified `/admin/api/conversations/*` routes instead. Conversation lists are summary-only and paginated. Message and context content is returned only by an explicit detail request. Every request that addresses stored content includes a registered Workspace ID; unknown or disabled Workspaces are rejected.

Admin execution actions operate through the existing Agent Session ownership boundary without rewriting historical owners. A privileged Admin action still requires the Workspace to be registered and authorized; it does not make an ambiguous historical Conversation claimable by an MCP principal.

`GET /admin/api/conversations` returns a bounded `progress` object on every
summary. It is projected from the same current-attempt evidence as the detail
route and contains changed/explored path counts, validation status, unresolved
failure count, active job count, pending approval count, and explicit
truncation/overflow indicators. An Admin may therefore inspect transcript-only,
zero-message, historical non-Admin-owned, and multi-execution Conversations
without changing durable ownership.

The detail route accepts independent `message_page`, `message_page_size`,
`context_page`, and `context_page_size` query parameters. Each paginated
collection includes its requested page, bounded page size, total, and navigation
state; changing one collection's page does not alter the other. Conversation
actions refresh the selected detail and its list summary after execution create,
turn, resume, close, approval, and validation responses.

Approval decisions require an explicit `session_id` in the request body. Pending
approval projection includes that owning Agent Session ID, and the server rejects
a missing, cross-Workspace, cross-Conversation, or non-pending Session/approval
pair. It never routes an approval to the latest execution merely because it is
newer. Projection failures remain structured Admin errors so the browser can
render a bounded retry action rather than treating missing progress as zero.

Codex scan roots are relative to the selected Workspace. The API rejects absolute paths, `..`, and escapes through symlinks or reparse points. Scan requests may set bounded `max_depth`, `max_files`, `max_file_bytes`, `max_total_bytes`, and `max_messages` values. Malformed or partially written JSONL records appear as item errors while other candidates remain available.

All delete and clear routes require the dedicated Admin credential, are idempotent, and return actual affected counts. Ordinary MCP bearer and OAuth credentials do not authorize these routes.

## Admin WebUI

The generated administration interface is served at `/admin`. Its source/build, authentication, stale-revision, redaction, accessibility, and conversation-detail boundaries are documented in `docs/admin-webui.md`.
