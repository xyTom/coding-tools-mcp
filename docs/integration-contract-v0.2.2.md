# v0.2.2 Extension Integration Contract

Status: normative integration decision record for
`integration/upstream-v0.2.2`.

This document fixes the compatibility decisions that later integration phases
must implement. It extends the upstream `0.2.2` runtime contract without
changing upstream product behavior in Phase 02.

## Protocol and version

- The MCP protocol target remains `2025-11-25` with explicit compatibility for
  `2025-06-18`.
- Negotiation continues to accept only the versions listed by
  `coding_tools_mcp.protocol.SUPPORTED_PROTOCOL_VERSIONS`; dates are not
  compared lexicographically.
- The package version remains `0.2.2` during integration. A fork release version
  is a release-phase decision.

## Fixed tool catalog

- `coding_tools_mcp.server.TOOL_REGISTRY` remains the single source for local
  tools and the permanently reserved local names.
- Every client sees the same deterministic local catalog for a given
  installation. Explicit installation capability gates such as optional
  `view_image` support may remove their own tool, but persisted profiles must
  not filter the list. Phase 07 may append a namespaced upstream snapshot fixed
  at Runtime initialization; it never replaces or renames local tools.
- The legacy values `full`, `read-only`, and `compat-readonly-all` are accepted
  only as migration inputs. They do not control `tools/list` and are omitted on
  the next successful settings write.
- Unknown legacy `tool_profile` values are ignored with the same migration
  warning rather than being reinterpreted as a security policy.

`--dangerously-fake-readonly-annotations` is an annotation compatibility
override, not a security feature. It does not hide tools, change schemas, block
handlers, or prevent mutation. The upstream requirements remain: dangerous
permission mode is mandatory, and HTTP use requires authentication. UI copy
must never describe this switch as safe or genuinely read-only.

## OAuth protocol and persistence

- `coding_tools_mcp.oauth.OAUTH_GRANT_TYPES_SUPPORTED` is the single source for
  authorization-server metadata and dynamic-client-registration narrowing.
- `coding_tools_mcp.oauth.OAUTH_RESPONSE_TYPES_SUPPORTED` is the corresponding
  response-type source.
- Phase 05 advertises `authorization_code` and `refresh_token` from the shared
  grant-type constant after both token-endpoint branches and rotation/reuse
  tests are complete. Response type `code` remains the only supported response.
- Phase 04 introduces a persistent, transactional OAuth Store in the stable
  user configuration directory. It must persist clients, grants, access-token
  metadata, refresh-token families and hashes, signing-key metadata, and audit
  events. Authorization codes remain short-lived and process-local.
- Store migrations are forward-only, idempotent, and transactional. Reopening
  the database must preserve data; a failed schema migration must not leave a
  half-migrated database. Bearer and refresh token plaintext must never be
  stored.
- Phase 05 adapts the upstream registry and handlers to that store. Store
  unavailability fails closed; it must not silently fall back to a permissive
  process-local registry.

## Agent to Workspace binding

- The binding point is HTTP initialization/runtime creation, after the
  authenticated OAuth identity (`client_id` and, when available, `grant_id`) is
  known and before project context or tools are exposed.
- The mapping resolves to one Workspace ID and constructs the session Runtime
  with that Workspace adapter. The binding is immutable for the lifetime of the
  MCP HTTP session.
- Ordinary MCP tools do not switch Workspace roots. Administrative mapping
  changes apply only to new sessions. Existing sessions retain their frozen
  Runtime and root until closed; new sessions targeting a missing or disabled
  Workspace fail closed rather than falling back to another root.
- stdio keeps one explicit default Workspace because it has no OAuth Agent
  identity.

## Upstream MCP Gateway

- Gateway configuration, server enable state, and include/exclude allowlists are
  parsed before a Runtime is created. The default configuration file is
  `mcp-servers.json` in the stable server configuration directory; an explicit
  `--upstream-config` or `CODING_TOOLS_MCP_UPSTREAM_CONFIG` path is also
  supported.
- Each Runtime owns independent upstream clients. It discovers tools during
  Runtime initialization and freezes the resulting definitions and routing map
  for that Runtime's lifetime. No start, stop, reload, profile, or notification
  path mutates `tools/list`, so `listChanged: false` remains truthful.
- Public names use the stable form `{alias}__{remote_name}`. `__` is reserved in
  aliases, nested remote names are preserved, every local `TOOL_REGISTRY` name
  remains reserved, and any public-name collision fails Runtime creation.
- Apart from replacing `name` with its public namespace, upstream tool
  definitions retain their original title, description, `inputSchema`,
  `outputSchema`, and real annotations. The local fake-readonly compatibility
  override never rewrites upstream annotations.
- `structuredContent`, `content`, and `isError` from a valid upstream
  `tools/call` result are preserved. Missing `content` is normalized to an empty
  array; structured data is never serialized into model text merely to fill the
  content field.
