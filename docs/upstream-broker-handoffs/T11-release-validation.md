# T11 Handoff — Stable-Catalog Upstream Broker Release Validation

Status: **complete (superseded by T12 final gate)**

> **Historical evidence only.** T11 is not a current merge-readiness source.
> Test counts, implementation notes, and review ordering below are point-in-time
> records and may be obsolete after T12. Current evidence is exclusively:
> `T12-post-review-remediation.md`, `upstream-broker-traceability.md`, the current
> generated reports, and a hash-stable independent final review.

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

The original T11 validation commit added no product runtime behavior; it added one
repeatable release-validation script, one machine-readable result report, and
this handoff. Post-review runtime and protocol changes are now owned by T12. The historical detail below is retained as review evidence, not as part of the original T11 no-feature-change claim.

## Post-review remediation

Three independent merge-readiness review rounds against HEAD
`60ef13de6b33390fef7a17fc07991c6d4a33d3a6` found defects that were not covered
by the original T00-T11 regression set. The working tree closes those findings
under the T12 post-review remediation task card, without implementing deferred dynamic scope.

Closed in the first review round:

- `upstream_tool_call_mutating` keeps truthful mutating annotations under the
  compatibility fake-readonly override;
- Manager-level call leases cover remote I/O, normalization, result budgeting,
  ResultStore writes, and handle injection;
- untrusted public Schema `pattern` values are removed;
- Streamable HTTP session requests and notifications send
  `MCP-Protocol-Version`;
- retained `not` and `uniqueItems` constraints are enforced;
- nested `$ref` nodes degrade without forcing object arguments;
- MCP and upstream JSON boundaries reject non-finite numbers;
- per-server observability is aggregate-only;
- English queries expand back to Chinese synonym keys;
- README and the machine-readable integration contract are synchronized.

Closed in the second review round:

- required result-handle metadata is budgeted before the final size check,
  survives all fallbacks, and the Manager defensively rechecks the serialized
  envelope against the 128,000-byte hard limit;
- `uniqueItems` uses canonical typed fingerprints rather than an O(n²) pairwise
  comparison, with a 10,000-item performance regression;
- `const`, `enum`, and `uniqueItems` use JSON-value equality semantics, so
  booleans remain distinct from numbers while numerically equal JSON numbers
  compare equal;
- unresolved or assertion-free branches in `allOf`, `anyOf`, and `oneOf` degrade
  conservatively without creating false multi-match rejection;
- the two Broker passthrough call tools omit `outputSchema`, allowing legal
  content-only upstream results to remain unchanged;
- all fake-readonly warnings, docstrings, startup messages, and CLI help now
  disclose the `upstream_tool_call_mutating` exemption;
- the modified stdio transport is type-clean; mypy now reports only the two
  pre-existing baseline errors in `tool_results.py` and `server.py`.

Closed in the third review round:

- numeric fingerprints use `Decimal(str(value))` without context-sensitive
  normalization, preserving exact equality for adjacent 30-, 100-, 1,000-, and
  4,000-digit integers while retaining `1 == 1.0` and `-0 == 0`;
- restrictive cardinality budgets are widen-only: oversized `enum`, `anyOf`,
  and `oneOf` constraints are removed, excess `allOf` branches are omitted, and
  truncated property maps no longer retain restrictive `additionalProperties`;
- partially sanitized assertions inside `not` and `oneOf` are treated as
  unknown, preventing containment from becoming stricter through negation or
  exclusive matching;
- real Broker regressions cover the 51st enum value, 41st declared property,
  schema-valued `additionalProperties`, and 11th `anyOf`/`oneOf` branch.

Closed in the third review round:

- one shared strict JSON loader now covers MCP stdio/HTTP, Admin JSON bodies,
  OAuth dynamic client registration, and upstream stdio/HTTP/SSE;
- 4,300-digit integers remain valid, while 4,301-digit integers produce stable
  parse/protocol errors without terminating the stdio loop or HTTP request
  handling;
- floating-point literals such as `1e309` that overflow to infinity are rejected
  at the same boundaries;
- `SPEC.md` and `docs/competitive-analysis.md` now report the fixed 25-tool
  catalog, and the spec lists all five Broker tools.

Additional final-review protocol closure:

- byte transport input is explicitly UTF-8 only; UTF-16/UTF-32 are rejected;
- decoder `RecursionError` is normalized to parse/protocol errors and does
  not terminate stdio readers or HTTP request handling;
- the project 4,300-digit integer limit is independent of Python's
  process-global `int_max_str_digits` setting.
- production MCP stdio uses raw binary pipes with UTF-8 decoding/encoding and
  LF framing, independent of Windows GBK/CP936 text-wrapper defaults;
- stdio response encoding preserves ordinary Unicode as real UTF-8 and falls
  back to ASCII JSON escapes for unpaired high/low surrogates, so adversarial
  `\ud800`/`\udc00` values cannot terminate the process;
- the real subprocess regression also verifies emoji output remains real UTF-8
  and a subsequent `tools/list` request succeeds.
- a real `python -m coding_tools_mcp --stdio` subprocess regression runs with
  `PYTHONUTF8=0` and no `PYTHONIOENCODING`, preserves Chinese UTF-8
  paths/content, returns `-32700` for invalid UTF-8, then continues.

Historical T11 validation result (superseded by T12):

```text
192 focused tests passed in 35.477s (skipped=37)
423 full-discovery tests passed in 80.622s
OK (skipped=84)
```

The release-validation script passed again with 30/30 top-1 and 30/30 top-5
search fixtures. The tracked machine-readable report was regenerated with
`uv run --frozen python scripts/validate_upstream_broker_release.py --output reports/upstream-broker-v6-release-validation.json`.
`git diff --check` and Ruff passed for all changed Python files.

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
Ran 423 tests in 80.622s
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

The post-review merge blockers are closed in the working tree. The implementation
is ready for final review after these changes are committed. No remote push, pull
request, merge, tag, or release publication was performed.
