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
- The integration baseline remains upstream `0.2.2`. Phase 14 assigns the fork
  development release `0.3.0.dev0`; the npm launcher uses the corresponding
  SemVer `0.3.0-dev.0`.

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
- Refresh exchange commits replacement-token creation, old-token consumption,
  family last-used state, access-token metadata, and issuance audits in one
  `BEGIN IMMEDIATE` transaction. Any failure rolls back all of those writes and
  leaves the original refresh token retryable. No OAuth schema migration is
  required for this atomicity fix.

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
- In a multi-Workspace server, each OAuth Client has an Admin-managed allowlist
  containing one or more active Workspace IDs. With several allowed IDs, each
  Authorize request selects the Workspace copied into that new Grant. Replacing
  the allowlist takes effect immediately for later authorizations; the Client is
  not permanently bound to one Workspace, and existing Grants retain their
  stored Workspace identity.
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
- Raw definitions remain server-side diagnostics. Public definitions replace
  `name` with the stable namespace, recursively sanitize untrusted metadata,
  retain supported public schema and real annotations, and receive a public
  schema digest. Containment budgets are widen-only: omitted enum values,
  properties, or non-monotonic combination branches cannot make previously
  legal arguments invalid. The local fake-readonly compatibility override never
  changes effective risk or Broker routing.
- The five Broker tools are fixed local `TOOL_REGISTRY` entries. Search and
  describe use only the current Runtime's frozen catalog. Read-only and mutating
  calls are separate routes; mutating calls require the matching public digest,
  and all Broker calls validate arguments against the public `inputSchema`.
- `expose_mode=direct` is the legacy default. `expose_mode=broker` retains the
  complete filtered catalog and directly exposes only pinned remote names.
  Admin changes are restart-only and do not mutate an existing Runtime.
- `structuredContent`, `content`, and `isError` from a valid upstream
  `tools/call` result are preserved and Broker dispatch does not double-wrap the
  MCP envelope. Content-only upstream results remain content-only, so the two
  passthrough Broker call tools omit `outputSchema`. Missing `content` is
  normalized to an empty array. Oversized Broker results may be stored before
  truncation in a short-lived, per-manager, session-owned ResultStore and paged
  through the fixed fetch tool. Required handle metadata is included inside the
  final 128,000-byte budget calculation and survives every fallback.
- Timeout, disconnect, oversized response, invalid JSON-RPC envelope, invalid
  schema/result shape, HTTP failure, and upstream RPC errors use stable
  structured Gateway errors.
- All MCP, Admin, OAuth DCR, and upstream transport JSON inputs use one
  strict decoder: integers are limited to 4,300 digits and floating-point
  values that overflow to non-finite representations are rejected.
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

## Authenticated Admin API

- The Admin API is enabled only when a dedicated Admin token is configured.
  Ordinary MCP bearer credentials and OAuth access tokens do not imply Admin
  authority. The same HTTP Authorization header may carry the dedicated token,
  but it is compared only with the Admin credential; `X-Admin-Token` is also
  accepted for explicit management clients.
- HTTP handlers authenticate, parse JSON, and dispatch to the Admin service.
  They contain no SQL and do not implement independent Settings, OAuth,
  Workspace, Gateway, Secret Vault, or CORS validation rules.
- Settings responses separate active startup values, persisted values, and the
  exact restart-required field list. Settings and Gateway writes require the
  revision read by the caller; stale revisions return a conflict instead of
  overwriting newer configuration.
- All responses are redacted. Client-secret digests, bearer or refresh token
  material, signing-key secret references, Vault values, and upstream
  credentials are not returned.
- OAuth management addresses Clients, Grants, Access Token JTIs, Refresh
  Families, and Signing Key KIDs by exact ID. Disable/revoke operations are
  idempotent and return an affected count plus the audit event ID when a state
  transition occurred.
- Workspace add/disable/default/check operations reuse the validated Workspace
  Catalog and never accept an arbitrary path for a check request.
