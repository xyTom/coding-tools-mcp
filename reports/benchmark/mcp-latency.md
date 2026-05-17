# MCP Runtime Latency Benchmark

- Conclusion: **PASS**
- Endpoint: `http://127.0.0.1:48803/mcp`
- Iterations: `8`
- Exec iterations: `4`
- Warmup iterations: `2`
- Max MCP p95 threshold: `5000 ms`

## Metrics

| metric | samples | min ms | p50 ms | p95 ms | max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| `mcp.tools_list` | 8 | 0.948 | 1.055 | 1.166 | 1.188 |
| `mcp.read_file` | 8 | 0.722 | 0.76 | 0.779 | 0.783 |
| `mcp.search_text` | 8 | 57.91 | 61.192 | 66.059 | 66.561 |
| `mcp.exec_command` | 4 | 46.722 | 46.866 | 47.126 | 47.163 |
| `native.read_text` | 8 | 0.034 | 0.035 | 0.036 | 0.036 |
| `native.search` | 8 | 4.0 | 4.155 | 4.33 | 4.39 |
| `native.exec_python` | 4 | 23.513 | 24.432 | 25.032 | 25.136 |

## Native Baseline Comparison

| operation | MCP p95 ms | native p95 ms | ratio |
| --- | ---: | ---: | ---: |
| `read_file` | 0.779 | 0.036 | 21.639 |
| `search_text` | 66.059 | 4.33 | 15.256 |
| `exec_command` | 47.126 | 25.032 | 1.883 |

## Failures

No failures recorded.

## Notes

- Native baselines are local developer-tool primitives, not equivalent MCP substitutes.
- Latency thresholds are intentionally broad; this smoke benchmark catches transport regressions and records trend evidence.
