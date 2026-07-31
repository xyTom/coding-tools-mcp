# Admin API

Phase 08 exposes a backend-only management API under `/admin/api`. The API is disabled unless a dedicated Admin token is configured with `--admin-token`, `CODING_TOOLS_MCP_ADMIN_TOKEN`, or `admin_token_secret_ref` in the server Secret Vault.

Ordinary MCP bearer credentials and OAuth access tokens are never promoted to Admin authority. Management clients authenticate with either:

```http
Authorization: Bearer <dedicated-admin-token>
```

or:

```http
X-Admin-Token: <dedicated-admin-token>
```

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
| GET | `/admin/api/secrets` | List configured Secret Vault names without values. |
| PUT | `/admin/api/secrets/{name}` | Set one Vault value; the value is never returned. |
| DELETE | `/admin/api/secrets/{name}` | Delete one Vault value idempotently. |
| GET | `/admin/api/workspaces` | List the persisted validated Workspace Catalog. |
| POST | `/admin/api/workspaces` | Add a Workspace with `expected_revision`. |
| POST | `/admin/api/workspaces/{id}/disable` | Disable a non-default Workspace. |
| POST | `/admin/api/workspaces/{id}/default` | Select an enabled default Workspace. |
| GET | `/admin/api/workspaces/{id}/check` | Check only a catalog Workspace ID; arbitrary paths are not accepted. |
| GET | `/admin/api/oauth/{collection}` | List redacted Clients, Grants, Tokens, Refresh Families, Signing Keys, or Audit Events. |
| POST | `/admin/api/oauth/{resource}/{id}/{action}` | Perform an exact-ID idempotent OAuth action. |
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

OAuth collections are `clients`, `grants`, `tokens`, `refresh-families`, `signing-keys`, and `audit`. Supported actions are Client `enable`/`disable`, Grant/Token/Refresh Family `revoke`, and Signing Key `activate`/`retire`/`revoke`.

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

## Secret boundary

Responses never include client-secret digests, bearer or refresh token plaintext, signing-key secret references, Vault values, or upstream credentials. Gateway `secret_ref` entries are accepted only when the server Secret Vault is enabled and the reference resolves. Startup and Admin validation fail closed otherwise.

## Chat and session persistence

Conversation lists are summary-only and paginated. Message and context content is returned only by the explicit conversation-detail endpoint. Every request that addresses stored content includes a registered Workspace ID; unknown or disabled Workspaces are rejected.

Codex scan roots are relative to the selected Workspace. The API rejects absolute paths, `..`, and escapes through symlinks or reparse points. Scan requests may set bounded `max_depth`, `max_files`, `max_file_bytes`, `max_total_bytes`, and `max_messages` values. Malformed or partially written JSONL records appear as item errors while other candidates remain available.

All delete and clear routes require the dedicated Admin credential, are idempotent, and return actual affected counts. Ordinary MCP bearer and OAuth credentials do not authorize these routes.

## Admin WebUI

The generated administration interface is served at `/admin`. Its source/build, authentication, stale-revision, redaction, accessibility, and conversation-detail boundaries are documented in `docs/admin-webui.md`.
