# Phase 02 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Implementation commit: `3d8b54c1032c5c138a07e8fef79221a605099586`
- Handoff commit: filled by next agent from `git log`
- Started from: `2fe8fb24ba5dba4e2588de8f2eb52b04e091a366`

## Scope Completed

- Added a normative extension contract for the v0.2.2 integration.
- Added a machine-readable decision table covering protocol, catalog,
  migration, OAuth, Workspace binding, telemetry, version, and secret-store
  boundaries.
- Added focused contract tests that bind the document to current upstream
  protocol constants, the fixed tool registry, fake-readonly behavior, OAuth
  metadata constants, DCR narrowing, and token-handler implementation.
- Added three explicitly phase-labeled skipped implementation tests for Phase
  03 settings migration, Phase 04 OAuth Store persistence, and Phase 06
  Agent-to-Workspace binding.
- Did not modify runtime, OAuth, transport, Admin, WebUI, desktop, workflow, or
  upstream test behavior.

## Files Changed

- `docs/integration-contract-v0.2.2.md`: records the decisions later phases must
  implement and exposes them as a tested JSON decision table.
- `tests/test_integration_contract_v022.py`: verifies the pure contract and
  current upstream compatibility facts; future implementation assertions are
  explicitly skipped with their removal Phase.
- `docs/integration-handoffs/STATUS.md`: marks Phase 02 complete.
- `docs/integration-handoffs/phase-02.md`: records this handoff.

## Decisions Applied

- Protocol target remains `2025-11-25`, compatible with `2025-06-18`.
- The integrated runtime uses one fixed catalog sourced from `TOOL_REGISTRY`;
  legacy `tool_profile` values never filter `tools/list`.
- Known legacy profiles are accepted as migration input, produce
  `legacy_tool_profile_ignored`, have no effective runtime value, and are
  omitted on the next successful settings write. Unknown values are also
  ignored with a warning rather than reinterpreted as security policy.
- `--dangerously-fake-readonly-annotations` is only an annotation compatibility
  override. It does not change handlers or prevent mutation and is not a
  security boundary.
- OAuth grant and response advertisement continue to use the constants in
  `coding_tools_mcp.oauth`; only implemented endpoint branches may be added.
- OAuth persistence is implemented in Phase 04 and connected to HTTP in Phase
  05. Authorization codes remain ephemeral; persisted token plaintext is
  forbidden.
- Agent-to-Workspace resolution occurs while creating the HTTP session Runtime
  during initialize, after OAuth identity is known. The binding is immutable
  and invalid mappings fail closed. stdio uses the explicit default Workspace.
- Upstream v0.2.2 telemetry defaults remain unchanged during integration.
- Server Admin and desktop profile secret stores remain separate.
- Integration package version remains `0.2.2` until the release Phase.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.test_integration_contract_v022` | 0 | Ran 8 tests: 5 passed and 3 were explicitly skipped for Phases 03, 04, and 06. |
| `.\.venv\Scripts\python.exe -m ruff check --ignore=E501 tests/test_integration_contract_v022.py` | 0 | All checks passed. |
| `.\.venv\Scripts\python.exe -m unittest tests.compliance.test_docs_required` | 0 | Ran 4 tests: OK. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite mcp-contract` | 0 | Runner exited 0; native Windows skipped 37 bodies on the already documented `/dev/null` preflight. Exact-SHA Linux evidence remains recorded in Phase 01. |
| `git diff --check` | 0 | No whitespace errors before the implementation commit. |
| `git diff --cached --check` | 0 | No whitespace errors in the staged implementation commit. |

## Known Baseline Failures

- Native Windows `mcp-contract` still skips its bodies because the upstream
  fixture preflight requires POSIX `/dev/null`. This is the Phase 01 documented
  baseline condition, not a new Phase 02 failure.

## Remaining Risks

- The three phase-labeled skipped tests must be replaced by real implementation
  assertions in Phases 03, 04, and 06 respectively; they must not become
  permanent skips.
- The final integrated candidate still requires a real Linux/full compliance
  run; pristine v0.2.2 exact-SHA evidence cannot validate later product code.

## Next Phase Preconditions

- Confirm this handoff commit and a clean integration worktree.
- Read `docs/integration-contract-v0.2.2.md` before porting configuration
  modules.
- Phase 03 may port only Settings Store, Settings Definition, Workspace Catalog,
  Secret Vault, their focused tests, and necessary package imports.
- Phase 03 must remove `TOOL_PROFILE_CHOICES` as a valid new setting, emit the
  documented migration warning for old values, omit `tool_profile` on the next
  successful write, and replace the Phase 03 skip with a real passing test.
- Phase 03 must not inject Workspace Catalog into Runtime or modify OAuth HTTP
  handlers, the tool registry, Admin/WebUI, or desktop behavior.

## Secret Check

- No credentials, OAuth databases, bearer tokens, signing secrets, vault data,
  or real user configuration were added.
