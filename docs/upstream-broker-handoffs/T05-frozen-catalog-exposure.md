# T05 Handoff — Frozen Catalog and Startup Exposure

Status: **complete**

Parent HEAD: `9c243ef`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `feat(upstream): freeze catalog and exposure at runtime startup`

## Implemented

- Added startup configuration for `expose_mode`, `pinned_tools`, `tags`, and `tool_policy`.
- Added global `tool_search.custom_synonyms` parsing with bounded validation.
- Legacy configurations without `expose_mode` remain `direct`.
- Include/exclude filtering runs before catalog and exposure decisions.
- Direct mode puts every filtered tool in both direct exposure and catalog.
- Broker mode puts every filtered tool in catalog and only pinned remote names in direct exposure.
- Collision detection still covers every discovered tool, including broker-only tools.
- Built compact `UpstreamToolCatalogEntry` values from sanitized public metadata.
- Built `CatalogSearchIndex` once during manager construction and published it in the same immutable registry state.
- Added typed catalog/search slots and manager catalog accessors.
- Added `catalog_tool_count` to status without changing existing direct `tool_count`.
- Updated Gateway Admin read/validate/save paths to preserve and validate `tool_search` while remaining restart-only.
- Existing Runtime snapshots remain unchanged after configuration persistence; new Runtime construction reads the new snapshot.

## Files changed

- `coding_tools_mcp/upstream.py`
- `coding_tools_mcp/admin.py`
- `tests/compliance/test_upstream_gateway.py`
- `tests/compliance/test_mcp_admin.py`
- `docs/upstream-broker-handoffs/T05-frozen-catalog-exposure.md`

## Tests

- targeted Gateway/Admin/search: **53 passed, 23 subtests passed**
- combined T02-T05 regression: **67 passed, 23 subtests passed in 7.61s**
- Ruff: **PASS**
- mypy: **PASS — 3 source files**

## Compatibility

- Old configuration remains fully direct.
- `tools.listChanged=false` is unchanged.
- No live start/stop/reload methods were added.
- Admin writes persist configuration and report restart-required only.
- No Broker tools are exposed yet; T06 adds fixed Search and Describe.

## Next task

- Add fixed local `upstream_tool_search` and `upstream_tool_describe`.
- Use the frozen catalog and index only.
- Never expose raw definitions.
