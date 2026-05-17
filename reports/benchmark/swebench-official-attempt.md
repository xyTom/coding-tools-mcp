# SWE-bench Smoke Regression Report

- Conclusion: **BLOCKED**
- Dataset: `princeton-nlp/SWE-bench_Lite` split `test`
- Smoke subset: `/root/codex-tool-runtime-mcp/benchmarks/swebench/subsets/smoke-lite-10.json`
- Raw log directory: `reports/benchmark/swebench-official-attempt/raw`
- Baseline predictions: `/root/codex-tool-runtime-mcp/benchmarks/swebench/predictions/baseline_native.jsonl`
- Candidate predictions: `/root/codex-tool-runtime-mcp/benchmarks/swebench/predictions/candidate_mcp.jsonl`
- Baseline resolved: `None`
- Candidate resolved: `None`

## Preflight

- docker: missing - docker executable not found
- swebench package: missing - swebench harness help/import failed
- baseline predictions: 1 rows, placeholder=True
- candidate predictions: 1 rows, placeholder=True

## Instances

- `sympy__sympy-12419` (sympy)

## Evaluation Commands

```bash
/root/venv/bin/python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --predictions_path /root/codex-tool-runtime-mcp/benchmarks/swebench/predictions/baseline_native.jsonl --max_workers 1 --run_id codex_tool_runtime_native_smoke --instance_ids sympy__sympy-12419
/root/venv/bin/python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --predictions_path /root/codex-tool-runtime-mcp/benchmarks/swebench/predictions/candidate_mcp.jsonl --max_workers 1 --run_id codex_tool_runtime_mcp_smoke --instance_ids sympy__sympy-12419
```

## Limitations

- Prediction files are schema-valid placeholders, not model-generated patches.
- Official SWE-bench evaluation requires a working Docker daemon.
- Official SWE-bench evaluation requires an importable swebench harness.
- Evaluation was requested but preflight/resource checks prevent a valid comparison.
