# SWE-bench Evaluation

SWE-bench's official Docker-backed harness evaluates patch predictions. This
repository uses it for advisory deterministic smoke evidence. It does not measure
model-generated solve rates or agent quality.

## Run and interpret current evidence

The [pinned benchmark runbook](../benchmarks/swebench/README.md) is the authoritative
source for commands, immutable inputs, workflow modes, and acceptance criteria.
It documents both the reference-patch harness control and the actual HTTP MCP
repair replay of `sympy__sympy-12419`.

The workflow defaults to `prediction_source=both`. A local `make benchmark-smoke`
is `PREFLIGHT_ONLY`; an explicit evaluation without Docker or the pinned harness
is `BLOCKED`. A replay pass establishes the tool path only. Official resolution
claims require complete fresh harness reports and a nonzero successful control.

For a workflow run, inspect its `swebench-lite-evidence` artifact. Current runs
include `attempt.json` with source/run identifiers and only that attempt's
selected evidence. Missing reports, a successful advisory workflow, or an older
checked-in result cannot establish an official pass for a new source commit.
See the runbook for `ERROR`, `INCONCLUSIVE`, and per-attempt raw-log handling.

## Historical checked-in artifacts

These preserved examples describe the runs recorded inside each artifact; they
are not current-head verification. Check their repository, source SHA, run ID,
prediction mode, and limitations before citing them.

- Smoke report: [swebench-regression.md](../reports/benchmark/swebench-regression.md)
- Smoke JSON: [swebench-regression.json](../reports/benchmark/swebench-regression.json)
- Official attempt report: [swebench-official-attempt.md](../reports/benchmark/swebench-official-attempt.md)
- Official attempt JSON: [swebench-official-attempt.json](../reports/benchmark/swebench-official-attempt.json)
- Official attempt raw logs: [swebench-official-attempt/raw](../reports/benchmark/swebench-official-attempt/raw)
- Pinned subset: [smoke-lite-10.json](../benchmarks/swebench/subsets/smoke-lite-10.json)
