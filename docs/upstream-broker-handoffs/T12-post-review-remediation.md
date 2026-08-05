# T12 Handoff — Post-Review Remediation

Status: **current review blockers remediated and validation complete; merge-ready pending independent final review and repository-state gates**

Branch: `feat/upstream-tool-broker-v6`

## Closed review findings

- Admin/WebUI now consumes the aggregate-only `exposure_report.servers[]` contract;
- the complete 4,300-digit JSON lifecycle no longer depends on Python's process-global integer-to-string limit: input parsing, Schema fingerprints and bound errors, canonical Schema JSON, result budgeting, MCP output, and upstream request output;
- non-finite output numbers fail closed and unpaired surrogates fall back to legal ASCII JSON escapes;
- surrogate-containing Schema/tool metadata cannot abort sanitizer or Runtime construction;
- surrogate-containing upstream results are preserved through UTF-8/ASCII JSON fallback and remain budgetable;
- upstream stdio request encoding is separated from pipe I/O classification: surrogate-bearing requests use the binary-buffer fallback, while closed pipes return retryable `UPSTREAM_DISCONNECTED` rather than a protocol error;
- upstream stdio response frames are read from the binary pipe with a 1 MiB hard limit; oversized frames return `UPSTREAM_RESPONSE_TOO_LARGE`, are drained with bounded chunks, and do not corrupt the next frame;
- parseable but deeply nested Schema assertion projections are bounded by the sanitizer containment depth; discovery performs no recursive pre-sanitizer copy, and raw/public snapshots use iterative freezing, so a 500-level Schema cannot abort Runtime construction;
- upstream `structuredContent`, when present, must be an object; result cloning is iterative and bounded to 64 levels, so invalid shapes or excessive legal nesting become stable non-retryable `UPSTREAM_PROTOCOL_ERROR` results;
- JSON container depth is uniform across upstream results, `structuredContent`, JSON-RPC errors, and error details: the root dict/list is level 1, child containers add one, scalars add none, 64 is accepted, and 65 is rejected;
- upstream JSON-RPC envelopes require an exact integer response ID equal to the request ID and an exact integer `error.code`; missing values plus boolean, floating-point, and string lookalikes are rejected as protocol errors;
- error details are bounded per untrusted top-level value without charging Gateway or status wrappers; `UpstreamStatus.payload()` preserves safe code/message/category/retryable fields and bounds only details, while excessive nesting, NaN/Infinity, or over-4,300-digit integers become bounded omissions before strict JSON output;
- cycles or shared containers inside one detail value are omitted, while cross-top-level Python identity sharing is normalized independently by JSON value;
- production and compliance stdio clients explicitly use UTF-8 rather than the Windows code page;
- call-after-close returns retryable `UPSTREAM_NOT_AVAILABLE`, while actual transport disconnects remain `UPSTREAM_DISCONNECTED`;
- `CatalogSearchIndex` is single-build and its published entries, fields, and tuple-backed tokenizer synonyms, reverse synonyms, and known phrases are immutable even through `object.__setattr__`;
- raw/public definition trees use non-`dict`/non-`list` immutable Mapping/Sequence wrappers, closing direct `dict.__setitem__` and `list.__setitem__` bypasses, while iterative thaw lets `deepcopy()` export ordinary mutable JSON even for the registered 500-level raw Schema regression;
- mutating digest validation and error text consistently accept stateless digests from search or describe, with a direct search-digest-to-mutating-call regression;
- post-review protocol changes are isolated under T12;
- stale Appendix A moved to a non-normative historical archive;
- stable-catalog ADR and a normative 25-requirement-row traceability matrix added; it covers call leases, fake-readonly exemptions, Schema containment, protocol-version headers, handle budgeting, typed equality, passthrough schemas, strict JSON boundaries, bounded error details, and immutable snapshots.

## Validation evidence

### Focused remediation suite

```text
Ran 140 tests in 15.858s
OK
```

Additional final targeted checks cover exact integer JSON-RPC response IDs and SSE selection, required exact integer `error.code`, exact 63/64/65 container boundaries through `_rpc_result()`, Gateway mapping, real `UpstreamStatus.payload()`, `result_json_bytes()`, and `strict_json_bytes()`, root-level result depth semantics, strict omission of NaN/Infinity/over-4,300-digit error details through the real Manager path, shared-container scope, 497-level parsed JSON-RPC errors, 500-level mutable raw/public snapshot export, immutable snapshots, UTF-8 compliance client, and type-compatible mutators.

### WebUI

```text
npm --prefix webui test
17 tests passed

npm --prefix webui run build
PASS
```

The WebUI test fixture uses the real aggregate backend shape and asserts that no `servers[].tools` array is required.

### Linux compliance gate

Executed from a native WSL2 Ubuntu 24.04 ext4 copy of the current worktree with Python 3.12, Linux Node/npm, an isolated HOME, and a `python -> python3` test-command shim:

```text
make PYTHON=python3 compliance
Ran 89 tests in 54.539s
OK
```

The passing `reports/compliance/latest.json` and `latest.md` were copied back to the tracked worktree. The final report records `commit = 60ef13de6b33390fef7a17fc07991c6d4a33d3a6+dirty`, `passed = true`, `tests_run = 89`, and `elapsed_seconds = 51.649`. Windows execution is not authoritative for this suite because its fixtures intentionally require POSIX TTY, permission-bit, `select()`, and `/dev/null` semantics.

### Authoritative Windows full discovery

Isolated environment:

- `HOME` / `USERPROFILE`: `D:\Partition\TEMP\coding-tools-mcp\broker-v6-t12-review9-home`
- isolated `APPDATA`: `D:\Partition\TEMP\coding-tools-mcp\broker-v6-t12-review9-appdata`;
- isolated `LOCALAPPDATA`: `D:\Partition\TEMP\coding-tools-mcp\broker-v6-t12-review9-localappdata`;
- isolated `TEMP` / `TMP`: `D:\Partition\TEMP\coding-tools-mcp\broker-v6-t12-review9-temp`;
- `CODING_TOOLS_MCP_TELEMETRY=off`.

```text
Ran 445 tests in 64.232s
OK (skipped=84)
```

### Release validation

Regenerated with:

```text
uv run --frozen python scripts/validate_upstream_broker_release.py   --output reports/upstream-broker-v6-release-validation.json
```

Result:

- status: `pass`;
- fixed local tool count: `25`;
- search top-1: `30/30`;
- search top-5: `30/30`;
- large-result handle: present;
- final envelope: `8,487 / 128,000` bytes.

### Static and type gates

- Ruff over `coding_tools_mcp` and `tests`: PASS;
- `compileall`: PASS;
- integration machine-contract regression: PASS;
- `git diff --check`: PASS before this handoff update and rerun after it;
- project Makefile-equivalent typecheck: `Success: no issues found in 38 source files`;
- stricter review scope still reports only the two known baseline production errors:
  - `coding_tools_mcp/tool_results.py:240`;
  - `coding_tools_mcp/server.py:2278`.

## Merge gate

The current review findings are remediated and the validation gates pass. This does not make commit the sole remaining gate:

- an independent final review must confirm that no Standards or Spec blocker remains;
- only after that review may all tracked and untracked T12 files enter a commit;
- the commit chain must then be reviewed;
- `git status --short` must be empty before merge readiness can be declared.

Until every step is explicitly completed, `merge-ready = no`.
