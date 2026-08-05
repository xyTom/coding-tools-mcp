# T00R Handoff — Stable-Catalog Taskbook Adaptation

Status: **complete**

Parent HEAD: `1039d8a17b412b7f872367c66dc311d2c232f404`

Branch: `feat/upstream-tool-broker-v6`

Worktree before task: clean

## Implemented

- Replaced the normative taskbook body with a v0.2.2 stable-catalog adaptation.
- Resolved the T00 architecture conflict in favor of the existing fixed-catalog contract.
- Removed all requirements for live `tool_profile` behavior.
- Removed all requirements for `UpstreamManager.start_server()` / `stop_server()` or Runtime reload.
- Kept `tools.listChanged=false` normative.
- Made direct/broker exposure a Runtime-construction-time decision only.
- Made Admin Gateway writes validate/persist/revision/restart-only; existing Runtime snapshots remain unchanged.
- Defined all five Broker tools as permanent local `TOOL_REGISTRY` entries.
- Defined mutating Broker safety through real annotations, separate risk routes, required public digest, and public Schema validation rather than profile-based hiding.
- Reworked T01–T11 for short-context, one-task-per-Agent execution.
- Added explicit forbidden implementation patterns and a T01 startup prompt.
- Retained Appendix A only as non-normative algorithm reference and explicitly invalidated its profile/dynamic-lifecycle fragments.

## Files changed

- `docs/upstream-tool-broker-v6-execution-taskbook.md`
- `docs/upstream-broker-handoffs/T00R-taskbook-adaptation.md`

## Tests

- command: `python -m pytest tests/compliance/test_upstream_gateway.py tests/compliance/test_docs_required.py -q`
  result: **PASS — 19 passed, 131 subtests passed**

- static review:
  - taskbook body states legacy profile is migration-only;
  - no task requires Runtime profile filtering;
  - no task requires dynamic upstream start/stop/reload;
  - no task changes `listChanged=false`;
  - T11 uses old/new Runtime reconstruction instead of live catalog mutation.

## Stable-catalog compatibility

- no `tool_profile` control path required: **yes**
- no dynamic start/stop/reload required: **yes**
- `listChanged` remains false: **yes**
- existing Runtime snapshot remains immutable: **yes**
- Admin writes remain restart-only: **yes**
- fixed local `TOOL_REGISTRY` remains authoritative: **yes**

## Known limitations

- Appendix A still contains the original v6 technical text, including superseded profile/dynamic lifecycle pseudocode. The taskbook header and normative body explicitly mark those fragments invalid and forbid copying them.
- OAuth principal + session composite ResultStore ownership remains deferred; v6 uses MCP session identity.
- No production code was changed in T00R.

## Next task prerequisites

- Start T01 from the commit containing this handoff.
- Read only T01, T00 baseline handoff, this handoff, the upstream manager/client sections, and the upstream compliance test.
- Preserve current public behavior: all upstream tools remain direct in T01.

## Do not redo

- Do not repeat the full T00 baseline unless HEAD or dependencies changed.
- Do not restore `tool_profile` to Runtime or Gateway code.
- Do not add `start_server()`, `stop_server()`, reload, or list-changed notifications.
- Do not implement sanitizer, catalog search, or expose mode in T01.
