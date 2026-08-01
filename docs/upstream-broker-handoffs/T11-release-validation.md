# T11 Handoff — Stable-Catalog Upstream Broker Release Validation

Status: **complete**

Parent HEAD: `414aec64a99c2e2dd5a3496ec5e6846c5746d103`

Branch: `feat/upstream-tool-broker-v6`

Commit subject: `docs(handoff): validate stable-catalog upstream broker`

## Completion status

- Stable-catalog adaptation: **complete**
- Phase 1 defensive controls: **complete**
- Phase 2 fixed Broker: **complete**
- Dynamic `listChanged` / live profile activation: **explicitly not implemented**
- Manager `start_server()` / `stop_server()` / reload APIs: **not implemented**
- Existing Runtime mutation after Admin writes: **not implemented by design**

T11 adds no product runtime behavior. It adds one repeatable release-validation script,
one machine-readable result report, and this handoff.

## Validation artifacts

- `scripts/validate_upstream_broker_release.py`
- `reports/upstream-broker-v6-release-validation.json`
- `docs/upstream-broker-handoffs/T11-release-validation.md`

The validation script disables telemetry for the validation process, constructs
independent direct and Broker Runtime snapshots, exercises the complete Broker
workflow, evaluates multilingual search quality, and records index/search timing.

## Authoritative tests

### Targeted stable-catalog Broker suite

```text
121 passed, 214 subtests passed in 12.01s
```

Covered sanitizer, search, result budget, fixed catalog, Broker discovery/calls,
ResultStore, close lifecycle, Admin restart-only configuration, schema drift, and
the integration contract.

### Authoritative full unittest discovery

Command:

```text
uv run --frozen python -m unittest discover -s tests -p "test_*.py"
```

Environment included explicit HOME/USERPROFILE and telemetry disabled.

Result:

```text
Ran 391 tests in 87.030s
OK (skipped=84)
```

The run emitted one non-failing `ResourceWarning` for implicit cleanup of an
existing test `TemporaryDirectory`; no failure or error was produced.

### Admin WebUI

```text
17 tests passed
formal build completed
```

`coding_tools_mcp/webui_dist/admin.html` was regenerated from `webui/src/**` and
matched the committed build output.

### Static and quality checks

- Fixed five Broker tools present in `TOOL_REGISTRY`: **PASS**
- Fixed local tool count: **25**
- `tool_profile` absent from upstream runtime routing: **PASS**
- `UpstreamManager.start_server()` absent: **PASS**
- `UpstreamManager.stop_server()` absent: **PASS**
- `git diff --check`: **PASS**
- Ruff for release validation script: **PASS**

## Runtime smoke results

### Legacy direct Runtime A

- `tools/list` count: **28** = 25 fixed local + 3 direct upstream
- upstream direct count: **3**
- upstream public definition bytes in direct context: **844**
- catalog count: **3**
- broker-only count: **0**

### Broker Runtime B

- `tools/list` count: **26** = 25 fixed local + 1 pinned upstream
- upstream direct count: **1**
- upstream public definition bytes in direct context: **308**
- catalog count: **3**
- broker-only count: **2**

The Broker configuration reduced direct upstream definition bytes from 844 to
308, a reduction of approximately **63.5%**, while retaining the complete catalog.

### Freeze and restart-only behavior

- Runtime A remained unchanged after the configuration file was rewritten to Broker mode.
- Runtime B, constructed after the write, used Broker exposure.
- Both Runtime initialization responses advertised `tools.listChanged=false`.
- No live profile switch or manager start/stop operation was executed.

### Broker workflow

Validated in sequence:

1. search → describe → readonly call for `release__search_repositories`;
2. search → describe → digest-bound mutating call for `release__create_issue`;
3. search → describe → large readonly call for `release__export_archive`;
4. overflow handle → `upstream_result_fetch` pagination;
5. cross-session fetch denial.

Large-result measurements:

- final inline envelope: **8,487 bytes**
- hard budget: **128,000 bytes**
- result handle: present
- first fetch page: **32,000 Unicode codepoints**
- cross-session result: `UPSTREAM_RESULT_NOT_FOUND`

Raw-only metadata, the raw canary, and `$defs` did not appear in search,
describe, or `tools/list` public output.

## Search quality and performance

Search quality used 30 English/Chinese fixtures covering repository, issue,
branch, file, database, deployment, browser, literature, NMR, spectrum, record,
and Git workflows.

```text
top-1: 30 / 30 (100%)
top-5: 30 / 30 (100%)
```

Performance report for a synthetic 500-tool catalog:

```text
build iterations: 25
build p50: 85.8537 ms
build p95: 93.2954 ms
search iterations: 600
search p50: 14.3123 ms
search p95: 20.2941 ms
```

These values are host-specific measurements, not hard product SLOs.

## Complete implementation commit chain

```text
1039d8a docs(broker): freeze upstream broker implementation baseline
878cf3b docs(broker): adapt taskbook to stable catalog contract
6da9b73 refactor(upstream): freeze registry in immutable runtime state
1533bca feat(upstream): contain untrusted tool metadata
e167acf fix(upstream): enforce hard result envelope budgets
9c243ef feat(upstream): add field-weighted broker search index
cb8e832 feat(upstream): freeze catalog and exposure at runtime startup
1290721 feat(broker): add fixed upstream discovery tools
055f35d feat(broker): validate and dispatch fixed upstream calls
f88ea19 feat(broker): add session-scoped upstream result paging
86b7306 fix(upstream): harden runtime call and close lifecycle
414aec6 docs(config): document restart-only upstream broker exposure
```

## Explicitly deferred scope

The following remain outside stable-catalog Broker v6:

- dynamic `tools/listChanged`;
- live profile filtering or activation;
- Runtime-local upstream start/stop/reload;
- OAuth principal + MCP session composite ResultStore ownership;
- persistent ResultStore data;
- vector search;
- complete JSON Schema `$ref` resolution;
- `expose_mode=auto`.

## Release disposition

The branch is ready for review or merge as a stable-catalog Broker implementation.
No remote push, pull request, merge, tag, or release publication was performed by T11.
