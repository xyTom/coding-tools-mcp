# WebCodex Runtime Platform Migration and Rollback

## Before upgrading

Stop or quiesce the service, then back up the configured data directory. At minimum preserve:

- `server-settings.json`;
- `agent-sessions.sqlite3`;
- OAuth persistence/database and OAuth secret vault when enabled;
- `transcripts.sqlite3` when chat/import persistence is used;
- server SecretVault files, including Admin/upstream/Runner secret references;
- upstream MCP server configuration.

Use filesystem/database backup procedures appropriate to the deployment. Do not copy a live SQLite file while concurrent writers are active unless using a SQLite-aware backup mechanism.

## Agent Session database

The integrated Agent Session store uses SQLite schema version **1** (`PRAGMA user_version=1`). The store creates the schema when absent and fails closed if the database reports a newer schema version than the running binary understands.

Rollback guidance:

- A pre-Agent version does not use `agent-sessions.sqlite3`; retain the file for a later re-upgrade or archive it.
- Do not downgrade a future database with a higher `user_version` by manually editing the pragma.
- Back up the database before any future schema migration.

## Workspace Catalog migration

Existing local configurations remain valid. New entries may contain:

- `target: "local"` with a local Path root; or
- `target: "runner"`, `runner_id`, and an opaque Runner-local root string.

Old local-only entries are interpreted as local. Local overlap/path checks apply only to local entries. A runner root is never resolved with Control Plane filesystem APIs.

Rollback guidance:

- Older binaries do not understand runner-target Workspaces. Before rolling back, disable/remove runner-target entries or restore the backed-up pre-upgrade `server-settings.json`.
- Keep the remote Workspace data on the Runner; rollback never migrates remote files into the Control Plane.

## Runner enable/disable and credentials

Remote Runner is opt-in:

- Do not configure runner-target Workspaces and do not issue Runner credentials to remain local-only.
- Revoke a Runner credential through the Admin API to prevent new enrollment.
- Removing/disabling a runner-target Workspace must not cause local fallback.

The Runner credential is stored separately from MCP OAuth/static bearer and the Admin token. Preserve the SecretVault when upgrading; revoke rather than exposing a credential during troubleshooting.

## Operator App enable/disable

The Operator App is an additional `/app` surface backed by `/api/app/*`. It does not alter the Core MCP 25-tool catalog.

- An MCP-only client can continue using `/mcp` without opening `/app`.
- Not provisioning Agent/Runner backends leaves those product capabilities unavailable without changing the local MCP tool namespace.
- Admin remains on its separate credential boundary.

## Local MCP-only rollback/safe mode

A local-only deployment can run with:

- only local Workspace entries;
- no Runner credentials/connections;
- no Agent Session use;
- the existing MCP endpoint and Core tool catalog.

The integration keeps `tools.listChanged=false`, immutable Runtime catalog semantics, and the Core 25-tool surface. Phase 21's optional workflow/OpenAPI facade is **not enabled**, so no second MCP surface must be removed during rollback.

## Upstream catalog/session changes

Discovery now produces an immutable `UpstreamCatalogTemplate`; individual Runtime instances create live upstream clients lazily. Live `Mcp-Session-Id`, ResultStore, and mutable clients remain Runtime-local.

Rollback considerations:

- A rollback may return to eager upstream client creation and therefore a different capacity profile.
- Close active MCP Sessions before rollback so remote upstream DELETE can run where supported.
- Do not reuse serialized live upstream session ids across versions.

## Inspect mode

`execution_fs_mode=inspect` is a new orthogonal setting. An older binary may ignore or reject it depending on settings validation. Restore the backed-up settings file or set the mode back to `normal` before rollback.

OS behavior is not portable: Linux Landlock enforcement and Windows capability reporting are intentionally not described as equivalent.

## Rollback checklist

1. Stop accepting new MCP/Operator/Runner traffic.
2. Close active MCP Sessions and allow bounded upstream/Runner close reconciliation where available.
3. Back up current DB/settings/vault files.
4. If downgrading to a version without Runner catalog fields, restore a local-only settings backup.
5. Start the older local MCP-only service and verify initialize/tools-list before re-enabling external access.
6. Keep newer Agent/Transcript databases archived if the older binary does not understand them; do not coerce schema versions.
7. Re-run the older version's own contract/security tests before production use.
