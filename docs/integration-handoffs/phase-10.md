# Phase 10 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `d629288b8d03d8e729922fff8508f65970600642`
- Implementation commit: `100d26788c062e88c6a4b9a5f89b45194fee02d1`
- Handoff commit: filled by the next agent from `git log`
- Phase 11 remains pending and was not started.

## Scope Completed

- Added a modular authenticated Admin WebUI that consumes the Phase 08/09 `/admin/api` contract.
- Established `webui/src/**` as the only editable frontend source.
- Added a deterministic dependency-free Node build that removes and recreates `coding_tools_mcp/webui_dist/**`.
- Generated one self-contained packaged `coding_tools_mcp/webui_dist/admin.html` and served it through the existing authenticated `/admin` route.
- Added state-model, security-model, DOM interaction, Python build-contract, real HTTP, and integration-contract tests.
- Added WebUI behavior and security documentation.

## Source and Build Boundary

- Editable frontend source is limited to:
  - `webui/src/admin.html`
  - `webui/src/admin.css`
  - `webui/src/admin.js`
  - `webui/src/settings-copy.js`
  - `webui/src/settings-model.js`
  - `webui/src/settings-page.js`
  - `webui/src/workspace-editor.js`
- `npm --prefix webui run build`:
  - reads only `webui/src/**`
  - deletes the previous `coding_tools_mcp/webui_dist` directory
  - generates one self-contained `admin.html`
  - embeds every module and stylesheet with a `data-build-source` marker
  - fails when an external JavaScript or CSS reference remains
- `coding_tools_mcp/webui_dist/admin.html` was not edited manually.
- Source and generated dist are committed atomically in the implementation commit.
- Repeated builds produced the same SHA-256:
  - `497635D4352F79C9F23BBCD244AFDAA8360AB3DEC700C5EE492474D97A4CA79E`
- No npm dependency, package lock, `pyproject.toml`, or `uv.lock` change was introduced.

## Authentication Boundary

- The WebUI uses only the dedicated Phase 08 Admin credential.
- The token exists only in the current page's JavaScript memory.
- It is sent in the `Authorization: Bearer` header to same-origin `/admin/api` requests.
- It is not placed in:
  - URLs
  - browser persistent storage
  - server Settings
  - Gateway configuration
  - logs
- The token input uses password/autocomplete-off behavior and can be explicitly cleared.
- Actual HTTP tests verify:
  - ordinary MCP bearer receives 401 for `/admin`
  - dedicated Admin bearer receives the generated WebUI with HTTP 200

## Settings Semantics

- Active, persisted, pending-restart fields, and persisted revision are shown separately.
- Settings writes send the exact `persisted_revision` last read by the page.
- HTTP 409 / `stale_revision` handling:
  - preserves the user's current form draft
  - re-reads the latest persisted Settings and revision
  - displays an explicit conflict
  - moves focus to the conflict alert
  - does not silently merge or overwrite
- A real DOM state test confirms a local draft port of 9000 remains while the server's latest persisted port becomes 8100 and the revision updates.

## Tool Catalog and Permission Copy

- WebUI source contains no `tool_profile` control, state, serializer, migration, or display path.
- Safe mode copy states that the fixed complete tool catalog remains visible and mutation tools are not hidden or disabled.
- Trusted and dangerous mode descriptions match the upstream Runtime capability boundary rather than promising catalog filtering.
- Fake-readonly annotations appear only in an advanced danger section.
- The warning states that fake-readonly:
  - does not hide tools
  - does not block mutation
  - does not change handlers
  - is not a security boundary
  - requires dangerous mode and restart-time CLI/environment configuration

## Gateway Semantics

- Gateway status and redacted persisted summaries are displayed.
- Gateway saves continue to use the Phase 08 persisted revision.
- A stale Gateway revision refreshes the persisted revision while preserving the textarea draft.
- The UI contains no start, stop, reload, or Runtime mutation control.
- Successful saves report `restart_required`; existing Runtime snapshots remain unchanged.
- The browser editor accepts only credential-free replacement documents and rejects credential/reference/sensitive header or environment keys.
- Existing credential-bearing configurations are represented only by redacted summaries.

## OAuth and Secret Boundary

- OAuth lists use the Phase 08 redacted collections and exact-ID action endpoints.
- A defensive frontend sanitizer removes client secrets, token material, digests, hashes, signing secrets, and credential-reference fields before rendering.
- Destructive OAuth actions require confirmation that identifies the resource, exact ID, and scope of impact.
- Secret Vault lists show names/configured state only.
- Vault values are accepted only through password inputs, never rendered, and are cleared after requests.
- Gateway, OAuth, Settings, and Vault pages do not expose credential values or internal credential references.

## Conversation Boundary

- Conversation listing calls the summary endpoint with Workspace ID, query, and pagination.
- Full messages and context are fetched only after explicit selection through the paginated detail endpoint.
- Message, context, conversation, Workspace labels, server errors, and summaries are treated as untrusted text.
- Rendering uses DOM node creation and `textContent`; WebUI source contains no `innerHTML` path.
- DOM tests use script/image/iframe/SVG-shaped payloads and confirm that no executable element is created.
- Delete confirmations include:
  - exact Workspace ID
  - exact object/conversation ID
  - expected affected scope
