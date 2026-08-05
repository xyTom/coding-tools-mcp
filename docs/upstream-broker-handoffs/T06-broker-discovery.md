# T06 Handoff — Fixed Broker Search and Describe

Status: **complete**

Parent HEAD: `cb8e832`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `feat(broker): add fixed upstream discovery tools`

## Implemented

- Added fixed local `upstream_tool_search` and `upstream_tool_describe` entries to `TOOL_REGISTRY`.
- Both tools are read-only, idempotent, and always present regardless of Gateway or catalog state.
- Added fixed short Broker instructions without dynamic server or tool counts.
- Search uses the frozen index and supports query, server, read_only, tags, name_prefix, and bounded limit.
- Search returns compact name/server/remote/title/description/tags/risk/digest/score metadata and no Schema.
- Describe returns the sanitized public definition, public digest, compact metadata, and effective risk.
- Raw definitions are never returned.
- Empty catalog returns a stable empt search and structured not-found describe result.
- `tools.listChanged` remains `false`.

## Files changed

- `coding_tools_mcp/server.py`
- `coding_tools_mcp/upstream.py`
- `tests/compliance/test_upstream_broker_discovery.py`
- `docs/tools-and-schemas.md`
- `docs/runtime-contract-v0.2.md`
- `docs/upstream-broker-handoffs/T06-broker-discovery.md`

## Tests

- targeted discovery/Gateway/schema-drift: **36 passed, 173 subtests passed**
- combined T02-T06 regression: **93 passed, 195 subtests passed in 8.83s**
- Ruff: **PASS**
- mypy upstream/search paths: **PASS — 2 source files**

## Compatibility

- Discovery tools are fixed local catalog entries.
- No conditional tool registration or profile filtering was added.
- Broker-only upstream tools remain hidden from direct definitions but are discoverable.
- No Broker call or ResultStore path exists yet.

## Next task

- Add fixed read-only and mutating Broker call tools.
- Validate risk route, public digest, and arguments before passthrough.
- Preserve upstream MCP envelopes without local double wrapping.
