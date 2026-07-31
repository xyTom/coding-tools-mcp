# T07 Handoff — Fixed Broker Calls and Passthrough

Status: **complete**

Parent HEAD: `1290721`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `feat(broker): validate and dispatch fixed upstream calls`

## Implemented

- Added fixed `upstream_tool_call` and `upstream_tool_call_mutating` local tools.
- Read-only route is read-only, non-idempotent, and open-world.
- Mutating route is destructive, non-idempotent, and open-world.
- Both routes are fixed in `TOOL_REGISTRY` and are never profile- or catalog-hidden.
- Read-only route only accepts `effective_risk=readonly`.
- Mutating route only accepts `effective_risk=mutating` (including unknown-defaulted-to-mutating).
- Read-only digest is optional; mutating digest is required and must be 32-character lowercase hex.
- Mismatched digest returns `UPSTREAM_SCHEMA_CHANGED` with the current public digest.
- Added public Schema argument validation for type, required, enum, const, string/array bounds, numeric bounds, additional properties, `oneOf`, `anyOf`, and `allOf`.
- Public Schema vailures return `UPSTREAM_ARGUMENTS_INVALID` before any remote call.
- Loose object Schemas remain compatible.
- Broker call handlers return complete MCP envelopes.
- `Runtime.call_tool()` recognizes the two Broker passthrough routes and does not call `make_tool_result()` again.
- Upstream `isError` is preserved.
- Fake-readonly compatibility annotations do not change runtime risk routing.

## Files changed

- `coding_tools_mcp/server.py`
- `tests/compliance/test_upstream_broker_calls.py`
- `docs/tools-and-schemas.md`
- `docs/upstream-broker-handoffs/T07-broker-calls.md`

## Tests

- T07 special / discovery / schema-drift: **24 passed, 169 subtests passed**
- combined T02-T07 regression: **104 passed, 204 subtests passed in 10.24s**
- Ruff: **PASS**
- mypy upstream/search paths: **PASS**

## Compatibility

- `tools.listChanged=false` is unchanged.
- No profile visibility branches were added.
- Direct upstream tools still use the same Manager normalization and result budget.
- Broker calls do not yet store overflow results or return handles.

## Next task

- Add session-isolated ResultStore.
- Store original Broker results before truncation.
- Add fixed `upstream_result_fetch` and handle metadata.