- Actual affected counts from the API are shown after the operation.

## Accessibility and Responsive Behavior

- Every input, select, and textarea has an explicit label.
- Settings errors and stale conflicts use focusable `role="alert"` regions.
- The destructive confirmation dialog restores focus to the initiating control.
- Navigation and actions are keyboard accessible.
- Controls use a 44-pixel minimum target size.
- Responsive layouts cover tablet and narrow-screen breakpoints, including a single-column form/detail layout.
- A skip link and focusable main region are included.

## Telemetry Display

- The frontend model supports enabled, off, debug, and unknown telemetry presentation states.
- The page displays the documented environment controls for disabling telemetry.
- Phase 10 does not add backend telemetry fields; when Phase 08 status does not report a mode, the UI honestly displays `未报告`.
- No chat content, paths, commands, model responses, or identifiers are added to telemetry.

## Files Changed

- `webui/package.json`
- `webui/scripts/build.mjs`
- `webui/src/admin.html`
- `webui/src/admin.css`
- `webui/src/admin.js`
- `webui/src/settings-copy.js`
- `webui/src/settings-model.js`
- `webui/src/settings-page.js`
- `webui/src/workspace-editor.js`
- `webui/tests/settings-model.test.mjs`
- `webui/tests/security-model.test.mjs`
- `webui/tests/dom-interactions.test.mjs`
- `coding_tools_mcp/webui.py`
- `coding_tools_mcp/webui_dist/admin.html` (formal build output only)
- `docs/admin-api.md`
- `docs/admin-webui.md`
- `docs/integration-contract-v0.2.2.md`
- `tests/test_webui.py`
- `tests/compliance/test_mcp_admin.py`
- `tests/test_integration_contract_v022.py`

No Desktop, OAuth Store/schema, OAuth/Refresh implementation, Gateway lifecycle, Admin service, HTTP Server implementation, Settings validator/store, Secret Vault format, `pyproject.toml`, or lockfile was changed.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `npm --prefix webui run test:models` | 0 | Ran 10 model/security/accessibility tests; all passed. |
| `npm --prefix webui run test:dom` | 0 | Ran 5 DOM/interaction tests; all passed. |
| `npm --prefix webui test` before and after build | 0 | Ran 15 tests; all passed both times. |
| `npm --prefix webui run build` | 0 | Generated self-contained `coding_tools_mcp/webui_dist/admin.html`. |
| source/dist exact-content and external-asset alignment check | 0 | Every source module is embedded exactly; no external asset reference remains. |
| deterministic repeat-build check | 0 | Pre/post build SHA-256 values are identical. |
| `.\.venv\Scripts\python.exe -m unittest tests.test_webui tests.compliance.test_mcp_admin tests.test_integration_contract_v022` | 0 | Ran 24 tests; all passed. |
| Phase 03–10 focused regression suite | 0 | Ran 95 tests; all passed. |
| Ruff over Phase 10 Python source/tests | 0 | All checks passed. |
| `py_compile` over Phase 10 Python source/tests | 0 | All modules compiled. |
| Mypy over `coding_tools_mcp/webui.py` | 0 | No issues found. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite security` | 0 | Native Windows runner exited 0; 15 bodies skipped by known `/dev/null` preflight. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite mcp-contract` | 0 | Native Windows runner exited 0; 37 bodies skipped by the same preflight. |
| `git diff --check` and staged whitespace check | 0 | No whitespace errors. |
| source/dist forbidden-path and credential scans | 0 | `tool_profile`, unsafe DOM/storage/lifecycle, and high-risk credential matches were all zero. |
| forbidden-scope scan | 0 | No Desktop, OAuth, Gateway lifecycle, Server/Admin backend, packaging, or lockfile changes. |

## Known Platform Baseline

- Native Windows security and MCP-contract runners continue to skip when their shared fixture requires `/dev/null`.
- Phase 10's Node model/DOM tests and real HTTP Admin tests execute on Windows and passed independently.
- Final integrated Linux CI must execute the skipped compliance bodies before release.

## Remaining Risks and Deferred Work

- Package-data inclusion for `coding_tools_mcp/webui_dist/admin.html` remains Phase 11 because Phase 10 was prohibited from modifying `pyproject.toml`.
- Backend telemetry status wiring remains Phase 11; the UI already supports the documented states and currently shows unknown when the backend omits the field.
- The WebUI intentionally does not edit credential-bearing Gateway documents; operators use the established Secret Vault/configuration workflow for those changes.
- Phase 05's Refresh rotation/access-token insertion cross-transaction availability risk remains unchanged.

## Phase 11 Preconditions

- Start from a clean worktree with Phase 10 marked complete.
- Preserve `webui/src/**` as source and regenerate dist only through the formal build.
- Add package-data without removing upstream Desktop package discovery, optional dependencies, or locale assets.
- Do not change telemetry privacy fields/defaults while wiring its status.
- Keep the Admin token memory-only and preserve dedicated Admin authentication.
- Do not introduce Gateway hot reload, tool-profile controls, or secret display.

## Secret Check

- No real Admin token, OAuth credential, Gateway credential, Vault value, transcript, database, private key, or browser storage artifact was committed.
- Test credentials and untrusted-content payloads are synthetic canaries only.
