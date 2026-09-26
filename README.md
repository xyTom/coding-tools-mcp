# Coding Tools MCP

**English** | [简体中文](README.zh-CN.md)

> Give any AI chat or agent a safe pair of hands on your codebase.

[![PyPI](https://img.shields.io/pypi/v/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![npm](https://img.shields.io/npm/v/coding-tools-mcp)](https://www.npmjs.com/package/coding-tools-mcp)
[![Python](https://img.shields.io/pypi/pyversions/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![compliance](https://github.com/xyTom/coding-tools-mcp/actions/workflows/compliance.yml/badge.svg)](https://github.com/xyTom/coding-tools-mcp/actions/workflows/compliance.yml)
[![release](https://github.com/xyTom/coding-tools-mcp/actions/workflows/release.yml/badge.svg)](https://github.com/xyTom/coding-tools-mcp/actions/workflows/release.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Coding Tools MCP is a **model-neutral coding runtime** served over the
[Model Context Protocol](https://modelcontextprotocol.io): file reading and
search, structured multi-file patches, command execution, interactive
sessions, and git — one server that any MCP client can drive. Claude Desktop,
Claude Code, Cursor, Cline, or an agent you build yourself all get the same
18 battle-tested tools, confined to one workspace, gated by permission modes.

## OAuth changes in this branch

The `oauth-refresh-persistence` branch is based on upstream `v0.3.0` and keeps
the original project behavior except for the OAuth improvements below:

- **Persistent RFC 7591 client registration.** Dynamic OAuth clients are saved
  to `~/.coding-tools-mcp/oauth_clients.json` by default, so restarting the MCP
  server no longer forces a previously registered client to obtain a new
  `client_id`.
- **OAuth refresh-token grant.** Authorization-code exchange now returns both
  an access token and a refresh token. Access tokens default to 24 hours;
  refresh tokens default to 30 days.
- **Fixed authorization window.** Refresh-token rotation preserves the original
  refresh-token expiry instead of extending the 30-day window indefinitely.
- **Token separation.** Access and refresh JWTs carry different `token_use`
  values so a refresh token cannot be accepted as an MCP access token.

Two additional environment variables are available:

```bash
CODING_TOOLS_MCP_OAUTH_REFRESH_TOKEN_TTL=2592000
CODING_TOOLS_MCP_OAUTH_CLIENT_REGISTRY=~/.coding-tools-mcp/oauth_clients.json
```

For refresh tokens to remain valid across server restarts,
`CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET` must also remain stable. The bundled
desktop client already persists this signing secret in its private profile
storage. See [docs/remote-mcp.md](docs/remote-mcp.md) for the full OAuth setup.

### Modified source and download

This modified branch is published from the contributor fork:

- Fork: `https://github.com/QingMu-Aoki/coding-tools-mcp`
- Branch: `oauth-refresh-persistence`
- Source: `https://github.com/QingMu-Aoki/coding-tools-mcp/tree/oauth-refresh-persistence`

Clone the modified branch directly:

```bash
git clone --branch oauth-refresh-persistence --single-branch https://github.com/QingMu-Aoki/coding-tools-mcp.git
cd coding-tools-mcp
```

These links become available after the branch has been pushed to the fork.

### Testing this branch

The PyPI/npm commands in the normal Quickstart below install the upstream
published release. Until these changes are merged upstream, test this branch
from a source checkout instead:

```bash
python -m pip install -e ".[desktop]"
coding-tools-mcp-desktop
```

For a server-only install, use `python -m pip install -e .` and start
`coding-tools-mcp` normally. Existing OAuth clients only need to authorize once
to receive a refresh token; subsequent access-token renewal can use the refresh
token until its absolute expiry.
[![Watch the demo](https://img.youtube.com/vi/N9lQaXt1eqQ/maxresdefault.jpg)](https://youtu.be/N9lQaXt1eqQ?si=LyEwvzzQF6QjUxR0)

## Why people use it

- **It turns a chat app into a coding agent.** Claude Desktop — or any MCP
  chat client — gets real repo access with the subscription you already have.
  No extra product required.
- **Safety is the product, not an afterthought.** One workspace root per
  server. Absolute paths, `..` traversal, and symlink escapes are rejected.
  Permission modes gate network access, shell expansion, inline scripts, and
  destructive commands. On Linux, [Landlock](docs/security-boundary.md) adds
  kernel-level filesystem confinement.
- **It is model- and vendor-neutral.** A fixed, truthfully annotated catalog —
  no profile switching, no annotation games. Swap models or clients freely;
  the runtime and its behavior stay put.
- **It is engineered for context windows.** Results are summarized, paginated,
  and capped by design; serialized tool-result bytes dropped 37%
  release-over-release on the deterministic dogfood workload with unchanged
  task completion.

## Quickstart

Run it with whichever toolchain you already have (the server is Python ≥ 3.11
from PyPI; the npm package is a thin launcher that starts it via `uv` or
`pipx`):

```bash
uvx coding-tools-mcp --stdio --workspace /path/to/repo   # Python toolchain
npx coding-tools-mcp --stdio --workspace /path/to/repo   # Node toolchain
```

Wire it into Claude Desktop, Claude Code, Cursor, or Cline — the JSON is the
same everywhere (swap `uvx` for `npx` if you prefer Node):

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

Then ask your client: *"run the test suite and fix the first failure."*

Prefer HTTP? Drop `--stdio` and the server speaks Streamable HTTP on
`http://127.0.0.1:8765/mcp`. Both protocol eras are served on either
transport: MCP `2026-07-28` in full, with `tools` as the only advertised
capability, and the handshake era `2025-11-25` with `2025-06-18`
compatibility. Neither has sessions. A one-line installer, per-client
walkthroughs, and troubleshooting live in
[docs/quickstart.md](docs/quickstart.md) and
[docs/mcp-client-config.md](docs/mcp-client-config.md).

## Seven things to try

**1. Make Claude Desktop your coding agent.** The config above is all it
takes — the chat window you already pay for can now read, patch, test, and
commit-review a real repository.

**2. Code on your own machine from anywhere.**

```bash
CODING_TOOLS_MCP_AUTH_MODE=bearer ./scripts/tunnel.sh cloudflared /path/to/repo
```

Loopback bind + authenticated HTTPS tunnel (`cloudflared`, `ngrok`, or
Microsoft Dev Tunnel). Point claude.ai on your phone at
`https://<tunnel-host>/mcp` and drive your home workstation from anywhere.
Bearer tokens and OAuth 2.1 + PKCE (with RFC 7591 dynamic registration) are
built in. → [docs/remote-mcp.md](docs/remote-mcp.md)

**3. Let an agent loose on untrusted code — inside a disposable sandbox.**

```bash
docker build -t coding-tools-mcp-sandbox:local .
docker run --rm --init -it -p 8765:8765 -v "$PWD:/workspace" coding-tools-mcp-sandbox:local
```

A containerized server with toolchains and caches preconfigured, safe to point
at a sketchy PR and destroy afterwards. → [docs/docker.md](docs/docker.md)

**4. Spin up a cloud sandbox with one MCP call.** The bundled
[Cloudflare Worker control plane](cloudflare/sandbox-control/README.md) exposes
`start_coding_tools_sandbox` as an MCP tool: one call dispatches a GitHub
Actions runner that boots the Docker sandbox and publishes it behind an
authenticated Cloudflare Tunnel. Ephemeral compute, no server of your own.

**5. Drive it from a GUI.**

```bash
python -m pip install "coding-tools-mcp[desktop]"
coding-tools-mcp-desktop
```

Per-workspace profiles, server and tunnel start/stop, credential setup with
clipboard helpers, live health checks. English and 简体中文.

**6. Keep an interactive command alive.** `exec_command` starts a REPL or
debugger under a real PTY; `write_stdin` feeds it across turns; `read_output`
pages long output; `kill_command` cleans up. Long-running processes are
first-class, with deadline watchdogs and bounded buffers.

**7. Give your own agent production-grade hands.** Building an agent loop with
the Anthropic SDK or anything else? Don't hand-roll file and exec tools —
speak MCP to this server and inherit the whole safety boundary. →
[docs/embedding.md](docs/embedding.md)

## The tool catalog

One stable, truthfully annotated set — permission modes change command
*policy*, never which tools the model sees. `apply_patch` is the sole
file-mutation primitive: staged, baseline-checked, atomic across files, with
rollback.

| Group | Tools |
| --- | --- |
| Files & search | `read_file` · `list_dir` · `list_files` · `search_text` · `apply_patch` · `view_image` |
| Execution | `exec_command` · `write_stdin` · `read_output` · `kill_command` · `request_permissions` |
| Git | `git_status` · `git_diff` · `git_log` · `git_show` · `git_blame` |
| Runtime | `server_info` · `check_exec_environment` |

Root `AGENTS.md`/`CLAUDE.md` files load automatically and come back in the
`instructions` of `initialize`, or of `server/discover` for a client that
never handshakes. Tool `content` is concise agent-facing text;
`structuredContent` carries the complete machine result. Schemas and result
envelopes: [docs/tools-and-schemas.md](docs/tools-and-schemas.md) ·
[docs/runtime-contract-v0.3.md](docs/runtime-contract-v0.3.md).

## Safety Boundary

| Mode | Meant for | What it allows |
| --- | --- | --- |
| `safe` (default) | day-to-day agent work | file tools and vetted commands; network-looking commands, shell expansion, inline scripts, and destructive commands all require explicit permission |
| `trusted` | local development | opens network, shell expansion, and inline scripts; keeps secret filtering and destructive-command checks |
| `dangerous` | isolated containers/VMs only | disables `exec_command` permission gates; workspace path boundaries still apply |

Recursive listing and search exclude `.git`, `node_modules`, build outputs,
virtualenvs, and caches. Commands run with workspace-bound cwd, scrubbed
environment, timeouts, and output caps. Linux hosts with Landlock get
kernel-enforced filesystem confinement; other platforms get an explicit
warning — this is still not a complete OS sandbox, so use the Docker image or
a VM for genuinely untrusted work. Details:
[SECURITY.md](SECURITY.md) · [docs/security-boundary.md](docs/security-boundary.md) ·
[docs/permission-modes.md](docs/permission-modes.md)

## Telemetry

The server sends anonymous usage telemetry (per-tool success/latency counters
and version/platform dimensions — never paths, arguments, commands, or file
contents) to help prioritize fixes. Disable it with
`CODING_TOOLS_MCP_TELEMETRY=off` or `DO_NOT_TRACK=1`; it is automatically off
in CI. `CODING_TOOLS_MCP_TELEMETRY=debug` prints every event to stderr instead
of sending. The full event list and guarantees are in
[docs/telemetry.md](docs/telemetry.md).

## Evidence, Dogfood and SWE-bench

Every release ships through a tag-triggered pipeline in which the compliance
suite, real-workload benchmark, and SWE-bench harness run from the same commit
that publishes to PyPI and npm — both via trusted publishing, npm with
provenance. Dogfood efficiency metrics are reproducible (`make dogfood-smoke`)
and checked in under `reports/`. This repository does not claim a
model-generated SWE-bench leaderboard result — see
[docs/swe-bench.md](docs/swe-bench.md) for exactly what is and is not
measured. More: [COMPLIANCE.md](COMPLIANCE.md) · [BENCHMARK.md](BENCHMARK.md) ·
[docs/dogfood.md](docs/dogfood.md)

## Documentation

| | |
| --- | --- |
| Getting started | [Quickstart](docs/quickstart.md) · [Client configuration](docs/mcp-client-config.md) · [Troubleshooting](docs/troubleshooting.md) |
| Remote & sandboxed | [Remote MCP](docs/remote-mcp.md) · [Docker sandbox](docs/docker.md) · [Cloud sandbox worker](cloudflare/sandbox-control/README.md) |
| Tools & contract | [Tools and schemas](docs/tools-and-schemas.md) · [Runtime contract](docs/runtime-contract-v0.3.md) · [Migrating to 0.3](docs/migration-0.3.md) · [Permission modes](docs/permission-modes.md) |
| Execution | [Exec recipes](docs/exec-command-recipes.md) · [Exec troubleshooting](docs/troubleshooting-exec.md) |
| Integration | [Embedding](docs/embedding.md) · [npm launcher](npm/coding-tools-mcp/README.md) |
| Security & quality | [Security policy](SECURITY.md) · [Security boundary](docs/security-boundary.md) · [CI and tests](docs/ci-and-tests.md) · [Limitations](docs/limitations.md) · [Competitive analysis](docs/competitive-analysis.md) |

## Development

```bash
python -m pip install -e ".[dev]"
make ci        # lint, typecheck, tests, protocol/integration suites, gates
```

The full gate matrix is in [docs/ci-and-tests.md](docs/ci-and-tests.md).

## License

This project is licensed under the [Apache License 2.0](LICENSE).

If you use code, documentation, substantial implementation details, or
derivative work from this project, preserve the copyright notice, license
notice, and [NOTICE](NOTICE) file, and clearly attribute the original project.

Project: Coding Tools MCP  
Author: Coding Tools MCP Contributors  
Source: https://github.com/xyTom/coding-tools-mcp

Citation metadata is available in [CITATION.cff](CITATION.cff).
