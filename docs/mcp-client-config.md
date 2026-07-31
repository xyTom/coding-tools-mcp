# MCP Client Configuration

Use MCP protocol version `2025-11-25`. Version `2025-06-18` remains supported
for existing clients. Tool profiles are not supported; permission modes never
change the fixed local catalog.

## stdio clients

### Codex

```toml
[mcp_servers.coding_tools]
command = "uvx"
args = ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
```

### Claude Code, Cursor, Cline, and compatible JSON clients

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

stdio uses the explicit/default Workspace for the process and does not invent an
OAuth Agent identity.

## Local Streamable HTTP

Configure the MCP endpoint:

```text
http://127.0.0.1:8765/mcp
```

Every client first calls `initialize` without `Mcp-Session-Id`. Later calls send
the returned Session ID and negotiated protocol version. Each Session owns an
independent Workspace binding, cwd, process table, retained output, project
context, and optional Gateway snapshot.

Loopback HTTP may use static bearer, OAuth, or explicit local no-auth. Never bind
no-auth to a public interface.

## Remote static bearer

Keep the server on loopback and expose it through an HTTPS tunnel:

```bash
CODING_TOOLS_MCP_AUTH_MODE=bearer \
  scripts/tunnel.sh cloudflared /path/to/repo
```

Configure the remote client with:

```text
URL: https://<tunnel-host>/mcp
Authorization: Bearer <mcp-bearer-token>
```

The bearer identifies MCP access only. It is not an Admin credential.

## Remote OAuth

OAuth-aware MCP clients can discover protected-resource and authorization-server
metadata, register through RFC 7591, and complete Authorization Code + PKCE.
The server advertises `authorization_code` and `refresh_token` grants and rotates
refresh tokens after use. Dynamic Client records and Grants persist across
restart; authorization codes remain short-lived and process-local.

With one enabled Workspace, new/legacy Clients bind to the sole default. With
multiple enabled Workspaces, configure an explicit Client mapping before
creating a Grant. See [Remote MCP](remote-mcp.md).

## Dedicated Admin access

The Admin WebUI at `/admin` and API at `/admin/api` require a separate Admin
token. Do not place that token in an MCP client configuration, URL, or persistent
browser storage. Ordinary MCP bearer and OAuth credentials receive `401` from
Admin endpoints.

## Upstream Gateway tools

Optional upstream tools appear with stable names such as
`github__search_issues`. The list is frozen when the Runtime initializes. Client
code must not expect `tools/list` change notifications or attempt to hot-reload
the Gateway. Remote tool permissions are controlled by the upstream server.
