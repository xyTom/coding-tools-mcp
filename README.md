# Coding Tools MCP

Coding Tools MCP is a model-neutral coding-agent runtime MCP server. It exposes local coding primitives to any MCP client:

```text
inspect repo -> search/read files -> apply structured patches -> run tests/commands
-> interact with stdin sessions -> inspect git status/diff
```

It is not a prompt wrapper. It does not expose external agent accounts, memory, cloud tasks, web search, image generation, model routing, plugin marketplace, or subagent orchestration as MCP tools.

## Documentation Map

- [Quickstart](docs/quickstart.md)
- [MCP client configuration](docs/mcp-client-config.md)
- [Remote MCP](docs/remote-mcp.md)
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

Install the published command from PyPI:

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh | bash
```

Install and start local Streamable HTTP against a workspace:

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh \
  | bash -s -- --start --workspace /path/to/repo
```

Install and expose a read-only bearer-token tunnel:

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh \
  | bash -s -- --tunnel cloudflared --auto-install-tunnel --workspace /path/to/repo
```

Or, from this checkout:

```bash
scripts/install.sh
```

Run the published package without a persistent install:

```bash
uvx coding-tools-mcp --workspace .
```

Use stdio for MCP clients:

```bash
uvx coding-tools-mcp --stdio --workspace /path/to/repo
```

If you are working from this checkout instead of a published package:

```bash
cd /root/coding-tools-mcp
python -m pip install -e ".[dev]"
coding-tools-mcp --workspace /path/to/repo --host 127.0.0.1 --port 8765
```

Install the optional image extra when you want `view_image` auto-resize support:

```bash
python -m pip install -e ".[image]"
```

HTTP endpoint:

```text
http://127.0.0.1:8765/mcp
```

Stdio:

```bash
coding-tools-mcp --stdio --workspace /path/to/repo
```

Set `CODING_TOOLS_MCP_TRACE=1` to emit redacted JSON tool-call trace events to stderr for local debugging. Logs stay off stdout so stdio JSON-RPC remains clean.

If your MCP client does not support permission elicitation and you explicitly want permission-gated operations to run, start with:

```bash
coding-tools-mcp --dangerously-skip-all-permissions --workspace /path/to/repo
```

This auto-grants permission-gated operations such as network-looking commands, destructive commands, shell expansion, and sensitive env passed through `exec_command`. Workspace path boundaries still apply.

## MCP Client Examples

Generic stdio client:

```toml
[mcp_servers.coding_tools]
command = "uvx"
args = ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
```

Claude Code:

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "uvx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

Cursor:

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "uvx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

Generic Streamable HTTP clients should use MCP protocol version `2025-06-18` and point at `http://127.0.0.1:8765/mcp`.

## Remote MCP

For remote MCP clients and local development over an HTTPS tunnel, keep the server bound to loopback and expose the tunnel URL with the safest profile your client can use. Anonymous tunnel testing should use `read-only` mode:

```bash
CODING_TOOLS_MCP_AUTH_MODE=noauth \
CODING_TOOLS_MCP_TOOL_PROFILE=read-only \
./scripts/tunnel.sh cloudflared /path/to/repo
```

Configure the remote MCP client with the HTTPS tunnel URL:

```text
URL: https://<tunnel-host>/mcp
```

The tunnel scripts support `cloudflared`, `ngrok`, and Microsoft Dev Tunnel. If the selected tunnel CLI is missing, the script asks before installing it:

```bash
scripts/tunnel.sh cloudflared /path/to/repo
scripts/tunnel.sh ngrok /path/to/repo
scripts/tunnel.sh devtunnel /path/to/repo
```

For clients that support custom headers, use bearer-token auth with `Authorization: Bearer <token>`. For MCP clients that speak OAuth 2.1 Authorization Code + PKCE, use `CODING_TOOLS_MCP_AUTH_MODE=oauth` with `scripts/tunnel.sh` (or `scripts/install.sh --auth-mode oauth`). The server can infer its OAuth issuer from the tunnel request URL, so one-shot tunnels like cloudflared work without setting `CODING_TOOLS_MCP_SERVER_URL` before startup; set it only when you want to pin a stable issuer. The script prints a generated OAuth password, accepts any non-empty client_id by default, and lets you opt into `CODING_TOOLS_MCP_OAUTH_CLIENT_ID`/`CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET` only when you need to lock down a confidential client. Clients that cannot send custom bearer headers and do not speak OAuth should use anonymous `read-only` mode only for local/testing tunnels, or be placed behind an external auth proxy for production use.

See [docs/remote-mcp.md](docs/remote-mcp.md) for the exact modes and security notes.

## Tool Profiles

- `full`: exposes all tools with truthful annotations. This is the default for backward compatibility.
- `read-only`: recommended for remote or safe-mode clients; exposes only inspection tools, git read tools, image viewing, and default-cwd helpers.
- `compat-readonly-all`: exposes all tools but advertises every tool as read-only for clients that gate availability on `readOnlyHint`. This is not a safety mode; mutation-capable tools such as `apply_patch`, `exec_command`, `write_stdin`, and `kill_session` can still mutate local state.

## Tools

P0 tools exposed by default:

- `server_info`
- `get_default_cwd`
- `set_default_cwd`
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
- `git_log`
- `git_show`
- `git_blame`
- `request_permissions`

Additional image tool exposed by default:

- `view_image`

For input/output schemas and result envelopes, see [docs/tools-and-schemas.md](docs/tools-and-schemas.md) and [docs/profile-v0.1.md](docs/profile-v0.1.md).

## Safety Boundary

The runtime binds one workspace root per server process. Paths are workspace-relative by default. Absolute paths, `..` traversal, and symlink escapes are rejected. Recursive listing/search excludes `.git`, `.reference`, `node_modules`, `target`, `dist`, build outputs, virtualenvs, and common caches by default.

`exec_command` runs under policy controls with workspace-bound cwd, timeout, output caps, sensitive-value and loader/startup environment rejection, destructive command checks, network-looking command checks, shell-expansion permission gates, indirect absolute-path checks, cancellation/kill cleanup, session deadline watchdogs, and bounded session buffers. On Linux hosts with Landlock support it also applies filesystem confinement; on Windows, macOS, or Linux hosts without Landlock, command results include a warning and external sandboxing is required before running untrusted commands. This is still not a complete OS/container sandbox; see [SECURITY.md](SECURITY.md).

`--dangerously-skip-all-permissions` disables the permission gates above for operators who accept that risk. Do not use it for untrusted workspaces or untrusted MCP clients.

## Compliance

```bash
make compliance
```

Compliance and CI commands are documented in [docs/ci-and-tests.md](docs/ci-and-tests.md). The checked-in report files are generated artifacts; inspect their `suite` field before treating them as full compliance evidence.

## Dogfood And Benchmark

Dogfood and SWE-bench notes live in [docs/dogfood.md](docs/dogfood.md), [docs/swe-bench.md](docs/swe-bench.md), and [BENCHMARK.md](BENCHMARK.md). This repository does not claim a model-generated SWE-bench leaderboard result.

## Development Commands

```bash
make lint
make typecheck
make test
make compliance
make ci
```

See [docs/ci-and-tests.md](docs/ci-and-tests.md) for the full test matrix.

## License

This project is source-available, not open source. See [LICENSE](LICENSE).
Internal evaluation, development, testing, and security review are permitted;
redistribution, hosted third-party service use, and production commercial use
require prior written permission.
