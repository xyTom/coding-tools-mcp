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

OAuth collections are `clients`, `grants`, `tokens`, `refresh-families`, `signing-keys`, and `audit`. Supported actions are Client `enable`/`disable`, Grant/Token/Refresh Family `revoke`, and Signing Key `activate`/`retire`/`revoke`.

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
