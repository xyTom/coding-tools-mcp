# SWE-bench Operator Notes

The default local gate is:

```bash
make benchmark-smoke
```

The official-attempt path is:

```bash
python benchmarks/swebench/run_smoke.py --install-swebench --run-evaluation --allow-placeholder-evaluation --instance-id sympy__sympy-12419 --max-workers 1 --report-json reports/benchmark/swebench-official-attempt.json --report-md reports/benchmark/swebench-official-attempt.md
```

The current checked-in official-attempt artifact is `reports/benchmark/swebench-official-attempt.md`.
