# SWE-bench Evaluation

SWE-bench is the external benchmark path for validating whether this MCP runtime can support issue-fixing agents, not just unit tests. The official harness is Docker-based and evaluates prediction files containing patches.

## Current Artifacts

- Smoke report: [../reports/benchmark/swebench-regression.md](../reports/benchmark/swebench-regression.md)
- Smoke JSON: [../reports/benchmark/swebench-regression.json](../reports/benchmark/swebench-regression.json)
- Official attempt report: [../reports/benchmark/swebench-official-attempt.md](../reports/benchmark/swebench-official-attempt.md)
- Official attempt JSON: [../reports/benchmark/swebench-official-attempt.json](../reports/benchmark/swebench-official-attempt.json)
- Official attempt raw logs: [../reports/benchmark/swebench-official-attempt/raw](../reports/benchmark/swebench-official-attempt/raw)
- Subset: [../benchmarks/swebench/subsets/smoke-lite-10.json](../benchmarks/swebench/subsets/smoke-lite-10.json)

Default local smoke conclusion: `PREFLIGHT_ONLY`.

Explicit official-harness attempt conclusion in this container: `BLOCKED`.

Recorded blocker categories:

- Docker executable or daemon unavailable.
- Official `swebench` harness unavailable or import/help path fails.
- Checked-in baseline and candidate prediction files are schema-valid placeholders, not real model-generated patches.

The repository must not claim SWE-bench pass until official harness results exist.

The GitHub Actions `swebench-lite` workflow defaults to `prediction_source=reference_patch`.
That mode generates non-empty prediction JSONL files from the SWE-bench Lite
reference patches before invoking the official harness. It is an official
harness sanity check with parsed resolved counts, not a native-vs-MCP model
leaderboard result.

## Official Attempt Command

```bash
python benchmarks/swebench/run_smoke.py \
  --install-swebench \
  --run-evaluation \
  --require-evaluation-pass \
  --instance-id sympy__sympy-12419 \
  --max-workers 1 \
  --report-json reports/benchmark/swebench-official-attempt.json \
  --report-md reports/benchmark/swebench-official-attempt.md
```

The preferred execution path is the manual GitHub Actions workflow, because the
local Codex container may not have Docker:

```bash
gh workflow run swebench-lite.yml \
  --ref recover-rollout-2026-05-16 \
  -f instance_ids=sympy__sympy-12419 \
  -f max_workers=1 \
  -f prediction_source=reference_patch \
  -f install_swebench=true \
  -f require_evaluation_pass=true
```

The workflow uploads `reports/benchmark/**`, including raw harness stdout/stderr,
captured `logs/run_evaluation` files, prediction paths, Docker diagnostics, and
environment metadata. It fails by default unless the official harness runs with
non-placeholder baseline and MCP-candidate predictions, parses resolved counts,
and satisfies the comparison below. Use `prediction_source=checked_in` only when
real model-generated prediction files have replaced the scaffold files. Use
`require_evaluation_pass=false` only for diagnostic attempts that are expected
to end in `BLOCKED`.

## Acceptance Standard

```text
candidate_mcp_resolved >= baseline_native_resolved
```

Both numbers must come from official harness output over the same dataset subset and prediction-generation budget.
