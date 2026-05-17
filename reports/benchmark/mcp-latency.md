# MCP Runtime Latency Benchmark

- Conclusion: **PASS**
- Endpoint: `http://127.0.0.1:54291/mcp`
- Iterations: `8`
- Exec iterations: `4`
- Warmup iterations: `2`
- Max MCP p95 threshold: `5000 ms`

## Metrics

| metric | samples | min ms | p50 ms | p95 ms | max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| `mcp.tools_list` | 8 | 0.937 | 1.09 | 1.132 | 1.138 |
| `mcp.read_file` | 8 | 0.706 | 0.728 | 0.751 | 0.76 |
| `mcp.search_text` | 8 | 57.462 | 57.831 | 61.201 | 62.117 |
| `mcp.exec_command` | 4 | 46.454 | 46.721 | 46.803 | 46.807 |
| `native.read_text` | 8 | 0.034 | 0.035 | 0.046 | 0.048 |
| `native.search` | 8 | 4.049 | 4.164 | 4.392 | 4.422 |
| `native.exec_python` | 4 | 23.796 | 24.74 | 25.18 | 25.209 |

## Native Baseline Comparison

| operation | MCP p95 ms | native p95 ms | ratio |
| --- | ---: | ---: | ---: |
| `read_file` | 0.751 | 0.046 | 16.326 |
| `search_text` | 61.201 | 4.392 | 13.935 |
| `exec_command` | 46.803 | 25.18 | 1.859 |

## Failures

No failures recorded.

## Notes

- Native baselines are local developer-tool primitives, not equivalent MCP substitutes.
- Latency thresholds are intentionally broad; this smoke benchmark catches transport regressions and records trend evidence.