- Gateway writes validate and persist configuration only. They set
  `restart_required` and do not start, stop, reload, or mutate any existing
  Runtime or Session snapshot.
- Gateway `secret_ref` values resolve only through the server Secret Vault.
  Missing Vault configuration, an incorrect key, or an unknown reference fails
  closed during startup and Admin validation.
- Allowed origins use `normalize_allowed_origins` for startup, Admin validation,
  persistence, and HTTP request checks.

## Chat, transcript, and Codex session persistence

- Chat conversations, messages, durable context entries, and imported Codex
  sessions are keyed by explicit Workspace ID. Conversation, message, context,
  session, query, cache, and deletion keys never fall back to a global row ID.
- Ordinary callers use a `WorkspaceTranscriptService` fixed to the immutable
  Workspace binding already established for their MCP Session. Cross-Workspace
  listing, import, clear, and deletion remain dedicated Admin operations.
- Codex session roots are relative paths inside a registered Workspace. Absolute
  paths, `..`, symlink/reparse-point escapes, disabled/unknown Workspaces, and
  files outside the selected root fail closed.
- Session scanning enforces depth, file-count, per-file byte, total-byte, and
  message-count limits. Invalid encoding, locked files, truncated JSONL, and
  malformed individual records are represented as per-file or per-line errors
  without failing the entire page.
- List APIs return summaries and bounded pagination. Full message/context body is
  returned only by an explicit Workspace-and-conversation detail request.
- Message, context, conversation, and imported-session deletion uses stable IDs,
  is idempotent, and returns actual affected counts.
- Chat text, transcript paths, commands, model responses, and summaries are not
  added to telemetry or ordinary logs.

## Admin WebUI

