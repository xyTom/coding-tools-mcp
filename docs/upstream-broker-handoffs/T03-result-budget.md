# T03 Handoff — Hard Result Envelope Budgets

Status: **complete**

Parent HEAD: `1533bca1a7a0957d6f6a89243ac515019e879654`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `fix(upstream): enforce hard result envelope budgets`

## Implemented

- Added `coding_tools_mcp/upstream_result.py` as the single content-aware result-budget boundary.
- Added a hard final UTF-8 JSON envelope limit of `128_000` bytes for direct upstream results.
- Split client and manager responsibilities:
  - `BaseUpstreamClient.call_tool_raw()` returns the raw `tools/call` result;
  - backward-compatible `BaseUpstreamClient.call_tool()` performs raw call plus normalization only;
  - `UpstreamManager.call_tool()` performs the sole normalize plus final budget pass.
- Preserved small normalized results without changing their structure or identity.
- Kept the v0.2.2 missing-content contract: absent `content` normalizes to `[]`; structured text is not copied into content.
- Added cumulative envelope-aware text truncation so multiple text blocks cannot bypass the limit.
- Added content-type-aware handling:
  - image, audio, and blob payloads are omitted as complete binary units and replaced with bounded text placeholders;
  - resource URI and bounded metadata are retained;
  - resource text is truncated and embedded blob data is removed;
  - oversized unknown content blocks become bounded placeholders;
  - small unknown blocks remain structurally available through recursive limiting.
- Added recursive `structuredContent` and `_meta` limits for depth, item count, key size, and cumulative UTF-8 string bytes.
- Added staged degradation profiles followed by a compact minimum envelope if an intermediate result still exceeds the limit.
- Re-serializes every truncated candidate before return and guarantees the production result does not exceed the configured envelope limit.
- Preserves `isError` for both successful and error results.
- Applies the same final budget to oversized upstream error envelopes and RPC error details.
- Creates truncation metadata when needed:
  - `_truncated: true`;
  - `_original_bytes` with the pre-truncation compact UTF-8 JSON size.
- Does not create a ResultStore entry, result handle, session-bound overflow state, or fetch action.

## Files changed

- `coding_tools_mcp/upstream_result.py`
- `coding_tools_mcp/upstream.py`
- `tests/compliance/test_upstream_result_budget.py`
- `tests/compliance/test_upstream_gateway.py`
- `docs/upstream-broker-handoffs/T03-result-budget.md`

## Tests

- command: `python -m pytest tests/compliance/test_upstream_result_budget.py tests/compliance/test_upstream_gateway.py tests/compliance/test_mcp_admin.py -q`
  - result: **PASS**
  - `37 passed, 20 subtests passed in 6.84s`
- command: `uv run --frozen ruff check coding_tools_mcp/upstream.py coding_tools_mcp/upstream_result.py tests/compliance/test_upstream_gateway.py tests/compliance/test_upstream_result_budget.py`
  - result: **PASS — All checks passed**
- command: `uv run --frozen --extra dev mypy coding_tools_mcp/upstream.py coding_tools_mcp/upstream_result.py`
  - result: **PASS — Success: no issues found in 2 source files**

## Acceptance coverage

- small result shape remains unchanged;
- missing content becomes an empty list without duplicating structured text;
- many text blocks share one cumulative budget;
- binary payload strings do not survive truncation;
- resource URI survives while resource blob is removed;
- oversized unknown content becomes a placeholder;
- nested structured content is recursively bounded;
- a missing structuredContent object receives Phase 1 metadata;
- direct manager calls enforce the final budget;
- oversized error details enforce the same final budget;
- final serialized envelope is at most `128_000` bytes;
- `isError` is preserved;
- no `_result_handle`, ResultStore, or handle-producing path exists.

## Compatibility

- Direct upstream public names, ordering, Schema definitions, and annotations are unchanged.
- Small tool-call results retain their existing MCP shape.
- Existing upstream error codes and small error envelopes remain unchanged because results below the limit are returned as-is.
- All upstream tools remain direct.
- `tools.listChanged=false` and immutable Runtime construction remain unchanged.

## Known limitations

- Truncated output is not recoverable in T03; ResultStore ownership and handles are intentionally deferred.
- Oversized nonstandard top-level result fields other than `_meta` are omitted during truncation.
- Very large resource metadata strings, including pathological URIs, are bounded to preserve the hard envelope guarantee.
- Result budgeting operates after the transport response-size guard; it is not a replacement for transport-level response limits.

## Next task prerequisites

- Start T04 from the commit containing this handoff.
- Read only T04, this handoff, and the tokenizer/BM25 appendix section.
- Keep the search module pure and disconnected from Runtime Broker wiring.

## Do not redo

- Do not move normalization back into the manager client request implementation.
- Do not copy structuredContent text into missing content.
- Do not store overflow or emit handles in T04.
- Do not implement Broker tools, expose mode, Runtime reload, or dynamic lifecycle.
