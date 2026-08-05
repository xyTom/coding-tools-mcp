# T02 Handoff — Schema Sanitizer, Risk, and Public Digest

Status: **complete**

Parent HEAD: `6da9b73546c1a49840b0448a923f8f81473020e1`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `feat(upstream): contain untrusted tool metadata`

## Implemented

- Added `coding_tools_mcp/upstream_sanitize.py` as the untrusted metadata containment boundary.
- Public tool definitions are now explicitly constructed rather than copied and pruned.
- Sanitized top-level `title`, `description`, `inputSchema`, `outputSchema`, and known boolean annotations.
- Added recursive Schema containment for supported JSON Schema fields, including:
  - string and string-array `type`;
  - `properties`, `required`, `items`, and `additionalProperties`;
  - `enum`, `default`, `const`, and bounded examples;
  - `oneOf`, `anyOf`, `allOf`, and `not`;
  - numeric, string, and array bounds;
  - bounded pattern and format metadata.
- Non-dict property schemas degrade to an unconstrained public property schema instead of being passed through.
- `$ref` nodes degrade to a bounded loose object schema; unsupported definitions are not retained as dangling references.
- Added property, enum, branch, recursion, value, and text limits.
- Added staged hard degradation with a final compact UTF-8 definition size strictly below 8192 bytes.
- Changed `UpstreamTool` to retain separate:
  - `raw_definition`;
  - `public_definition`;
  - `effective_risk`;
  - `public_schema_digest`;
  - optional `raw_schema_digest`.
- `tool_definitions()` now returns only deep copies of public definitions.
- Public digest is derived only from the sanitized public `inputSchema`.
- Raw-only metadata changes alter the raw digest without drifting the public digest.
- Risk classification now follows local policy, then real upstream annotations, then defaults unknown tools to mutating.
- Added a programmatic `tool_policy` slot to `UpstreamServerConfig`; configuration-file validation and Admin wiring remain assigned to T05/T10.
- Invalid upstream metadata now degrades safely instead of preventing the complete fixed Runtime snapshot from initializing.

## Files changed

- `coding_tools_mcp/upstream_sanitize.py`
- `coding_tools_mcp/upstream.py`
- `tests/compliance/test_upstream_sanitize.py`
- `tests/compliance/test_upstream_gateway.py`
- `docs/upstream-broker-handoffs/T02-sanitizer-risk-digest.md`

## Tests

- command: `python -m pytest tests/compliance/test_upstream_sanitize.py tests/compliance/test_upstream_gateway.py tests/compliance/test_mcp_admin.py -q`
  - result: **PASS**
  - `35 passed, 20 subtests passed in 7.10s`
- command: `uv run --frozen ruff check coding_tools_mcp/upstream.py coding_tools_mcp/upstream_sanitize.py tests/compliance/test_upstream_gateway.py tests/compliance/test_upstream_sanitize.py`
  - result: **PASS — All checks passed**
- command: `uv run --frozen --extra dev mypy coding_tools_mcp/upstream.py coding_tools_mcp/upstream_sanitize.py`
  - result: **PASS — Success: no issues found in 2 source files**
- static stable-catalog contract:
  - no `tool_profile` control path;
  - no `start_server()` or `stop_server()`;
  - direct definitions are sourced from `public_definition`;
  - result: **PASS**

## Compatibility

- Public upstream names and ordering remain unchanged.
- All upstream tools remain directly exposed in T02.
- Valid supported Schema fields and real annotations remain visible.
- Unsupported `$defs` and unknown metadata no longer enter `tools/list`.
- Direct tool execution and result/error envelopes are unchanged.
- Fake-readonly compatibility display does not change `effective_risk`.
- `tools.listChanged=false` and fixed Runtime construction remain unchanged.

## Security boundary

- The sanitizer is untrusted metadata containment, not a complete prompt-injection defense.
- Raw definitions remain server-side diagnostic data and are never returned by `tool_definitions()`.
- Unknown or conflicting annotations classify as mutating unless an explicit local policy says otherwise.
- Public definitions always use a hard byte budget before model exposure.

## Known limitations

- Config-file parsing and Admin validation do not yet populate `tool_policy`; that integration is deferred to the configuration task.
- Catalog entries, Broker discovery, expose mode, and result budgeting are not implemented in T02.
- Full JSON Schema `$ref` resolution is intentionally not implemented; references degrade.
- Raw digests are `None` when raw `inputSchema` is not an object or cannot be canonically serialized.

## Next task prerequisites

- Start T03 from the commit containing this handoff.
- Read only T03, this handoff, result normalization/call paths, and the result-truncation appendix section.
- Preserve raw/public definition separation and use the public definition for all model-visible tool metadata.

## Do not redo

- Do not restore verbatim upstream definition exposure.
- Do not compute Broker-facing digests from raw Schema.
- Do not treat unknown annotations as read-only.
- Do not implement ResultStore handles, catalog search, expose mode, or Broker tools in T03.
