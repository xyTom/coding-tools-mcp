# MCP SuperAssistant Examples

`config.stdio.json` is the recommended simple MCP SuperAssistant proxy config for `coding-tools-mcp`.

Replace `/path/to/repo` with the workspace to expose, then run:

```bash
npx -y @srbhptl39/mcp-superassistant-proxy@latest --config ./config.stdio.json --outputTransport streamableHttp
```

Configure the browser extension:

```text
Connection Type: Streamable HTTP
Server URI: http://localhost:3006/mcp
```

For SSE, use `--outputTransport sse` and set the extension URL to `http://localhost:3006/sse`.

The example uses `--tool-profile read-only` by default. Switch to `full` only when the browser client is trusted to mutate the workspace.

## Nested MCP

Use `config.nested.json` when the browser should see tools from `coding-tools-mcp` plus upstream MCP servers managed by `coding-tools-mcp`.

1. Copy `config.nested.json` and `upstream.mcp-servers.example.json`.
2. Replace `/path/to/repo`, `/path/to/upstream.mcp-servers.json`, and the upstream server targets.
3. Keep `include_tools` narrow for every upstream exposed to a browser chat client.

Tool names are namespaced by upstream alias. If the upstream is itself another MCP gateway, nested names are preserved, for example:

```text
nested_gateway__inner__search
```

