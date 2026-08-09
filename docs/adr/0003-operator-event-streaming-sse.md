# ADR 0003: Operator browser events use authenticated fetch-based SSE

## Status

Accepted.

## Context

The Agent workbench needs ordered server events for assistant text, tool activity,
progress, approvals, failures, and repository-context changes. Operator requests
require bearer authorization, while native `EventSource` cannot set an
`Authorization` header. Credentials must never be put in event-stream URLs.

## Decision

The browser uses authenticated `fetch` to read SSE:

```text
GET /api/app/sessions/{session_id}/events?after={cursor}
Accept: text/event-stream
Authorization: Bearer <operator credential>
```

Create/list/get, turns, interrupt and approval decisions remain ordinary Operator
HTTP requests. The browser keeps a bounded in-memory projection and reconnects
with the latest cursor. A page refresh restores durable AgentSession state; it
never resubmits the previous turn.

## Security and failure behavior

- Operator bearer material is header-only and memory-only.
- Admin credentials are not accepted as Operator identity.
- Backend text is rendered with DOM creation and `textContent`, never HTML.
- Stream loss is recoverable transport state and does not imply session/job loss.
- Browser SSE and Runner WebSocket are separate transport/lifecycle boundaries.
