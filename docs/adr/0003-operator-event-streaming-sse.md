# ADR 0003: Operator browser events use authenticated fetch-based SSE

## Status

Superseded by `/admin` Conversation Center service projections and execution APIs.

## Context

This ADR described a former Operator App design that used
`/api/app/sessions/{session_id}/events`. That product entry, route namespace,
and separate Operator browser session were removed. They must not be restored.

## Decision

Conversation Center reads bounded execution and evidence projections through
authorized `/admin/api/conversations/*` service APIs. The browser polls or
explicitly refreshes after a user-visible action; durable execution state remains
separate from browser transport state. A refresh may recover the Admin session,
but it never resubmits a prior Agent turn.

## Security and failure behavior

- Admin authority is separate from MCP principal ownership.
- Historical execution owners are not rewritten for privileged Admin access.
- Backend text is rendered with DOM creation and `textContent`, never HTML.
- Browser transport loss does not imply session/job loss.
- Browser SSE and Runner WebSocket are separate transport/lifecycle boundaries.
