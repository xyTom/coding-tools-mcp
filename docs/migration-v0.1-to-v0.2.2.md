# Upgrade and rollback: v0.1 / upstream v0.2.2 to the integrated v0.2.2 runtime

This guide describes the implemented migration boundary. It intentionally does
not restore `docs/profile-v0.1.md` as a current contract. Historical profile
behavior belongs in migration history only; the normative runtime contract is
[Runtime Contract v0.2](runtime-contract-v0.2.md).

## Before upgrading

Stop every server process and make a filesystem-level backup of the complete
stable configuration directory. The directory is selected by
`CODING_TOOLS_MCP_CONFIG_DIR`, or defaults to:

- Windows: `%APPDATA%\coding-tools-mcp`
- POSIX: `$XDG_CONFIG_HOME/coding-tools-mcp` or `~/.config/coding-tools-mcp`

Back up these files together when present:

- `server-settings.json`
- `mcp-servers.json`
- `oauth.sqlite3` plus any SQLite `-wal`/`-shm` files after a clean shutdown
- `oauth-secrets.json`
- `server-secrets.json`
- `transcripts.sqlite3` plus any SQLite sidecars after a clean shutdown

Also preserve the independent `CODING_TOOLS_MCP_SECRETS_KEY`. Do not store the
master key in the same backup archive as the encrypted Vault unless the archive
has its own access controls.

Record the public OAuth origin (`CODING_TOOLS_MCP_SERVER_URL`) and current active
signing-key ID/fingerprint, but never copy secret material into tickets, logs, or
migration notes.

## Stable configuration directory

The integrated runtime reads server identity state from the stable user
configuration directory, not from a managed Workspace. `--workspace` and
`CODING_TOOLS_MCP_WORKSPACE` provide the fallback/default Workspace root; they do
not relocate OAuth or Settings files.

Older fork deployments may have stored settings under a Workspace-specific
`.coding-tools-mcp` directory. There is no silent recursive search for such
files. Review and migrate those values explicitly into the stable directory so
an unrelated Workspace cannot redefine server identity. Do not copy plaintext
secret fields into `server-settings.json`; put secret values into the Secret
Vault and persist only references.

## Settings schema migration

`server-settings.json` is versioned and written atomically. Corrupt JSON or a
future unsupported schema version fails closed and is not overwritten.

### Legacy `tool_profile`

`tool_profile` is accepted only as migration input. On read/write it is removed
and emits the stable warning:

```text
legacy_tool_profile_ignored
```

Known and unknown old values have the same result. There is no `--tool-profile`,
`CODING_TOOLS_MCP_TOOL_PROFILE`, WebUI selector, or runtime filtering path.
Permission modes do not change the fixed tool catalog.

### Legacy single Workspace

A legacy `workspace` value remains a fallback when no `workspace_catalog` is
present. Migrate explicitly through the Admin Workspace page/API to a catalog
containing stable `id`, `name`, `root`, `enabled`, and one default entry. New
HTTP Sessions freeze one catalog Workspace at initialization; ordinary tools
cannot switch roots.

Workspace roots must exist, be directories, be unique, not be filesystem roots,
and not be nested inside one another. Unknown or disabled entries fail closed.

## OAuth migration

The integrated OAuth implementation requires persistent state and the encrypted
Vault:

```text
oauth.sqlite3
oauth-secrets.json
CODING_TOOLS_MCP_SECRETS_KEY
```

The Store persists Clients, Grants, access-token `jti` metadata, refresh-token
families, signing-key metadata, and audit events. It does not persist plaintext
bearer tokens, refresh tokens, client secrets, or signing material.

### Reauthorization expectation

Tokens issued by older stateless implementations may lack required `kid`,
`grant_id`, `workspace_id`, or `jti` state and therefore are not accepted by the
new fail-closed validator. Plan a controlled reauthorization after upgrade.
Persistent dynamic Client registration and refresh rotation then avoid routine
reauthorization across later restarts, provided the public origin, Store, Vault,
Client/Grant state, and signing-key ring remain intact.

### Workspace mapping

- If exactly one Workspace is enabled, old unbound Clients are assigned to that
  sole default.
