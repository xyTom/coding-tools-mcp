# ADR 0001: Stable-Catalog Upstream Broker

- Status: Accepted
- Date: 2026-08-02

## Context

The v0.2.2 runtime contract fixes `tools/list` for the lifetime of each Runtime. Earlier Broker drafts assumed live profiles, upstream start/stop, and `listChanged=true`, which conflict with that contract.

## Decision

Upstream discovery occurs before Runtime publication. Each Runtime owns one immutable catalog, direct-definition set, search index, client set, and ResultStore. Five local Broker tools are permanently registered. Admin writes are persist-and-restart-only. Unknown remote risk is mutating. Search and describe may both return the public schema digest; the mutating call validates that digest statelessly.

## Consequences

- Existing Runtime instances never change after Admin configuration writes.
- Broker-only schemas do not enter `tools/list`.
- No live `tool_profile`, start/stop/reload, or `listChanged=true` path exists.
- A session-bound proof that describe occurred is not provided; adding one requires a separate product decision.
- Post-review protocol hardening is tracked in T12 rather than retroactively attributed to T11.
