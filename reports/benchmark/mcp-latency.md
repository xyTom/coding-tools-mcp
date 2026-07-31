# MCP Runtime Latency Benchmark

- Conclusion: **PASS**
- Endpoint: `http://127.0.0.1:40743/mcp`
- Iterations: `8`
- Exec iterations: `4`
- Warmup iterations: `2`
- Max MCP p95 threshold: `5000 ms`

## Metrics

| metric | samples | min ms | p50 ms | p95 ms | max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| `mcp.tools_list` | 8 | 2.08 | 2.136 | 3.08 | 3.463 |
| `mcp.read_file` | 8 | 1.307 | 1.371 | 1.752 | 1.808 |
| `mcp.search_text` | 8 | 6.932 | 7.51 | 9.7 | 10.177 |
| `mcp.exec_command` | 4 | 48.944 | 50.249 | 51.329 | 51.387 |
| `native.read_text` | 8 | 0.042 | 0.044 | 0.049 | 0.05 |
| `native.search` | 8 | 5.032 | 5.37 | 6.885 | 6.935 |
| `native.exec_command` | 4 | 2.217 | 4.321 | 5.823 | 6.085 |

## Native Baseline Comparison

| operation | MCP p95 ms | native p95 ms | ratio |
| --- | ---: | ---: | ---: |
| `read_file` | 1.752 | 0.049 | 35.755 |
| `search_text` | 9.7 | 6.885 | 1.409 |
| `exec_command` | 51.329 | 5.823 | 8.815 |

## Failures

No failures recorded.

## Notes

- Native baselines are local developer-tool primitives, not equivalent MCP substitutes.
- Latency thresholds are intentionally broad; this smoke benchmark catches transport regressions and records trend evidence.