- `webui/src/**` is the sole editable frontend source. Packaged files under `coding_tools_mcp/webui_dist/**` are recreated only by the formal build.
- The public `/admin` login shell contains no server data; after token entry it consumes the dedicated-Admin Phase 08/09 API. It does not bypass API authentication, settings/Gateway revisions, Workspace IDs, or conversation pagination.
- The Admin token is kept in page memory only and is never placed in URLs or browser persistent storage.
- No legacy tool-profile UI/state/serialization exists. Safe mode does not claim to hide mutation tools; fake-readonly annotations are presented only as a dangerous non-security compatibility override.
- Stale settings writes preserve the user draft, refresh the persisted revision, and present a conflict instead of silently overwriting. Gateway writes remain restart-only.
- OAuth/Gateway/Vault credential material and references are not displayed. Conversation lists use summaries and full text is fetched only through paginated detail calls.
- Untrusted server/transcript content is rendered through node creation and `textContent`, not `innerHTML`. Destructive actions require an ID-specific confirmation and restore focus.

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
    "compatible": [
      "2025-06-18"
    ]
  },
  "version": {
    "integration": "0.2.2",
    "fork_release": "0.3.0.dev0",
    "npm_launcher": "0.3.0-dev.0"
  },
  "tool_catalog": {
    "strategy": "fixed",
    "source": "coding_tools_mcp.server.TOOL_REGISTRY",
    "legacy_tool_profile_controls_catalog": false,
    "optional_installation_gates": [
      "view_image"
    ],
    "fake_readonly": {
      "security_boundary": false,
      "changes_catalog": false,
      "changes_handlers": false,
      "requires_permission_mode": "dangerous",
      "http_requires_authentication": true,
      "exempt_tools": [
        "upstream_tool_call_mutating"
      ]
    }
  },
  "legacy_tool_profile_migration": {
    "warning_code": "legacy_tool_profile_ignored",
    "persist_on_next_write": false,
    "unknown_value": "ignore_with_warning",
    "cases": [
      {
        "input": {
          "tool_profile": "full"
        },
        "output": {
          "tool_profile": null,
          "catalog": "fixed",
          "warning": "legacy_tool_profile_ignored"
        }
      },
      {
        "input": {
          "tool_profile": "read-only"
        },
        "output": {
          "tool_profile": null,
          "catalog": "fixed",
          "warning": "legacy_tool_profile_ignored"
        }
      },
      {
        "input": {
          "tool_profile": "compat-readonly-all"
        },
        "output": {
          "tool_profile": null,
          "catalog": "fixed",
          "warning": "legacy_tool_profile_ignored"
        }
      }
    ]
  },
  "oauth": {
    "grant_types_source": "coding_tools_mcp.oauth.OAUTH_GRANT_TYPES_SUPPORTED",
    "response_types_source": "coding_tools_mcp.oauth.OAUTH_RESPONSE_TYPES_SUPPORTED",
    "advertised_grant_types": [
      "authorization_code",
      "refresh_token"
    ],
    "advertised_response_types": [
      "code"
    ],
    "authorization_codes": "ephemeral",
    "persistent_store_phase": 4,
    "http_integration_phase": 5,
    "migration": "idempotent_transactional"
  },
  "workspace_binding": {
    "phase": 6,
    "point": "http_initialize_runtime_factory",
    "identity_fields": [
      "client_id",
      "grant_id",
      "workspace_id"
    ],
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
    "schema": "sanitized_public_definition",
    "schema_containment_budget": "widen_only",
    "raw_schema_storage": "server_side_only",
    "annotations": "preserve_real",
    "structured_content": "preserve",
    "content": "normalize_boundary_without_json_assumption",
    "remote_workspace_boundary_claim": false,
    "session_identity_mutation": false,
    "tool_profile_controls": false,
    "stable_catalog_broker_extension": true,
    "expose_modes": [
      "direct",
      "broker"
    ],
    "legacy_missing_expose_mode": "direct",
    "broker_tools": [
      "upstream_tool_search",
      "upstream_tool_describe",
      "upstream_tool_call",
      "upstream_tool_call_mutating",
      "upstream_result_fetch"
    ],
    "broker_tools_fixed_local_catalog": true,
    "broker_public_schema_digest": true,
    "broker_mutating_digest_source": "search_or_describe_not_session_proof",
    "broker_argument_validation": "public_input_schema",
    "broker_passthrough": true,
    "broker_call_output_schema": "omitted_for_passthrough",
    "result_handle_budgeting": "reserved_before_final_size_check",
    "upstream_result_unicode": "utf8_with_surrogate_escape_fallback",
    "upstream_call_after_close_error": "UPSTREAM_NOT_AVAILABLE",
    "catalog_search_index_snapshot": "single_build_immutable",
    "catalog_tokenizer_snapshot": "sealed_immutable_synonym_reverse_and_phrase_maps",
    "upstream_definition_snapshot": "immutable_mapping_sequence_wrappers_with_iterative_mutable_deepcopy_export",
    "schema_assertion_projection": "bounded_by_schema_containment_depth",
    "upstream_discovery_schema_snapshot": "sanitize_then_iterative_freeze_without_recursive_copy",
    "upstream_structured_content": "object_when_present",
    "json_container_depth_semantics": "root_container_is_level_1_scalar_adds_no_level",
    "upstream_result_structure_depth": 64,
    "upstream_result_nesting_error": "UPSTREAM_PROTOCOL_ERROR",
    "upstream_rpc_response_id": "exact_integer_request_id_not_boolean_float_or_string",
    "upstream_rpc_error_code": "required_exact_integer",
    "upstream_error_details_depth": 64,
    "upstream_error_details_depth_scope": "per_untrusted_detail_value_excluding_gateway_and_status_wrappers",
    "upstream_error_details_json": "strict_json_or_bounded_omission",
    "upstream_error_details_overflow": "rpc_protocol_error_or_bounded_omission",
    "upstream_status_error_mapping": "preserve_safe_envelope_bound_details_only",
    "upstream_error_details_shared_container_scope": "cycles_or_shared_within_single_value_omitted_cross_top_level_identity_normalized_independently",
    "upstream_stdio_response_limit_bytes": 1048576,
    "upstream_stdio_oversize_error": "UPSTREAM_RESPONSE_TOO_LARGE",
    "json_schema_equality": "exact_typed_json_value_semantics",
    "unique_items_validation": "canonical_fingerprint_expected_linear",
    "strict_json_input": "utf8_bounded_integer_finite_float_structural_errors_normalized",
    "strict_json_output": "bounded_integer_finite_float_surrogate_escape_fallback",
    "json_encoding": "utf-8_only",
    "mcp_stdio_encoding": "raw_pipe_utf8",
    "mcp_stdio_output_unicode": "utf8_with_surrogate_escape_fallback",
    "upstream_stdio_output_unicode": "utf8_with_surrogate_escape_fallback",
    "json_integer_digit_limit": 4300,
    "json_integer_limit_source": "project_fixed_independent_of_python_global",
    "json_integer_lifecycle": "input_schema_result_and_transport",
    "json_nesting_failure": "parse_or_protocol_error",
    "strict_json_boundaries": [
      "mcp_stdio",
      "mcp_http",
      "admin_http",
      "oauth_dcr",
      "upstream_stdio",
      "upstream_http",
      "upstream_sse"
    ],
    "result_store": "session_owned_ttl_in_memory",
    "dynamic_activation": false
  },
  "admin_api": {
    "phase": 8,
    "authentication": "dedicated_admin_token",
    "ordinary_mcp_bearer_is_admin": false,
    "handler_sql": false,
    "responses_redacted": true,
    "settings_views": [
      "active",
      "persisted",
      "pending_restart"
    ],
    "stale_update": "revision_conflict",
    "gateway_change": "persist_and_restart_only",
    "gateway_dynamic_reload": false,
    "gateway_secret_ref": "server_secret_vault_fail_closed",
    "oauth_actions": "exact_id_idempotent_affected_count_audit_event",
    "workspace_validation": "workspace_catalog",
    "allowed_origins_source": "coding_tools_mcp.settings_definition.normalize_allowed_origins"
  },
  "chat_persistence": {
    "phase": 9,
    "workspace_keyed": true,
    "ordinary_scope": "immutable_workspace_service",
    "global_operations_authentication": "dedicated_admin_token",
    "scan_roots": "registered_workspace_relative_only",
    "scan_limits": [
      "depth",
      "files",
      "file_bytes",
      "total_bytes",
      "messages"
    ],
    "malformed_record": "item_error_continue",
    "list_default": "summary_paginated",
    "full_content": "explicit_detail_only",
    "delete": "stable_id_workspace_keyed_idempotent_affected_count",
    "telemetry_content": false
  },
  "telemetry": {
    "default_policy": "upstream_v0.2.2",
    "change_during_integration": false
  },
  "secret_stores": {
    "server_admin": "server_secret_vault",
    "desktop": "desktop_profile_storage",
    "shared": false
  },
  "webui": {
    "phase": 10,
    "source_root": "webui/src",
    "dist": "build_generated_only",
    "authentication": "dedicated_admin_token",
    "admin_token_storage": "page_memory_only",
    "tool_profile_controls": false,
    "safe_mode_hides_mutation_tools": false,
    "fake_readonly_security_boundary": false,
    "settings_stale_update": "preserve_draft_refresh_revision_conflict",
    "gateway_change": "persist_and_restart_only",
    "gateway_dynamic_reload": false,
    "gateway_new_server_default_expose_mode": "broker",
    "gateway_exposure_report": "aggregate_counts_only",
    "gateway_exposure_preview": "active_aggregate_counts_plus_draft_configuration",
    "gateway_activation_copy": "new_session_runtime_or_service_restart",
    "gateway_list_changed_claim": false,
    "secret_material_displayed": false,
    "conversation_list": "summary_only",
    "conversation_detail": "explicit_paginated",
    "untrusted_rendering": "dom_text_content"
  }
}
```
<!-- integration-contract-json:end -->
