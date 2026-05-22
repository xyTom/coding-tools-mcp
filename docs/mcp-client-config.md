# MCP Client Configuration

Use MCP protocol version `2025-06-18`.

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

## Continue, Cursor, Cline, And Generic HTTP Clients

Configure a Streamable HTTP MCP server at:

```text
http://127.0.0.1:8765/mcp
```

The server is designed for local loopback use. Do not bind it to a public interface without external authentication and sandboxing.

## Remote MCP

For remote MCP clients, keep the server on loopback and expose it through an HTTPS tunnel. Anonymous tunnel testing should use `read-only` mode:

```bash
CODING_TOOLS_MCP_AUTH_MODE=noauth \
CODING_TOOLS_MCP_TOOL_PROFILE=read-only \
scripts/tunnel.sh cloudflared /path/to/repo
```

Configure the remote MCP client with:

```text
URL: https://<tunnel-host>/mcp
```

Static bearer-token auth is available for MCP clients that support custom `Authorization` headers. MCP clients that speak OAuth 2.1 Authorization Code + PKCE can use `--oauth-mode` instead, which publishes the standard discovery endpoints (`/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource`) and an HTML password gate on `/oauth/authorize`. Clients that cannot send custom bearer headers and do not speak OAuth should use anonymous `read-only` mode only for local/testing tunnels, or be placed behind an external auth proxy for production use. See [Remote MCP](remote-mcp.md) for details.
