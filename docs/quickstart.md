# Quickstart

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

When working from this checkout instead of a published package, start Streamable HTTP with:

```bash
make start
```

Endpoint:

```text
http://127.0.0.1:8765/mcp
```

For browser chat clients such as MCP SuperAssistant, see [Browser chat clients](browser-clients.md).

The same HTTP process also serves the Chinese Web Admin Console by default:

```text
http://127.0.0.1:8765/admin
```

For an OAuth-protected personal console that can be opened from another machine:

```bash
uvx coding-tools-mcp --host 0.0.0.0 --port 8765 --workspace /path/to/repo --oauth-mode
```

This exposes `/mcp`, `/admin`, and `/oauth/authorize` on the same port. Disable the console with `--no-admin-ui` or `CODING_TOOLS_MCP_ADMIN_UI=0`.

MCP admin configuration defaults to `<workspace>/.coding-tools-mcp/mcp-servers.json`. Override it with `--upstream-config` or choose a directory with `--config-dir`; `server-settings.json` in the same config directory stores next-startup values such as host, port, workspace, OAuth issuer, permission mode, and shell environment policy. Runtime token/default-cwd/session/MCP reload changes apply immediately, while host/port/workspace/OAuth issuer changes require restart.

Admin tokens saved to settings are plaintext and should be treated as sensitive. Use `CODING_TOOLS_MCP_SECRETS_KEY` plus `secret_ref` for MCP server secrets. The admin console does not install skills.

Pass a different workspace, host, port, or extra server flags with Make variables:

```bash
make start MCP_WORKSPACE=/path/to/repo MCP_PORT=8000 MCP_ARGS="--permission-mode trusted"
```

If dependencies are missing, install the runtime in editable mode:

```bash
python -m pip install -e ".[dev]"
```

Start stdio:

```bash
coding-tools-mcp --stdio --workspace /path/to/repo
```

Run the acceptance gate:

```bash
make compliance
```

For local trace debugging:

```bash
CODING_TOOLS_MCP_TRACE=1 coding-tools-mcp --workspace /path/to/repo
```

Trace JSON lines are written to stderr.

For toolchains that require inherited shell variables, start the server with a broader shell environment policy:

```bash
CODING_TOOLS_MCP_SHELL_ENV_INHERIT=all coding-tools-mcp --workspace /path/to/repo
```

For local development with dependency downloads, shell expansion, and inline interpreter snippets, use trusted mode:

```bash
coding-tools-mcp --permission-mode trusted --workspace /path/to/repo
```

`--allow-network` remains a compatibility flag when you only want to open the network-looking command gate.

If the MCP client cannot show permission prompts and you intentionally want to disable `exec_command` permission gates inside an isolated container or VM:

```bash
coding-tools-mcp --permission-mode dangerous --workspace /path/to/repo
```

Use this only with trusted workspaces and trusted clients in an externally hardened environment. `--dangerously-skip-all-permissions` remains as a compatibility alias.
