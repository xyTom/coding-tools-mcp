# MCP Client Configuration

Two protocol eras are served at once. A client that speaks `2026-07-28` needs
no configuration and no handshake: it discovers the server with
`server/discover` and states its version in every request. A handshake client
uses `2025-11-25`, and `2025-06-18` remains supported for existing clients.

## Claude Desktop

Edit `claude_desktop_config.json` — `~/Library/Application Support/Claude/` on
macOS, `%APPDATA%\Claude\` on Windows — and restart Claude Desktop:

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

## Codex

```toml
[mcp_servers.coding_tools]
command = "uvx"
args = ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
```

## Claude Code

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

Or from the command line:

```bash
claude mcp add coding-tools -- uvx coding-tools-mcp --stdio --workspace /path/to/repo
```

## Cursor

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

## VS Code

GitHub Copilot reads `.vscode/mcp.json` in the workspace:

```json
{
  "servers": {
    "coding-tools": {
      "type": "stdio",
      "command": "uvx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

For the local HTTP server instead, use `"type": "http"` with `"url"` and
`"headers"` keys.

## Windsurf

Edit `~/.codeium/windsurf/mcp_config.json` and restart Windsurf:

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

## Gemini CLI

One command, project scope by default:

```bash
gemini mcp add coding-tools -- uvx coding-tools-mcp --stdio --workspace /path/to/repo
```

Or edit `~/.gemini/settings.json` (user scope) or `.gemini/settings.json`
(project scope):

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

For the local HTTP server, transport `http` replaces the command entirely:

```bash
gemini mcp add --transport http coding-tools http://127.0.0.1:8765/mcp
```

Authenticated endpoints take `--header "Authorization: Bearer <token>"`. Keep
the server name free of underscores: Gemini CLI derives tool names from it and
splits on the first one.

## Continue, Cursor, Cline, And Generic HTTP Clients

Configure a Streamable HTTP MCP server at:

```text
http://127.0.0.1:8765/mcp
```

The server is designed for local loopback use. Do not bind it to a public interface without external authentication and sandboxing.

## Remote MCP

For remote MCP clients, keep the server on loopback and expose it through an
HTTPS tunnel with authentication. The fixed tool set includes mutation and
command execution:

```bash
CODING_TOOLS_MCP_AUTH_MODE=bearer \
integrations/tunnels/tunnel.sh cloudflared /path/to/repo
```

Configure the remote MCP client with:

```text
URL: https://<tunnel-host>/mcp
```

Static bearer-token auth is available for clients that support custom
`Authorization` headers. OAuth-aware MCP clients can use `--oauth-mode`, which
publishes protected-resource and authorization-server discovery plus RFC 7591
dynamic registration and a PKCE authorization flow. Clients that support
neither require an external authenticated proxy. See [Remote MCP](remote-mcp.md).

## ChatGPT

ChatGPT is a cloud client: it cannot launch a local process, so it needs the
authenticated HTTPS tunnel from [Remote MCP](#remote-mcp), and its custom
connectors offer OAuth or no authentication — there is no static bearer
header to enter.

```bash
CODING_TOOLS_MCP_AUTH_MODE=oauth integrations/tunnels/tunnel.sh cloudflared /path/to/repo
```

In ChatGPT, enable developer mode (Settings → Connectors → Advanced
settings), then create a custom connector pointing at
`https://<tunnel-host>/mcp`. If ChatGPT discovers the OAuth endpoints itself,
the authorization page asks for the password the script printed. If the
connector form asks for details instead, fill them in:

- Authorization URL: `https://<tunnel-host>/oauth/authorize`
- Token URL: `https://<tunnel-host>/oauth/token`
- Client ID and secret: from a pre-registered client — start the tunnel with
  `CODING_TOOLS_MCP_OAUTH_CLIENT_ID`,
  `CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET`, and
  `CODING_TOOLS_MCP_OAUTH_REDIRECT_URIS` set to the redirect URI ChatGPT
  shows.

## Grok

grok.com is also cloud-only. Start the same OAuth tunnel, then add a custom
connector at grok.com/connectors (Settings → Connectors) with
`https://<tunnel-host>/mcp`; Grok completes the OAuth flow in a popup.

The xAI API takes the static bearer tunnel instead — a request's remote MCP
tool entry pins the server and the header:

```json
{
  "server_url": "https://<tunnel-host>/mcp",
  "server_label": "coding_tools",
  "authorization": "<CODING_TOOLS_MCP_AUTH_TOKEN>"
}
```

Only Streamable HTTP and SSE transports are supported on Grok's side, which
is what the tunnel already speaks.
