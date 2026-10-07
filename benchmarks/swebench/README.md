# Pinned SWE-bench smoke evidence

These are **advisory deterministic checks**, not model-generated solve-rate or
agent-quality measurements. The release workflow does not depend on this job.
The default workflow runs both paths and uploads their separate reports:

1. `reference_patch`: a harness control that evaluates verified gold patches.
   Neither prediction file in this control demonstrates MCP runtime behavior.
2. `mcp_reference_replay`: a real HTTP MCP session edits a clean checkout of
   `sympy__sympy-12419` using a scripted reference repair. Only the diff returned
   by MCP becomes the candidate prediction for the official harness.

## Immutable inputs

`pins.json` is the source of truth for:

- Exact `swebench==4.1.0` harness version (an installed mismatch is rejected).
- Hugging Face dataset revision, original Parquet SHA-256, and the SHA-256 of
  `fixtures/smoke-lite-10.json`, the ten selected complete dataset rows.
- Benchmark instance, Git base commit, gold patch hash, source file hash, and
  expected repaired file hash.
- Linux/amd64 Docker manifest digests for all ten instances. The runner pulls
  each digest, assigns `coding-tools-pinned-v1` locally for the harness's tag-only
  interface, and verifies both references resolve to the same image ID. The
  recorded upstream `source_tag` is provenance only; runtime never pulls it.

Reference generation is offline. The official harness reads the verified local
JSON fixture, so its own dataset loader cannot silently move to a newer revision.
Updating pins is a reviewed fixture change, not part of a normal run.
See [fixture provenance](fixtures/README.md).

## Actual MCP replay

```bash
make swebench-mcp-replay
# Optional read-only local Git source; changes still happen in temporary clones:
python benchmarks/swebench/replay_mcp.py --source-checkout /path/to/sympy
```

The runner creates two clean temporary checkouts at the pinned base commit. The
native control uses `git apply` on the verified gold patch. A local
coding-tools-mcp HTTP server then performs:

`read_file → apply_patch → read_file → apply_changes(action=edit) → read_file → git_diff → exec_command`

The line-addressed edit uses the revision and line numbers from the intervening
MCP read. Final read-back must equal the native control byte-for-byte and match
the pinned result hash. The MCP-produced diff must exactly match the native diff.
MCP `exec_command` checks patch whitespace and Python syntax, polling when needed.
It does **not** claim that historical SymPy's tests ran on the host interpreter.

Outputs under `reports/benchmark/swebench-mcp-replay/` include the full MCP
transcript, server logs, runtime source commit, pins, a status report, and separate
native/MCP prediction JSONL files. Each attempt replaces the status report with
`INCONCLUSIVE` before clearing old predictions or loading inputs. A completed
replay records `PASS` or `FAIL`; a Ctrl-C interruption stays `INCONCLUSIVE`.
Failures and Ctrl-C interruptions remove the prediction outputs, including
partially written files. A forced process termination may prevent cleanup, so
only a `PASS` replay report identifies a completed prediction pair for the
subsequent stage. Other files and historical official-harness results are
preserved.
The subsequent official harness stage is the source of resolved-instance results.

```bash
python benchmarks/swebench/run_smoke.py \
  --install-swebench --run-evaluation --require-evaluation-pass \
  --prediction-source mcp_reference_replay \
  --instance-id sympy__sympy-12419 \
  --baseline-predictions reports/benchmark/swebench-mcp-replay/baseline_native.jsonl \
  --candidate-predictions reports/benchmark/swebench-mcp-replay/candidate_mcp.jsonl
```

## Reference-patch harness control

```bash
make swebench-reference-predictions
python benchmarks/swebench/run_smoke.py \
  --install-swebench --run-evaluation --require-evaluation-pass \
  --prediction-source reference_patch \
  --instance-id sympy__sympy-12419 \
  --baseline-predictions reports/benchmark/swebench-reference-predictions/baseline_reference.jsonl \
  --candidate-predictions reports/benchmark/swebench-reference-predictions/candidate_reference.jsonl
```

`python benchmarks/swebench/run_smoke.py` is preflight only. Checked-in
`predictions/*.jsonl` remain empty scaffolds and cannot yield a valid passing
comparison. Malformed, duplicate, missing, or partly empty selected predictions
are rejected. Unknown instance IDs fail rather than silently selecting no work.

## Outcomes and workflow

A replay `PASS` means the tool-path checks passed. An official `PASS` additionally
requires working Docker, the pinned harness, verified images, successful harness
processes, complete fresh per-instance reports, a nonzero native resolved count,
and an MCP/control count at least as high. Missing Docker is `BLOCKED`; incomplete
reports are `INCONCLUSIVE`. Invalid inputs or unexpected execution errors produce
current `ERROR` reports. A new attempt replaces prior summaries before preflight;
an interrupted attempt remains `INCONCLUSIVE`. Unique run IDs and raw-log
directories prevent reuse of stale harness reports.
`--raw-dir` selects the parent of the unique attempt directory; the report's
`raw_dir` field identifies the exact directory to inspect.

The workflow's default `prediction_source=both` runs the reference control and
actual MCP replay as separate evidence stages. `reference_patch`,
`mcp_reference_replay`, and `checked_in` can also be selected explicitly. The MCP
replay always targets its pinned SymPy instance; `instance_ids` selects the
reference/checked-in smoke subset. `source_ref` must be an immutable 40-character
commit SHA and the checked-out HEAD is verified. Release calls set
`blocking=false`; failures remain visible in reports and do not gate publication.
Each workflow attempt writes into its own runner-temporary evidence directory,
records its source/run identifiers in `attempt.json`, and uploads only that
directory. Checked-in historical reports and unselected control stages are never
included as current evidence. Missing reports mean that stage did not complete;
they are not evidence of a pass.

Pull requests changing the benchmark, its tests, or its workflows also run
`swebench-pr`: one pinned SymPy MCP replay and the native/MCP official evaluations,
with one worker and a 30-minute whole-job limit. It uses a read-only token, receives
no secrets, and cancels superseded PR evidence runs. This advisory check is not a
release dependency or a required branch-protection gate. Inspect its report
payloads, not just the workflow conclusion. Existing manual and release callers
retain the 180-minute default; `timeout_minutes` accepts integers from 1 to 180.

Run offline regression coverage with:

```bash
python -m unittest tests.test_swebench -v
```

This includes a real MCP-over-HTTP repair on the checked-in SymPy source fixture,
without network, Docker, model API access, or a historical Python environment.
