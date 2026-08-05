# T01 Handoff — Fixed Registry Snapshot Migration

Status: **complete**

Parent HEAD: `878cf3be4d944ddea8c585095e3a960c21d9be44`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `refactor(upstream): freeze registry in immutable runtime state`

## Implemented

- Added frozen `UpstreamRegistryState` with one Runtime-generation view of:
  - all upstream tools;
  - stable direct tool-name order;
  - reserved catalog slot;
  - reserved search-index slot;
  - upstream clients.
- Wrapped tool, catalog, and client mappings in `MappingProxyType`.
- Replaced the separate mutable `_tools`, `_tool_order`, and `clients` fields.
- Changed all manager reads to capture one local `state` before accessing tools and clients.
- Built the complete registry in local dictionaries and published it once after all configurations finished initialization.
- Closed all already-initialized local clients if a later fatal configuration error prevents publication.
- Kept nonfatal upstream initialization failures tolerant and represented through the existing status payload.
- Kept `close()` idempotent while preserving the fixed exposed tool snapshot after close.
- Preserved all tools as direct; catalog and search remain empty future slots.

## Files changed

- `coding_tools_mcp/upstream.py`
- `tests/compliance/test_upstream_gateway.py`
- `docs/upstream-broker-handoffs/T01-registry-state.md`

`coding_tools_mcp/server.py` was reviewed but did not require modification because Runtime already consumes the manager snapshot only during construction and keeps `tools.listChanged=false`.

## Tests

- command: `python -m pytest tests/compliance/test_upstream_gateway.py tests/compliance/test_mcp_admin.py -q`
  - result: **PASS**
  - `26 passed, 20 subtests passed in 6.19s`
- command: `uv run --frozen ruff check coding_tools_mcp/upstream.py tests/compliance/test_upstream_gateway.py`
  - result: **PASS**
- command: `uv run --frozen --extra dev mypy coding_tools_mcp/upstream.py`
  - result: **PASS — Success: no issues found in 1 source file**
- static contract assertions:
  - no `tool_profile` in `upstream.py`;
  - no `start_server()` or `stop_server()`;
  - result: **PASS**

## Compatibility

- Upstream public names and ordering are unchanged.
- All discovered upstream tools remain directly exposed.
- Existing annotations and result/error envelopes are unchanged.
- Two Runtime instances still receive separate client/session state.
- Disabled and failed upstream configurations retain existing status semantics.
- `tools.listChanged=false` remains unchanged.
- Calling a known tool after manager close still returns structured `UPSTREAM_DISCONNECTED`.

## Known limitations

- `UpstreamTool.definition` is still the existing raw mutable dictionary value; T02 replaces it with raw/public definition tracks and sanitization.
- Client call/close mutual exclusion is not added in T01; final lifecycle locking remains a later task.
- Catalog and search-index slots are intentionally empty.
- No expose mode, Broker tools, result budget, or dynamic lifecycle was implemented.

## Next task prerequisites

- Start T02 from the commit containing this handoff.
- Read only T02, this handoff, the `UpstreamTool`/definition paths, and the sanitizer appendix section.
- Preserve fixed Runtime construction and all-direct exposure while adding raw/public metadata tracks.

## Do not redo

- Do not restore separate mutable manager client/tool dictionaries.
- Do not introduce Runtime profile filtering or dynamic start/stop/reload.
- Do not implement catalog search, expose mode, Broker dispatch, or result truncation in T02.
