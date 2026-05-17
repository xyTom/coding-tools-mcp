# Current Integration Report

This report summarizes the recovered rollout on branch `recover-rollout-2026-05-16`. It is local evidence until the branch is pushed and GitHub Actions is verified on the pushed SHA.

## Current Local Evidence

- Package version in editable install: `0.1.3`.
- `python -m pip install -e ".[dev]"`: passed.
- `make lint`: passed.
- `make typecheck`: passed.
- `make test`: passed, `56` tests.
- `make ci`: passed with lint, typecheck, unittest discovery, protocol/integration gates, docs/schema gates, dogfood smoke, and SWE-bench preflight.
- `make compliance`: passed, `56` tests, `suite: all`.
- `make dogfood-runner DOGFOOD_PORT=8765`: passed; deterministic HTTP dogfood report conclusion `PASS`.
- `make benchmark-smoke`: passed; default SWE-bench smoke conclusion `PREFLIGHT_ONLY`.
- Explicit official SWE-bench attempt command: ran and produced `BLOCKED` with raw pip-install and harness-help logs.

## Primary Artifacts

- Compliance report: [compliance/latest.md](compliance/latest.md)
- Dogfood report: [dogfood/codex-on-mcp.md](dogfood/codex-on-mcp.md)
- Dogfood transcript: [../docs/dogfood/codex-on-mcp-transcript.json](../docs/dogfood/codex-on-mcp-transcript.json)
- SWE-bench preflight: [benchmark/swebench-regression.md](benchmark/swebench-regression.md)
- SWE-bench official attempt: [benchmark/swebench-official-attempt.md](benchmark/swebench-official-attempt.md)
- SWE-bench official raw logs: [benchmark/swebench-official-attempt/raw](benchmark/swebench-official-attempt/raw)

## Implemented Improvements

- MCP protocol handling validates JSON-RPC envelopes, params objects, initialize protocol version, initialized state, cancellation notifications, stdio session continuity, and parse-error IDs.
- Security hardening includes shell-expansion gating, setuid/setgid executable rejection, secret-value environment filtering, Linux Landlock filesystem confinement, direct syscall read/write denial coverage, cancellation/kill cleanup, watchdog-backed session deadlines, bounded session buffers, and reader-thread drain on process exit.
- Observability includes opt-in `CODEX_TOOL_RUNTIME_TRACE=1` JSON tool-call traces on stderr with secret redaction and no stdout protocol pollution.
- Compliance report semantics no longer overclaim all required tools for partial suites; non-`all` suites mark required tool coverage as `not_measured`.
- CI now runs lint, typecheck, unittest discovery, protocol/integration gates, docs-required, schema-drift, dogfood smoke, SWE-bench preflight, full compliance refresh, and artifact upload.
- Docs-required and schema-drift gates fail on missing operator docs, evidence artifacts, workflow gates, or live schema/profile drift.

## SWE-bench Status

The official harness was genuinely attempted with:

```bash
python benchmarks/swebench/run_smoke.py --install-swebench --run-evaluation --allow-placeholder-evaluation --instance-id sympy__sympy-12419 --max-workers 1 --report-json reports/benchmark/swebench-official-attempt.json --report-md reports/benchmark/swebench-official-attempt.md
```

Result: `BLOCKED`.

Observed blockers:

- Docker executable is not available in this container.
- `pip install swebench` completed, but `python -m swebench.harness.run_evaluation --help` failed due a `pyarrow` API mismatch in the installed dependency set.
- Prediction files remain placeholders, so they cannot support score claims.

## Remaining Release Requirements

- Push the branch and verify GitHub Actions on the pushed SHA.
- Record final commit, CI run URL, and any release tag only after remote verification.
- Do not claim SWE-bench pass until Docker-backed official harness results exist with real baseline and MCP-candidate predictions and parsed resolved counts.
