# MCP Runtime Latency Benchmark

- Conclusion: **PASS**
- Endpoint: `http://127.0.0.1:35403/mcp`
- Iterations: `8`
- Exec iterations: `4`
- Warmup iterations: `2`
- Max MCP p95 threshold: `5000 ms`

## Metrics

| metric | samples | min ms | p50 ms | p95 ms | max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| `mcp.tools_list` | 8 | 0.942 | 1.06 | 1.133 | 1.141 |
| `mcp.read_file` | 8 | 0.598 | 0.635 | 0.762 | 0.767 |
| `mcp.search_text` | 8 | 60.903 | 64.047 | 70.773 | 71.299 |
| `mcp.exec_command` | 4 | 46.752 | 47.041 | 47.106 | 47.107 |
| `native.read_text` | 8 | 0.033 | 0.033 | 0.035 | 0.035 |
| `native.search` | 8 | 3.952 | 4.15 | 4.417 | 4.433 |
| `native.exec_python` | 4 | 24.93 | 25.068 | 25.875 | 26.008 |

## Native Baseline Comparison

| operation | MCP p95 ms | native p95 ms | ratio |
| --- | ---: | ---: | ---: |
| `read_file` | 0.762 | 0.035 | 21.771 |
| `search_text` | 70.773 | 4.417 | 16.023 |
| `exec_command` | 47.106 | 25.875 | 1.821 |

## Failures

No failures recorded.

## Notes

- Native baselines are local developer-tool primitives, not equivalent MCP substitutes.
- Latency thresholds are intentionally broad; this smoke benchmark catches transport regressions and records trend evidence.