- If multiple Workspaces are enabled, configure
  `oauth_client_workspace_bindings` or
  `CODING_TOOLS_MCP_OAUTH_WORKSPACE_ID` for a pre-registered Client.
- A Grant copies the Client Workspace at authorization time; changing the Client
  mapping does not rewrite existing Grants.
- Missing or disabled mappings reject Grant/Session creation.

### Refresh and signing keys

Authorization Code exchange returns a refresh token. Each refresh rotates the
opaque token; reuse of an old token revokes the family. Refresh rotation,
replacement, access-token `jti` metadata, family timestamps, and issuance audits
commit in one
SQLite transaction. A persistence/audit failure rolls the whole exchange back,
so the original refresh token remains retryable and no partial replacement is
left behind. Signing-key rotation creates a new active key and retires the
previous key so unexpired access tokens remain verifiable. Emergency key revoke
invalidates tokens associated with that key.

Do not delete retired key material until every token that depends on it has
expired or been revoked. Admin pages expose IDs, status, fingerprints, and
impact counts only—not secret material.

## Gateway migration

Place Gateway configuration in the stable `mcp-servers.json` or provide
`--upstream-config` / `CODING_TOOLS_MCP_UPSTREAM_CONFIG`. Configuration,
enabled state, and allowlists are read before Runtime initialization. Each
Runtime receives an immutable namespaced snapshot.

Saving Gateway configuration only persists a new revision and sets
`restart_required`; it does not reload established Sessions. A local/upstream
namespace collision fails closed. `secret_ref` requires the server Secret Vault
and a valid master key.

## Admin WebUI and token separation

The Admin WebUI at `/admin` requires a dedicated Admin token. An MCP static
bearer or OAuth access token is never promoted to Admin. The browser token is
kept in page memory and sent only in the request header.

Settings/Gateway writes use an expected revision. A stale `409` must be resolved
explicitly; the WebUI preserves the draft and refreshes the persisted revision.

## Desktop separation

The optional Desktop client keeps its profiles and secrets under its own storage
root. Do not copy Desktop secret/profile files into server Settings or connect
them to the server Vault. The Desktop app launches/manages local profiles;
`/admin` manages a running HTTP server.

## Rollback

Do not start an older binary against a configuration directory after the newer
binary has migrated SQLite or settings schemas. Older code may not understand
new columns, key-ring state, Workspace bindings, or secret references.

A safe rollback is a full snapshot rollback:

1. Stop all new and old server processes.
2. Preserve the failed-upgrade directory for diagnosis.
3. Restore `server-settings.json`, both Vault files, `oauth.sqlite3`,
   `mcp-servers.json`, and `transcripts.sqlite3` from the same pre-upgrade
   snapshot.
4. Restore the matching `CODING_TOOLS_MCP_SECRETS_KEY` and public OAuth origin.
5. Start the older binary only against that restored directory.
6. Expect Agents authorized after the snapshot to reauthorize.

Never restore only `oauth.sqlite3` without the matching Vault/key ring, or only a
Vault without the matching Store. Never generate a new master key to “repair” an
existing encrypted Vault.

## Validation checklist

After upgrade:

- `server_info` reports the expected protocol, Workspace, and fixed local tool
  count;
- two HTTP Sessions do not share cwd/process/output state;
- OAuth discovery advertises `authorization_code` and `refresh_token`;
- a refresh succeeds once and replay of the old refresh token fails;
- a disabled Client/Grant/access-token `jti` fails on the next request;
- existing Sessions keep their frozen Workspace, while new Sessions reject a
  disabled Workspace;
- Gateway changes report `restart_required` and do not alter established
  `tools/list`;
- `/admin` rejects ordinary MCP/OAuth bearer credentials;
- Admin responses and logs contain no token or Vault material;
- `npm --prefix webui test`, `docs-required`, and `schema-drift` pass.

## Refresh exchange recovery

Refresh exchange is atomic and does not require a schema migration. If any
replacement-token write, access-token metadata write, family update, or issuance
audit fails, the transaction rolls back and the same original refresh token may
be retried after the persistence problem is corrected. A successfully committed
exchange still consumes the old token, and replay continues to revoke the token
family.
