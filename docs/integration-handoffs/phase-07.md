# Phase 07 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `aac8c2dd6c59fed234d5d1574d676942e334d987`
- Implementation commit: `ce2170405f95fb4777235c9ec168c36c1fd63c52`
- Handoff commit: filled by the next agent from `git log`
- Phase 08 remains pending and was not started.

## Scope Completed

- Ported the upstream MCP Gateway composition for Streamable HTTP and stdio servers.
- Added strict Gateway configuration loading from an explicit
  `--upstream-config`, `CODING_TOOLS_MCP_UPSTREAM_CONFIG`, or the stable server
  configuration directory's `mcp-servers.json`.
- Added per-Runtime upstream clients and immutable tool-definition/routing snapshots.
- Appended namespaced upstream tools to the fixed local catalog without changing
  `TOOL_REGISTRY` or any local tool name.
- Preserved upstream schemas, real annotations, content blocks,
  `structuredContent`, and `isError` at the Gateway boundary.
- Added stable structured errors for timeout, disconnect, connection failure,
  HTTP failure, oversized response, invalid JSON/SSE, invalid JSON-RPC envelopes,
  invalid tool definitions/results, and upstream RPC errors.
- Added focused Gateway and integration-contract coverage.

## Immutable Catalog and Namespace Decisions

- Each Runtime discovers upstream tools during Runtime construction and freezes
  the public definitions and routing map for that Runtime's lifetime.
- `tools/list` does not query the remote server after Runtime construction.
- No Gateway start, stop, reload, profile, or notification path mutates the
  snapshot; `listChanged: false` therefore remains truthful.
- Public names use `{alias}__{remote_name}`. Aliases cannot contain `__`; nested
  remote names remain intact.
- Every local `TOOL_REGISTRY` name is permanently reserved. A public-name
  collision fails Runtime construction and closes any partially initialized
  upstream clients.
- A configured upstream that cannot initialize contributes an immutable error
  status and zero tools to that Runtime. It is not dynamically retried into the
  existing Session catalog.

## Schema, Annotation, and Result Boundaries

- Apart from replacing the tool `name` with its stable public namespace, the
  Gateway deep-copies and preserves upstream title, description, `inputSchema`,
  `outputSchema`, annotations, and extension fields.
- The local `--dangerously-fake-readonly-annotations` compatibility switch still
  affects only local definitions. Upstream annotations are never rewritten.
- Valid upstream `content`, `structuredContent`, and `isError` are returned
  directly. Missing content is normalized to an empty array; structured data is
  not serialized into model text as a fallback.
- Upstream RPC errors retain their remote JSON-RPC error object inside stable
  structured Gateway error details.

## Session, Identity, and Security Boundaries

- The control Runtime and every HTTP MCP Session own separate upstream clients.
  stdio owns one Runtime-local Gateway instance.
- Gateway calls do not mutate the established OAuth identity, Workspace binding,
  cwd, process sessions, retained output, project context, or local Runtime
  directories.
- Remote tools are explicitly reported as remote capabilities. Local Workspace
  confinement and local permission gates are not claimed as security boundaries
  for remote data or side effects.
- stdio upstream processes receive a minimal base environment. Additional values
  require explicit Gateway configuration.
- Literal and `env_ref` environment entries are supported. The library-level
  `secret_ref` form requires a supplied resolver; Phase 07 startup does not
  connect one and fails closed for such entries.
- No `tool_profile` parameter, filter, visibility branch, or annotation rewrite
  remains in Gateway or Runtime production paths.

## Files Changed

- `coding_tools_mcp/upstream.py`
- `coding_tools_mcp/server.py`
- `docs/integration-contract-v0.2.2.md`
- `tests/compliance/test_upstream_gateway.py`
- `tests/test_integration_contract_v022.py`

No Admin, WebUI, OAuth Store schema, OAuth grant/refresh/signing logic, desktop,
or local tool-registry definitions were changed.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.compliance.test_upstream_gateway tests.test_integration_contract_v022` | 0 | Ran 22 tests; all passed. Gateway contributes 13 focused tests. |
| `.\.venv\Scripts\python.exe -m unittest tests.test_oauth_store tests.test_oauth_integration tests.test_oauth_refresh tests.test_oauth_signing tests.test_oauth_fail_closed tests.compliance.test_oauth_persistence tests.test_workspace_session_binding tests.test_settings_foundation tests.test_integration_contract_v022 tests.compliance.test_upstream_gateway` | 0 | Ran 65 Phase 03-07 focused tests; all passed. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite mcp-contract` | 0 | Native Windows runner exited 0; 37 bodies skipped by the known `/dev/null` fixture preflight. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite tool-golden` | 0 | Native Windows runner exited 0; 9 bodies skipped by the same known preflight. |
| Ruff over changed source/tests | 0 | All checks passed. |
| `py_compile` over changed source/tests | 0 | All modules compiled. |
| Mypy over `upstream.py` | 0 | No issues found. |
| Mypy over `server.py` with the documented upstream Windows/baseline error codes disabled | 0 | No issues found. |
| `git diff --check` and staged diff check | 0 | No whitespace errors. |
| production `tool_profile` and dynamic lifecycle scan | 0 | Zero production matches. |
| high-risk credential and forbidden-scope scan | 0 | Zero credential matches and zero forbidden files. |
| `server.py` SQL and `TOOL_REGISTRY` definition scan | 0 | No added SQL and no registry-definition changes. |

## Known Platform Baseline

- Native Windows `mcp-contract` and `tool-golden` bodies remain skipped because
  the upstream fixture requires `/dev/null`.
- This is the Phase 01 platform baseline, not a Phase 07 regression.
- Final integrated Linux CI must execute those bodies before release.

## Remaining Risks and Deferred Work

- Gateway configuration is file/CLI/environment based in this phase. Authenticated
  Admin read/write and status APIs belong to Phase 08.
- A remote server may expose different tools to a future Runtime after remote
  changes or recovery. Existing Runtime/Session snapshots remain unchanged.
- Remote capabilities and side effects remain governed by the remote server.
- Gateway Secret Vault resolution is not connected in Phase 07; `secret_ref`
  entries fail closed until a later composition explicitly supplies a resolver.
- The Phase 05 Refresh rotation/access-token issuance cross-transaction
  availability risk remains unchanged and was not touched.

## Phase 08 Preconditions

- Read the Phase 07 contract and this handoff before adding Admin composition.
- Admin may manage persisted Gateway configuration and report error status, but
  must not mutate an already initialized Runtime/Session snapshot.
- Any Gateway configuration change applies only after a new Runtime/server
  lifecycle boundary and must preserve `listChanged: false` for existing Sessions.
- Keep Admin authentication, redaction, stale-update handling, and audit behavior
  within the Phase 08 boundaries.

## Secret Check

- No real credentials, authorization headers, OAuth databases, Vault files, or
  private keys were committed.
- All test values are synthetic canaries.
