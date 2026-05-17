# SWE-bench Smoke Regression Report

- Conclusion: **BLOCKED**
- Dataset: `princeton-nlp/SWE-bench_Lite` split `test`
- Smoke subset: `/root/codex-tool-runtime-mcp/benchmarks/swebench/subsets/smoke-lite-10.json`
- Raw log directory: `reports/benchmark/swebench-official-attempt/raw`
- Baseline predictions: `reports/benchmark/swebench-reference-predictions/baseline_reference.jsonl`
- Candidate predictions: `reports/benchmark/swebench-reference-predictions/candidate_reference.jsonl`
- Baseline resolved: `None`
- Candidate resolved: `None`
- Baseline completed: `None` / `None`
- Candidate completed: `None` / `None`

## Preflight

- docker: missing - docker executable not found
- swebench package: missing - swebench harness help/import failed
- baseline predictions: 1 rows, placeholder=False
- candidate predictions: 1 rows, placeholder=False

## Instances

- `sympy__sympy-12419` (sympy)

## Evaluation Commands

```bash
/root/venv/bin/python3 -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --predictions_path reports/benchmark/swebench-reference-predictions/baseline_reference.jsonl --max_workers 1 --run_id codex_tool_runtime_native_smoke --instance_ids sympy__sympy-12419
/root/venv/bin/python3 -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --predictions_path reports/benchmark/swebench-reference-predictions/candidate_reference.jsonl --max_workers 1 --run_id codex_tool_runtime_mcp_smoke --instance_ids sympy__sympy-12419
```

## Harness Reports

### Baseline
- No harness report artifacts captured.
### Candidate
- No harness report artifacts captured.

## Limitations

- Official SWE-bench evaluation requires a working Docker daemon.
- Official SWE-bench evaluation requires an importable swebench harness.
- Evaluation was requested but preflight/resource checks prevent a valid comparison.
