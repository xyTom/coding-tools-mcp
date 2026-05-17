# SWE-bench Evaluation

SWE-bench is the external benchmark path for validating whether this MCP runtime can support issue-fixing agents, not just unit tests. The official harness is Docker-based and evaluates prediction files containing patches.

## Current Artifacts

- Smoke report: [../reports/benchmark/swebench-regression.md](../reports/benchmark/swebench-regression.md)
- Smoke JSON: [../reports/benchmark/swebench-regression.json](../reports/benchmark/swebench-regression.json)
- Official attempt report: [../reports/benchmark/swebench-official-attempt.md](../reports/benchmark/swebench-official-attempt.md)
- Official attempt JSON: [../reports/benchmark/swebench-official-attempt.json](../reports/benchmark/swebench-official-attempt.json)
- Official attempt raw logs: [../reports/benchmark/swebench-official-attempt/raw](../reports/benchmark/swebench-official-attempt/raw)
- Subset: [../benchmarks/swebench/subsets/smoke-lite-10.json](../benchmarks/swebench/subsets/smoke-lite-10.json)

Default local/CI smoke conclusion: `PREFLIGHT_ONLY`.

Explicit official-harness attempt conclusion in this container: `BLOCKED`.

Recorded blocker categories:

- Docker executable or daemon unavailable.
- Official `swebench` harness unavailable or import/help path fails.
- Baseline and candidate prediction files are schema-valid placeholders, not real model-generated patches.

The repository must not claim SWE-bench pass until official harness results exist.

## Official Attempt Command

```bash
python benchmarks/swebench/run_smoke.py \
  --install-swebench \
  --run-evaluation \
  --allow-placeholder-evaluation \
  --instance-id sympy__sympy-12419 \
  --max-workers 1 \
  --report-json reports/benchmark/swebench-official-attempt.json \
  --report-md reports/benchmark/swebench-official-attempt.md
```

## Acceptance Standard

```text
candidate_mcp_resolved >= baseline_native_resolved
```

Both numbers must come from official harness output over the same dataset subset and prediction-generation budget.
