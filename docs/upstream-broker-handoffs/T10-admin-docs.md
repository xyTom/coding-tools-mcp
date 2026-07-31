# T10 Handoff — Restart-Only Broker Admin, WebUI, and Operations

Status: **complete**

Parent HEAD: `86b7306`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `docs(config): document restart-only upstream broker exposure`

## Implemented

- Added bounded validation for include, exclude, pinned, tags, and tool-policy collections.
- Preserved legacy missing `expose_mode` as `direct`.
- Preserved dedicated credential redaction, Vault resolution, and revision-conflict behavior.
- Gateway Admin payload now explicitly reports:
  - activation at a new MCP Session/Runtime or service restart;
  - new-server default `expose_mode=broker`;
  - `list_changed=false`;
  - `dynamic_reload=false`.
- Added an upstream-only exposure report with:
  - direct tool count and public-definition bytes;
  - complete catalog and broker-only counts;
  - largest sanitized public definitions;
  - per-server direct/broker-only public tool metadata.
- The report excludes local and Admin definitions and contains no raw definitions or credentials.
- Admin WebUI new-server templates default to broker exposure.
- Added direct/pinned/broker-only preview based on the current Runtime's frozen public catalog.
- Added exact UI copy that changes activate only for a new Session/Runtime or service restart and do not send `list_changed`.
- Rebuilt `coding_tools_mcp/webui_dist/admin.html` from `webui/src/**` with the formal build.
- Added `docs/upstream-broker.md` covering workflow, risk annotations, digest and argument validation, ResultStore isolation, Runtime freeze, legacy profile behavior, operations, and explicit non-goals.
- Extended the v0.2.2 machine-readable integration contract without reintroducing live profiles or catalog mutation.
- Corrected the T09 handoff punctuation typo for `UPSTREAM_DISCONNECTED`.

## Configuration bounds

- include/exclude/pinned: at most 256 unique strings, each at most 512 characters;
- tags: at most 32 unique strings, each at most 64 characters;
- tool policy: at most 256 entries, keys at most 512 characters;
- custom synonyms retain the existing 128-term / 10-values-per-term / 64-character bounds.

## Files changed

- `coding_tools_mcp/upstream.py`
- `coding_tools_mcp/admin.py`
- `webui/src/admin.html`
- `webui/src/admin.js`
- `webui/tests/security-model.test.mjs`
- `coding_tools_mcp/webui_dist/admin.html`
- `tests/compliance/test_upstream_admin_observability.py`
- `tests/compliance/test_mcp_admin.py`
- `README.md`
- `docs/upstream-broker.md`
- `docs/tools-and-schemas.md`
- `docs/integration-contract-v0.2.2.md`
- `docs/upstream-broker-handoffs/T09-runtime-lifecycle.md`
- `docs/upstream-broker-handoffs/T10-admin-docs.md`

## Tests

- targeted Admin/Gateway/observability: **38 passed, 26 subtests passed**
- T10 combined backend and contract validation: **63 passed, 198 subtests passed in 7.90s**
- WebUI model/security tests: **12 passed**
- WebUI DOM tests: **5 passed**
- formal WebUI build: **PASS**
- Ruff: **PASS**
- mypy: **PASS — 4 production source files**
- `git diff --check`: **PASS**

## Compatibility

- Existing Runtime snapshots are not changed by Admin writes.
- New Runtime construction reads the new persisted configuration.
- Legacy configurations without `expose_mode` remain direct.
- The five Broker tools remain fixed local entries.
- `tools.listChanged=false` remains truthful.
- No manager start/stop/reload API or live profile activation was added.

## Next task

- T11 is validation, performance measurement, and release handoff only; it must add no new product feature.
