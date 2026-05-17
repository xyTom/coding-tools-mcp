# SWE-bench Smoke Regression

This directory contains the regression scaffolding for the benchmark path:

- `subsets/smoke-lite-10.json`: fixed SWE-bench Lite smoke subset.
- `predictions/baseline_native.jsonl`: schema-valid baseline prediction scaffold.
- `predictions/candidate_mcp.jsonl`: schema-valid candidate MCP prediction scaffold.
- `generate_reference_predictions.py`: generates non-empty reference-patch
  predictions for official harness sanity runs.
- `run_smoke.py`: preflight/report script and optional official harness launcher.

The checked-in prediction files are placeholders with empty patches. They are
valid JSONL inputs for the official harness, but they are not a meaningful
native-vs-MCP comparison until a real baseline runner and MCP runner generate
patches.

Preflight/report:

```bash
python benchmarks/swebench/run_smoke.py
```

Official evaluation, when Docker and `swebench` are available and real
predictions have been generated:

```bash
python benchmarks/swebench/run_smoke.py --run-evaluation
```

Official harness sanity with reference patches:

```bash
make swebench-reference-predictions
python benchmarks/swebench/run_smoke.py \
  --run-evaluation \
  --require-evaluation-pass \
  --instance-id sympy__sympy-12419 \
  --baseline-predictions reports/benchmark/swebench-reference-predictions/baseline_reference.jsonl \
  --candidate-predictions reports/benchmark/swebench-reference-predictions/candidate_reference.jsonl
```

Reference patches validate that the Docker/SWE-bench harness path can produce
resolved counts. They are not model-generated benchmark predictions.
