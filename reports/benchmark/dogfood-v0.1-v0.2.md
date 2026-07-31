# Deterministic Dogfood Before/After

- Baseline versions: `0.1.7`
- Candidate versions: `0.2.0`
- Repetitions: `5` baseline / `5` candidate
- Pass rate: `1.0` baseline / `1.0` candidate

| metric | baseline median | candidate median | change |
| --- | ---: | ---: | ---: |
| `completion_rate` | 1.0 | 1.0 | 0.0% |
| `total_elapsed_ms` | 1875.665 | 1881.306 | 0.301% |
| `tool_call_count` | 18.0 | 18.0 | 0.0% |
| `argument_bytes` | 1682.0 | 1682.0 | 0.0% |
| `result_bytes` | 18898.0 | 11853.0 | -37.279% |
| `first_patch_success_rate` | 1.0 | 1.0 | 0.0% |
| `session_poll_count` | 0.0 | 0.0 | n/a |
| `tool_latency_p50_ms` | 4.214 | 4.717 | 11.936% |
| `tool_latency_p95_ms` | 337.949 | 329.82 | -2.405% |

## Interpretation Boundary

This uses the same deterministic MCP-only runner, fixture, task order, and machine.
It measures runtime/tool-contract regression, not model quality and not Codex, OpenCode,
or Devspace end-to-end performance. Timing samples are local and should be treated as
directional; completion, call counts, polling, and serialized byte counts are deterministic.
