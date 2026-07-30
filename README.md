# Coding Tools MCP

**English** | [简体中文](README.zh-CN.md)

> Give any MCP-capable AI client a bounded, auditable pair of hands on your codebase.

[![PyPI](https://img.shields.io/pypi/v/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![npm](https://img.shields.io/npm/v/coding-tools-mcp)](https://www.npmjs.com/package/coding-tools-mcp)
[![Python](https://img.shields.io/pypi/pyversions/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![compliance](https://github.com/xyTom/coding-tools-mcp/actions/workflows/compliance.yml/badge.svg)](https://github.com/xyTom/coding-tools-mcp/actions/workflows/compliance.yml)
[![release](https://github.com/xyTom/coding-tools-mcp/actions/workflows/release.yml/badge.svg)](https://github.com/xyTom/coding-tools-mcp/actions/workflows/release.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Coding Tools MCP is a model-neutral coding runtime served over the Model Context
Protocol. It provides bounded file reads and search, atomic multi-file patches,
command execution, interactive process sessions, Git inspection, optional
upstream MCP composition, persistent OAuth, Workspace-bound HTTP sessions, and
an authenticated Admin WebUI.

The default local catalog contains 20 stable, truthfully annotated tools. Permission
modes change command policy, never `tools/list`. Optional upstream tools are
snapshotted at Runtime initialization and exposed under stable namespaces; their
remote capabilities remain governed by the upstream server, not by this
server's local Workspace boundary.

## Why people use it

- **One runtime contract across clients.** Claude Desktop, Claude Code, Codex,
  Cursor, Cline, and custom agents use the same MCP schemas and result envelopes.
- **A real safety boundary.** Each MCP Session is immutably bound to one validated
  Workspace. Local path tools reject absolute paths outside that root, `..`
  traversal, and symlink escapes. Linux Landlock adds kernel-level confinement
  when available.
- **Stable tools instead of profiles.** The removed v0.1 `tool_profile` setting is
  migration input only. It cannot hide tools, alter annotations, or change the
  fixed local catalog.
- **Persistent remote identity.** OAuth Clients, Grants, access-token metadata,
  refresh-token families, and signing-key state survive restart and can be
  revoked precisely by ID.
- **Operational management without secret disclosure.** `/admin` exposes
  revision-aware Settings, Workspace, Gateway, OAuth, Secret Vault, and
  conversation/session management through a dedicated Admin credential.
- **Bounded outputs for context windows.** Tool results are summarized and
  paginated while `structuredContent` retains the complete machine interface.

## Quickstart

The Python package requires Python 3.11 or newer. The npm package is a thin
launcher that starts the pinned Python server through `uvx` or `pipx`.

```bash
uvx coding-tools-mcp --stdio --workspace /path/to/repo
npx coding-tools-mcp --stdio --workspace /path/to/repo
```

Example MCP client configuration:

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

For Streamable HTTP, omit `--stdio`. The default endpoint is
`http://127.0.0.1:8765/mcp`. The primary protocol is MCP `2025-11-25`, with
explicit `2025-06-18` compatibility.

## Integrated v0.2.2 architecture

### Sessions and Workspaces

Every successful HTTP `initialize` creates an independent Runtime with its own
Workspace binding, cwd, process table, retained output, project instructions,
and upstream-tool snapshot. The binding cannot change during the Session.
Subsequent POST and DELETE requests must match the authorization context used at
initialization. stdio uses the explicitly configured default Workspace and does
not invent an OAuth identity.

### Persistent OAuth

OAuth supports Authorization Code + PKCE S256, RFC 7591 dynamic registration,
`authorization_code` and `refresh_token` grants, refresh rotation and family
reuse detection, access-token `jti` revocation, and active/retired/revoked
signing keys. Client secrets are stored only as digests, refresh tokens only as
peppered hashes, and signing material only in the encrypted Secret Vault.

OAuth startup is fail-closed: the persistent Store and
`CODING_TOOLS_MCP_SECRETS_KEY`-backed Vault must be available. See
[Remote MCP](docs/remote-mcp.md) and the
[upgrade guide](docs/migration-v0.1-to-v0.2.2.md).

### Gateway

Upstream MCP configuration is fixed before Runtime initialization. Each Runtime
gets an immutable namespaced snapshot, so `listChanged: false` remains truthful.
Local tool names are reserved; namespace collisions fail closed. Admin Gateway
changes are persisted for restart only—there is no hot reload, start, or stop
control path.

### Admin WebUI

Configure a dedicated Admin token and open `/admin`. Ordinary MCP bearer and
OAuth access tokens are never promoted to Admin authority. Settings writes use
revision checks; a stale HTTP 409 preserves the browser draft and requires an
explicit conflict resolution. Secret values, token material, hashes, digests,
and Vault references are never displayed.

## The local tool catalog

| Group | Tools |
| --- | --- |
| Files and search | `read_file`, `list_dir`, `list_files`, `search_text`, `apply_patch`, `view_image` |
| Execution | `exec_command`, `write_stdin`, `read_output`, `kill_session`, `request_permissions` |
| Git | `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame` |
| Runtime | `server_info`, `check_exec_environment`, `get_default_cwd`, `set_default_cwd` |

`apply_patch` is the only direct local file-mutation primitive. Root
`AGENTS.md`/`CLAUDE.md` instructions are loaded during initialization. Tool
`content` is concise agent-facing text; `structuredContent` is the stable
machine-readable result.

## Safety Boundary

| Mode | Intended use | Actual behavior |
| --- | --- | --- |
| `safe` | daily agent work | network-looking commands, shell expansion, inline scripts, and destructive commands require permission |
| `trusted` | normal local development | enables network, expansion, and inline snippets while retaining secret and destructive-command checks |
| `dangerous` | isolated container or VM | disables command permission gates; direct local path tools remain Workspace-bound |

Permission modes never hide mutation tools. The advanced
`--dangerously-fake-readonly-annotations` compatibility switch only rewrites
exposure hints in `tools/list`; it does not prevent execution or mutation and is
not a security boundary.

The server is not a complete OS sandbox on every platform. Use the Docker image,
a VM, or another external sandbox for untrusted repositories. An upstream MCP
tool is a remote capability and is controlled by that upstream server's own
security model.

## Remote access and management

Keep HTTP bound to loopback and publish it through an authenticated HTTPS tunnel.
The fixed local catalog includes mutation and execution, so never expose
`noauth` publicly. See [Remote MCP](docs/remote-mcp.md).

The Admin WebUI and API are documented separately:

- [Admin API](docs/admin-api.md)
- [Admin WebUI](docs/admin-webui.md)
- [Chat and Codex session persistence](docs/chat-persistence.md)

## Desktop client versus Admin WebUI

The optional Desktop client is a local launcher and tunnel/profile manager:

```bash
python -m pip install "coding-tools-mcp[desktop]"
coding-tools-mcp-desktop
```

Desktop profile and secret storage remain separate from server Settings and the
server Secret Vault. The Admin WebUI manages a running HTTP server through its
dedicated Admin API; it is not the Desktop application and does not start or stop
the Desktop runtime.

## Telemetry

Anonymous usage telemetry is enabled by default upstream and contains closed
schemas of counters, enums, durations, and version/platform dimensions—never
paths, Workspace/Agent/Client IDs, commands, arguments, file contents, chat
content, or transcript summaries. Disable it with
`CODING_TOOLS_MCP_TELEMETRY=off` or `DO_NOT_TRACK=1`; CI disables it
automatically. `CODING_TOOLS_MCP_TELEMETRY=debug` prints events to stderr instead
of sending them. See [docs/telemetry.md](docs/telemetry.md).

## Evidence, Dogfood and SWE-bench

The repository includes reproducible compliance, dogfood, latency, and
SWE-bench harnesses. It does not claim a model-generated SWE-bench leaderboard
result. See [COMPLIANCE.md](COMPLIANCE.md), [BENCHMARK.md](BENCHMARK.md), and
[docs/swe-bench.md](docs/swe-bench.md).

## Documentation

| Topic | Documents |
| --- | --- |
| Getting started | [Quickstart](docs/quickstart.md), [Client configuration](docs/mcp-client-config.md), [Troubleshooting](docs/troubleshooting.md) |
| Runtime contract | [Tools and schemas](docs/tools-and-schemas.md), [Runtime contract](docs/runtime-contract-v0.2.md), [Permission modes](docs/permission-modes.md) |
| Remote and OAuth | [Remote MCP](docs/remote-mcp.md), [Upgrade and rollback](docs/migration-v0.1-to-v0.2.2.md) |
| Administration | [Admin API](docs/admin-api.md), [Admin WebUI](docs/admin-webui.md), [Chat persistence](docs/chat-persistence.md) |
| Security and sandboxing | [Security policy](SECURITY.md), [Security boundary](docs/security-boundary.md), [Docker](docs/docker.md) |
| Packaging and clients | [Desktop README](apps/desktop-client/README.md), [npm launcher](npm/coding-tools-mcp/README.md) |
| Quality | [CI and tests](docs/ci-and-tests.md), [Dogfood](docs/dogfood.md), [SWE-bench](docs/swe-bench.md), [Limitations](docs/limitations.md) |

## Development

```bash
python -m pip install -e ".[dev]"
make ci
```

The full gate matrix is in [docs/ci-and-tests.md](docs/ci-and-tests.md).

## License

Licensed under the [Apache License 2.0](LICENSE). Preserve the copyright,
license, [NOTICE](NOTICE), and attribution requirements when redistributing
substantial code or documentation.
