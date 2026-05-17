# MCP Client Configuration

Use MCP protocol version `2025-06-18`.

## Codex

```toml
[mcp_servers.codex_tool_runtime]
command = "uvx"
args = ["codex-tool-runtime-mcp", "--stdio", "--workspace", "/path/to/repo"]
```

## Claude Code

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

## Cursor

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

## Continue, Cursor, Cline, And Generic HTTP Clients

Configure a Streamable HTTP MCP server at:

```text
http://127.0.0.1:8765/mcp
```

The server is designed for local loopback use. Do not bind it to a public interface without external authentication and sandboxing.
