# Benchmarks

This tree owns deterministic dogfood and benchmark/regression harnesses.

- `dogfood/mcp_deterministic_runner.py` talks to the local MCP server over HTTP
  and performs deterministic coding tasks using MCP tools only.
- `runtime_latency.py` records local MCP HTTP latency for `tools/list`,
  `read_file`, `search_text`, and `exec_command`, alongside native local
  developer-tool baselines.
- `swebench/run_smoke.py` validates SWE-bench smoke scaffolding and launches the
  official SWE-bench harness when resources and real predictions are available.
- `swebench/generate_reference_predictions.py` creates non-empty reference-patch
  JSONL files for GitHub Actions official-harness sanity checks.

The dogfood runner may start the server and prepare fixtures. It does not use
direct filesystem, shell, or git operations for the coding loop after the MCP
server is reachable.
