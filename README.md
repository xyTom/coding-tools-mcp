# Codex Tool Runtime MCP

Codex Tool Runtime MCP is a model-neutral coding-agent runtime MCP server. It exposes local coding primitives to any MCP client:

```text
inspect repo -> search/read files -> apply structured patches -> run tests/commands
-> interact with stdin sessions -> inspect git status/diff
```

It is not a `codex(prompt)` wrapper. It does not expose Codex accounts, memory, cloud tasks, web search, image generation, model routing, plugin marketplace, or subagent orchestration as MCP tools.

## Documentation Map

- [Quickstart](docs/quickstart.md)
- [MCP client configuration](docs/mcp-client-config.md)
- [Tools and schemas](docs/tools-and-schemas.md)
- [Security policy](SECURITY.md)
- [CI and test commands](docs/ci-and-tests.md)
- [Dogfood](docs/dogfood.md)
- [SWE-bench evaluation](docs/swe-bench.md)
- [Known limitations](docs/limitations.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Competitive analysis](docs/competitive-analysis.md)
- Normative MCP runtime profile: [docs/profile-v0.1.md](docs/profile-v0.1.md)

## Quickstart

Run directly with `uvx` against the current directory:

```bash
uvx codex-tool-runtime-mcp --workspace .
```

Use stdio for MCP clients:

```bash
uvx codex-tool-runtime-mcp --stdio --workspace /path/to/repo
```

If you are working from this checkout instead of a published package:

```bash
cd /root/codex-tool-runtime-mcp
python -m pip install -e ".[dev]"
codex-tool-runtime-mcp --workspace /path/to/repo --host 127.0.0.1 --port 8765
```

HTTP endpoint:

```text
http://127.0.0.1:8765/mcp
```

Stdio:

```bash
codex-tool-runtime-mcp --stdio --workspace /path/to/repo
```

Set `CODEX_TOOL_RUNTIME_TRACE=1` to emit redacted JSON tool-call trace events to stderr for local debugging. Logs stay off stdout so stdio JSON-RPC remains clean.

## MCP Client Examples

Codex:

```toml
[mcp_servers.codex_tool_runtime]
command = "uvx"
args = ["codex-tool-runtime-mcp", "--stdio", "--workspace", "/path/to/repo"]
```

Claude Code:

```json
{
  "mcpServers": {
    "codex-tool-runtime": {
      "command": "uvx",
      "args": ["codex-tool-runtime-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

Cursor:

```json
{
  "mcpServers": {
    "codex-tool-runtime": {
      "command": "uvx",
      "args": ["codex-tool-runtime-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

Generic Streamable HTTP clients should use MCP protocol version `2025-06-18` and point at `http://127.0.0.1:8765/mcp`.

## Tools

P0 tools exposed by default:

- `read_file`
- `list_dir`
- `list_files`
- `search_text`
- `apply_patch`
- `exec_command`
- `write_stdin`
- `kill_session`
- `git_status`
- `git_diff`
- `request_permissions`

Additional image tool exposed by default:

- `view_image`

For input/output schemas and result envelopes, see [docs/tools-and-schemas.md](docs/tools-and-schemas.md) and [docs/profile-v0.1.md](docs/profile-v0.1.md).

## Safety Boundary

The runtime binds one workspace root per server process. Paths are workspace-relative by default. Absolute paths, `..` traversal, and symlink escapes are rejected. Recursive listing/search excludes `.git`, `.reference`, `node_modules`, `target`, `dist`, build outputs, virtualenvs, and common caches by default.

`exec_command` runs under policy controls with workspace-bound cwd, timeout, output caps, sensitive-value and loader/startup environment rejection, destructive command checks, network-looking command checks, shell-expansion permission gates, indirect absolute-path checks, Linux Landlock filesystem confinement, cancellation/kill cleanup, session deadline watchdogs, and bounded session buffers. This is still not a complete OS/container sandbox; see [SECURITY.md](SECURITY.md).

## Compliance

```bash
make compliance
```

The compliance report files are overwritten by the most recent reported suite. Inspect the `suite` field in [reports/compliance/latest.json](reports/compliance/latest.json) before citing the result as full compliance evidence. Non-`all` reports mark required tool coverage as `not_measured`.

GitHub Actions also runs compliance. Historical run `25957328972` passed for an earlier commit; final release evidence must cite the final pushed commit and its GitHub Actions run.

## Dogfood And Benchmark

Dogfood:

- [reports/dogfood/codex-on-mcp.md](reports/dogfood/codex-on-mcp.md)
- conclusion: `PASS`

SWE-bench:

- [reports/benchmark/swebench-regression.md](reports/benchmark/swebench-regression.md)
- [reports/benchmark/swebench-official-attempt.md](reports/benchmark/swebench-official-attempt.md)
- default smoke conclusion: `PREFLIGHT_ONLY`
- explicit official-harness attempt in this container: `BLOCKED`

The repository does not claim a model-generated SWE-bench leaderboard result. Docker or harness availability can block official evaluation, and checked-in predictions are placeholders until replaced with real baseline and MCP-candidate patches.

Manual official SWE-bench attempts should run through `.github/workflows/swebench-lite.yml`. The workflow defaults to `prediction_source=reference_patch`, generates non-empty reference-patch prediction files for the selected Lite instances, uploads `reports/benchmark/**`, and fails unless the official harness produces parsed resolved counts with `candidate_mcp_resolved >= baseline_native_resolved`. Use `prediction_source=checked_in` only after replacing the scaffold files with model-generated predictions.

## Development Commands

```bash
make lint
make typecheck
make test
make test-mcp-contract
make test-tool-golden
make test-security
make test-e2e
make test-codex-compat
make test-docs-required
make test-schema-drift
make dogfood-mcp
make dogfood-runner
make dogfood-smoke
make benchmark-smoke
make benchmark-real-workloads
make compliance
make ci
```
