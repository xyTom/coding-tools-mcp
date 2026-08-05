# T08 Handoff — Session-Scoped Upstream Result Paging

Status: **complete**

Parent HEAD: `055f35d`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `feat(broker): add session-scoped upstream result paging`

## Implemented

- Added `ResultStore` with default limits:
  - single result <= 8 MiB;
  - per owner <= 16 handles and 16 MiB;
  - global <= 64 MiB;
  - TTL 5 minutes;
  - FIFO eviction.
- Stored the original normalized UTF-8 JSON envelope before budget truncation.
- Only oversized Broker calls with a concrete owner are stored.
- Direct upstream calls and Broker calls without an owner do not store results.
- Attached `_result_handle` and `_result_fetch_tool` to truncated Broker metadata.
- Added fixed local `upstream_result_fetch` as read-only and idempotent.
- Fetch pages by Unicode codepoint offset with 1..32000 limit and runtime clamping.
- HTTP Runtime owner is the published `Mcp-Session-Id`.
- stdio Runtime owner is a per-Runtime high-entropy `stdio:` identity.
- Cross-owner, unknown, and expired handles use the same opaque `UPSTREAM_RESULT_NOT_FOUND` error.
- Upstream manager close clears its own ResultStore.
- Fixed local directory now contains all ive Broker tools.

## Files changed

- `coding_tools_mcp/upstream_result_store.py`
- `coding_tools_mcp/upstream.py`
- `coding_tools_mcp/server.py`
- `tests/compliance/test_upstream_result_store.py`
- `tests/compliance/test_upstream_gateway.py`
- `docs/tools-and-schemas.md`
- `docs/runtime-contract-v0.2.md`
- `docs/upstream-broker-handoffs/T08-result-store.md`

## Tests

- T08 special / T07 / schema-drift: **29 passed, 173 subtests passed**
- combined T02-T08 regression: **114 passed, 208 subtests passed in 11.10s**
- Ruff: **PASS**
- mypy: **PASS — 3 source files**

## Owner boundary

- HTTP ownership is the MCP session identity.
- stdio ownership is Runtime/process-local.
- No shared default owner exists.
- OAuth principal + session composite ownership is not implemented in v6 and is intentionally deferred.

## Compatibility

- `tools.listChanged=false` is unchanged.
- No profile visibility branches were added.
- Small upstream results remain inline and unstored.
- Oversized results larger than 8 MiB are truncated without a handle.
- ResultStore is explicitly per-manager and is not shared between Runtimes.

## Next task

- Harden client call/close concurrency with leases or equivalent locking.
- Preserve per-Runtime client, upstream HTTP session, catalog, and ResultStore isolation.
