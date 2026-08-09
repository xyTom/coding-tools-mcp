# RM02 Handoff — deep-freeze catalog template

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `e4cff0027dd6cf9c28d7cb0fe146f2ba72a2d031`
- Implementation commit: `849b34e581c575a336227ebd57d81b92ccbbaa32`

## Defect Closed

- `UpstreamCatalogTemplate` now defensively copies each config and freezes `env`, `headers`, and `tool_policy` with the existing `_freeze_json` semantics, including nested reference objects.
- Config tuple fields are rebuilt as tuples during template construction.
- Lazy live-client creation receives an independent ordinary config copy, so client-side mutations cannot affect the template or another Runtime manager.
- Existing Core tool definitions, schema fingerprints, and config-revision isolation remain unchanged.

## Files Changed

- `coding_tools_mcp/upstream.py`: added the shared config copy/freeze boundary and used an independent copy for lazy live clients.
- `tests/test_upstream_lazy_catalog.py`: added source defensive-copy, deep-freeze, and two-manager/live-client isolation regression coverage.

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: unchanged
- Core 25 tool catalog/schema changed: no
- Real credential or transcript data used: no

## Validation

| Command | Exit code | Passed / skipped | Notes |
| --- | ---: | --- | --- |
| `python -m unittest tests.test_upstream_lazy_catalog` before fix | 1 | 5 passed / 2 failed | Stable repro: template config mappings were mutable and two live factories received the same config object. |
| `python -m unittest tests.test_upstream_lazy_catalog tests.compliance.test_schema_drift tests.compliance.test_tool_golden` | 0 | 15 passed / 9 skipped | Required RM02 gate passed; skips are the existing platform/environment skips. |
| `python -m unittest tests.test_upstream_lazy_catalog tests.test_upstream_resilience tests.test_upstream_http_integration tests.compliance.test_upstream_gateway tests.compliance.test_upstream_lifecycle` | 0 | 61 passed | Adjacent upstream regression passed. The existing background initialize/close test still prints its pre-existing worker-thread traceback while unittest exits 0. |
| `pytest -W error::pytest.PytestUnhandledThreadExceptionWarning tests/test_upstream_lazy_catalog.py` | 0 | 7 passed | New deep-freeze/isolation tests pass with warning-as-error. |
| `git diff --check` | 0 | pass | No whitespace errors. |

## Security Review

- Template config mappings directly mutable: no
- Nested secret/env reference leaked or mutated through the template: no
- Live client config shared across Runtime managers: no
- Real credential/data used: no

## Remaining Risks

- The existing background initialize/close traceback remains for later lifecycle/thread cleanup review; RM01 recorded the same behavior.
- RM03–RM13 remain open. Phase 18/22 must remain `in remediation`.

## Next Card Preconditions

- Start RM03 from `849b34e581c575a336227ebd57d81b92ccbbaa32`.
- Keep the execution book untracked and modify only the RM03 allowlist.
