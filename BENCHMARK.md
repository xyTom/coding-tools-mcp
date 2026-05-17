# Benchmark And Regression

This project includes deterministic MCP dogfood plus a SWE-bench smoke/regression scaffold.

Detailed operator docs:

- [docs/dogfood.md](docs/dogfood.md)
- [docs/swe-bench.md](docs/swe-bench.md)

## Dogfood

Command:

```bash
make dogfood-smoke
```

Reports:

- [reports/dogfood/codex-on-mcp.md](reports/dogfood/codex-on-mcp.md)
- [reports/dogfood/codex-on-mcp.json](reports/dogfood/codex-on-mcp.json)
- [docs/dogfood/codex-on-mcp-transcript.json](docs/dogfood/codex-on-mcp-transcript.json)

Current conclusion: `PASS`.

The runner completes repository inspection, JavaScript and Python failing/passing tests, patching, git status/diff, timeout handling, long-running stdin, kill/closed-session behavior, binary/image behavior with `view_image`, and workspace escape denial using MCP calls only.

## SWE-bench Smoke Scaffold

Command:

```bash
make benchmark-smoke
```

Artifacts:

- [benchmarks/swebench/subsets/smoke-lite-10.json](benchmarks/swebench/subsets/smoke-lite-10.json)
- [benchmarks/swebench/predictions/baseline_native.jsonl](benchmarks/swebench/predictions/baseline_native.jsonl)
- [benchmarks/swebench/predictions/candidate_mcp.jsonl](benchmarks/swebench/predictions/candidate_mcp.jsonl)
- [reports/benchmark/swebench-regression.md](reports/benchmark/swebench-regression.md)
- [reports/benchmark/swebench-regression.json](reports/benchmark/swebench-regression.json)
- [reports/benchmark/swebench-regression/raw](reports/benchmark/swebench-regression/raw)
- [reports/benchmark/swebench-official-attempt.md](reports/benchmark/swebench-official-attempt.md)
- [reports/benchmark/swebench-official-attempt/raw](reports/benchmark/swebench-official-attempt/raw)

Default smoke conclusion: `PREFLIGHT_ONLY`.

## MCP Runtime Latency

Command:

```bash
make benchmark-latency
```

Reports:

- [reports/benchmark/mcp-latency.md](reports/benchmark/mcp-latency.md)
- [reports/benchmark/mcp-latency.json](reports/benchmark/mcp-latency.json)

The latency smoke starts a local MCP HTTP server, measures `tools/list`,
`read_file`, `search_text`, and `exec_command`, and records direct local
baselines using Python file reads, `rg` or a Python search fallback, and a
native Python subprocess. It is trend evidence and a regression tripwire, not a
claim that MCP transport should be faster than direct local tool calls.

An explicit official-harness attempt is documented as `BLOCKED` in [reports/benchmark/swebench-official-attempt.md](reports/benchmark/swebench-official-attempt.md) when Docker or the official harness is unavailable. Checked-in predictions are schema-valid placeholders, not real model-generated patches, so they must not be used as score claims.

Manual official attempts run through [.github/workflows/swebench-lite.yml](.github/workflows/swebench-lite.yml). The workflow installs the harness, records Docker diagnostics, invokes `benchmarks/swebench/run_smoke.py --run-evaluation`, uploads `reports/benchmark/**`, and fails by default unless the official harness produces parsed resolved counts from real non-placeholder baseline and MCP-candidate predictions.

Official PASS requires:

```text
candidate_mcp_resolved >= baseline_native_resolved
```

Both numbers must come from official SWE-bench harness output over the same subset and prediction-generation budget.

## Remaining Benchmark TODOs

- Generate real baseline and MCP-candidate predictions instead of placeholder patches.
- Run at least one official SWE-bench Lite instance end-to-end with Docker.
- Save raw harness logs and resolved counts as artifacts from a successful official run.
- Use `.github/workflows/swebench-lite.yml` for manual official-harness attempts.
