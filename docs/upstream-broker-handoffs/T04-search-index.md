# T04 Handoff — Field-Weighted Catalog Search Index

Status: **complete**

Parent HEAD: `e167acf2ec53c3eefbc172c21dcdf029bb2a970c`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `feat(upstream): add field-weighted broker search index`

## Implemented

- Added `coding_tools_mcp/upstream_search.py` as a pure, Runtime-independent catalog search module.
- Added instance-scoped `ToolTokenizer` with NFKC normalization, separator handling, camelCase splitting, CJK phrase/bigram fallback, and bounded synonym expansion.
- Added compact frozen catalog/search types and a runtime-checkable `SearchBackend` Protocol.
- Added field-weighted BM25 scoring with weights name 5, title 4, tags 4, alias 3, arguments 2, and description 1.
- Added server, risk/read-only, tags, and normalized name-prefix filters.
- Added exact public-name, alias/remote, unique remote-name, and unique-prefix fast paths.
- Added deterministic score/tool-ID sorting, default limit 5, and hard maximum 20.
- Search results contain compact metadata and public digest only; no Schema is returned.
- Added 50, 200, and 500-tool fixtures.

## Files changed

- `coding_tools_mcp/upstream_search.py`
- `tests/compliance/test_upstream_search.py`
- `docs/upstream-broker-handoffs/T04-search-index.md`

No existing Runtime, manager, configuration, or server code was modified.

## Tests

- `python -m pytest tests/compliance/test_upstream_search.py -q`
  - **PASS — 18 passed, 3 subtests passed**
- combined upstream/search regression:
  - **PASS — 62 passed, 23 subtests passed in 7.06s**
- Ruff:
  - **PASS — All checks passed**
- mypy:
  - **PASS — Success: no issues found in 1 source file**

## Compatibility

- No direct upstream visibility changed.
- No catalog is attached to `UpstreamRegistryState` yet.
- No Broker tools, expose mode, ResultStore, handles, or fetch path were added.
- Stable-catalog Runtime construction remains unchanged.

## Next task prerequisites

- Start T05 from the commit containing this handoff.
- Wire compact catalog entries and this index into the one-time Runtime snapshot.
- Preserve legacy configuration as direct exposure.
- Do not add live lifecycle or list-changed notifications.
