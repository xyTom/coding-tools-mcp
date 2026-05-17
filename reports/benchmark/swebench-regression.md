# SWE-bench Smoke Regression Report

- Conclusion: **PREFLIGHT_ONLY**
- Dataset: `princeton-nlp/SWE-bench_Lite` split `test`
- Smoke subset: `/root/codex-tool-runtime-mcp/benchmarks/swebench/subsets/smoke-lite-10.json`
- Raw log directory: `reports/benchmark/swebench-regression/raw`
- Baseline predictions: `/root/codex-tool-runtime-mcp/benchmarks/swebench/predictions/baseline_native.jsonl`
- Candidate predictions: `/root/codex-tool-runtime-mcp/benchmarks/swebench/predictions/candidate_mcp.jsonl`
- Baseline resolved: `None`
- Candidate resolved: `None`
- Baseline completed: `None` / `None`
- Candidate completed: `None` / `None`

## Preflight

- docker: missing - docker executable not found
- swebench package: missing - swebench harness help/import failed
- baseline predictions: 10 rows, placeholder=True
- candidate predictions: 10 rows, placeholder=True

## Instances

- `astropy__astropy-12907` (astropy)
- `django__django-11099` (django)
- `matplotlib__matplotlib-18869` (matplotlib)
- `pytest-dev__pytest-5221` (pytest)
- `psf__requests-2317` (requests)
- `scikit-learn__scikit-learn-10297` (scikit-learn)
- `sphinx-doc__sphinx-10325` (sphinx)
- `sympy__sympy-12419` (sympy)
- `pallets__flask-4992` (flask)
- `pydata__xarray-3364` (xarray)

## Evaluation Commands

```bash
/root/venv/bin/python3 -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --predictions_path /root/codex-tool-runtime-mcp/benchmarks/swebench/predictions/baseline_native.jsonl --max_workers 2 --run_id codex_tool_runtime_native_smoke --instance_ids astropy__astropy-12907 django__django-11099 matplotlib__matplotlib-18869 pallets__flask-4992 psf__requests-2317 pydata__xarray-3364 pytest-dev__pytest-5221 scikit-learn__scikit-learn-10297 sphinx-doc__sphinx-10325 sympy__sympy-12419
/root/venv/bin/python3 -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --predictions_path /root/codex-tool-runtime-mcp/benchmarks/swebench/predictions/candidate_mcp.jsonl --max_workers 2 --run_id codex_tool_runtime_mcp_smoke --instance_ids astropy__astropy-12907 django__django-11099 matplotlib__matplotlib-18869 pallets__flask-4992 psf__requests-2317 pydata__xarray-3364 pytest-dev__pytest-5221 scikit-learn__scikit-learn-10297 sphinx-doc__sphinx-10325 sympy__sympy-12419
```

## Harness Reports

### Baseline
- No harness report artifacts captured.
### Candidate
- No harness report artifacts captured.

## Limitations

- Prediction files are schema-valid placeholders, not model-generated patches.
- Official SWE-bench evaluation requires a working Docker daemon.
- Official SWE-bench evaluation requires an importable swebench harness.
