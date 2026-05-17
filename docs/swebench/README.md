# SWE-bench Operator Notes

The default local gate is:

```bash
make benchmark-smoke
```

The official-attempt path is:

```bash
python benchmarks/swebench/run_smoke.py --install-swebench --run-evaluation --require-evaluation-pass --instance-id sympy__sympy-12419 --max-workers 1 --report-json reports/benchmark/swebench-official-attempt.json --report-md reports/benchmark/swebench-official-attempt.md
```

Prefer `.github/workflows/swebench-lite.yml` for official attempts. It runs on
GitHub-hosted Linux, records Docker and environment diagnostics, uploads
`reports/benchmark/**`, and defaults to `prediction_source=reference_patch`.
That mode generates non-empty reference-patch predictions and fails unless the
official harness produces parsed resolved counts with
`candidate_mcp_resolved >= baseline_native_resolved`. Use
`prediction_source=checked_in` only after replacing the scaffold files with
model-generated predictions.

The current checked-in official-attempt artifact is `reports/benchmark/swebench-official-attempt.md`.
