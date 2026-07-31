# T09 Handoff — Runtime Lifecycle and Client Leases

Status: **complete**

Parent HEAD: `f88ea19`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `fix(upstream): harden runtime call and close lifecycle`

## Implemented

- Added a BaseUpstreamClient lifecycle condition, closed state, and active call-lease count.
- `call_tool_raw()` now acquires a lease before transport use and always releases it.
- `close()` atomically marks the client closed, waits for already-acquired leases, then closes the transport.
- New calls after client close raise retryable `UPSTREAM_DISCONNECTED@.
- Stdio transport shutdown moved to the Base client `_close_transport()` hook.
- Added a Manager lifecycle lock.
- Manager call selects the frozen state and client under the lock, then relies on the client lease.
- Manager close atomically cuts off new calls, then closes each per-Runtime client and clears its ResultStore.
- Close remains idempotent.
- No start_server(), stop_server(), reload, or list-change notification was added.
- Added concurrency tests proving an already-started call completes while close waits.
- Added isolation tests for clients, catalogs, search indexes, HTTP upstream sessions, MCP sessions, and ResultStore data.
- Modeled restart as old Runtime close followed by new Runtime construction.

## Files changed

- `coding_tools_mcp/upstream.py`
- `tests/compliance/test_upstream_gateway.py`
- `tests/compliance/test_upstream_lifecycle.py`
- `docs/upstream-broker-handoffs/T09-runtime-lifecycle.md`

## Tests

- T09 lifecycle / Gateway / ResultStore: **37 passed, 17 subtests passed**
- combined T02-T09 regression: **118 passed, 208 subtests passed in 11.89s**
- Ruff: **PASS**
- mypy: **PASS — 2 production source files**

## Concurrency contract

- Calls that have acquired a client lease are allowed to complete.
- Close waits for those calls before transport shutdown.
- Calls that lose the race to close fail with a retryable disconnect.
- Client and Manager close are idempotent.
- No lock is held while waiting on remote I/O.

## Isolation

- Each Runtime owns its own client instances.
- Each HTTP upstream client has its own `Mcp-Session-Id` state.
- Each Runtime has its own frozen catalog and search index.
- Each UpstreamManager has its own ResultStore.
- Admin configuration writes do not replace an existing Runtime state.

## Next task

- T10 is the Admin/WebUI and observability task.