- Timeout, disconnect, oversized response, invalid JSON-RPC envelope, invalid
  schema/result shape, HTTP failure, and upstream RPC errors use stable
  structured Gateway errors.
- Gateway tools are remote capabilities. Local Workspace path confinement and
  local permission gates describe local tools only; they are not claimed as a
  security boundary for a remote server. The remote server controls its own
  data and side effects. A stdio upstream receives only a minimal process
  environment; additional variables require explicit Gateway configuration
  through literal values or `env_ref`. The library-level `secret_ref` form is
  accepted only when composition supplies a secret resolver; Phase 07 startup
  does not connect one and therefore fails closed for such entries.
- Calling a Gateway tool cannot alter the Runtime's OAuth identity, Workspace
  binding, cwd, local process sessions, retained output, or project context.
  Different MCP Sessions do not share upstream client/session state.
- Legacy `tool_profile` values remain settings-migration inputs only. Gateway
  configuration, discovery, visibility, routing, and annotations contain no
  `tool_profile` control path.

## Telemetry and secret-store boundaries

- Integration preserves the upstream v0.2.2 telemetry default: anonymous
  telemetry is enabled unless disabled by the existing environment controls,
  `DO_NOT_TRACK`, or CI behavior. Changing that default is a separate product
  decision.
- Telemetry must not gain Workspace IDs, Agent IDs, paths, commands, file
  contents, OAuth identifiers, or secret material during integration.
- Server Admin settings use the server Secret Vault introduced in Phase 03.
  Desktop profiles keep their existing desktop-local storage. The two stores
  are not unified during this integration, and neither side may silently read
  the other's secrets.

## Machine-readable decision table

The following block is consumed by the Phase 02 contract tests.

<!-- integration-contract-json:start -->
```json
{
  "schema_version": 1,
  "protocol": {
    "target": "2025-11-25",
    "compatible": ["2025-06-18"]
  },
  "version": {
    "integration": "0.2.2"
  },
  "tool_catalog": {
    "strategy": "fixed",
    "source": "coding_tools_mcp.server.TOOL_REGISTRY",
    "legacy_tool_profile_controls_catalog": false,
    "optional_installation_gates": ["view_image"],
    "fake_readonly": {
      "security_boundary": false,
      "changes_catalog": false,
      "changes_handlers": false,
      "requires_permission_mode": "dangerous",
      "http_requires_authentication": true
    }
  },
  "legacy_tool_profile_migration": {
    "warning_code": "legacy_tool_profile_ignored",
    "persist_on_next_write": false,
    "unknown_value": "ignore_with_warning",
    "cases": [
      {
        "input": {"tool_profile": "full"},
        "output": {"tool_profile": null, "catalog": "fixed", "warning": "legacy_tool_profile_ignored"}
      },
      {
        "input": {"tool_profile": "read-only"},
        "output": {"tool_profile": null, "catalog": "fixed", "warning": "legacy_tool_profile_ignored"}
      },
      {
        "input": {"tool_profile": "compat-readonly-all"},
        "output": {"tool_profile": null, "catalog": "fixed", "warning": "legacy_tool_profile_ignored"}
      }
    ]
  },
  "oauth": {
    "grant_types_source": "coding_tools_mcp.oauth.OAUTH_GRANT_TYPES_SUPPORTED",
    "response_types_source": "coding_tools_mcp.oauth.OAUTH_RESPONSE_TYPES_SUPPORTED",
    "advertised_grant_types": ["authorization_code", "refresh_token"],
    "advertised_response_types": ["code"],
    "authorization_codes": "ephemeral",
    "persistent_store_phase": 4,
    "http_integration_phase": 5,
    "migration": "idempotent_transactional"
  },
  "workspace_binding": {
    "phase": 6,
    "point": "http_initialize_runtime_factory",
    "identity_fields": ["client_id", "grant_id", "workspace_id"],
    "immutable_per_session": true,
    "ordinary_tool_switching": false,
    "invalid_mapping": "fail_closed",
    "disabled_workspace_new_session": "fail_closed",
    "disabled_workspace_existing_session": "retain_frozen_binding_until_close",
    "stdio_binding": "default_workspace"
  },
  "gateway": {
    "phase": 7,
    "namespace": "{alias}__{remote_name}",
    "local_names_reserved": true,
    "collision": "fail_closed",
    "snapshot_point": "runtime_initialize",
    "immutable_per_runtime": true,
    "list_changed": false,
    "config_before_initialize": true,
    "schema": "preserve_except_public_name",
    "annotations": "preserve_real",
    "structured_content": "preserve",
    "content": "normalize_boundary_without_json_assumption",
    "remote_workspace_boundary_claim": false,
    "session_identity_mutation": false,
    "tool_profile_controls": false
  },
  "telemetry": {
    "default_policy": "upstream_v0.2.2",
    "change_during_integration": false
  },
  "secret_stores": {
    "server_admin": "server_secret_vault",
    "desktop": "desktop_profile_storage",
    "shared": false
  }
}
```
<!-- integration-contract-json:end -->
