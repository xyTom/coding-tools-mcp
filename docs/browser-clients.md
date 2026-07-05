# Browser Chat Clients

This guide covers browser-based AI chat clients that call MCP tools from a web page, with MCP SuperAssistant as the reference client.

## Recommended: MCP SuperAssistant Proxy

Use the MCP SuperAssistant browser extension with its local proxy, and let the proxy start `coding-tools-mcp` over stdio. This avoids browser CORS and custom-header limitations.

1. Copy the stdio example and replace `/path/to/repo` with the workspace you want to expose:

```bash
cp examples/mcp-superassistant/config.stdio.json config.json
```

2. Start the proxy with Streamable HTTP output:

```bash
npx -y @srbhptl39/mcp-superassistant-proxy@latest --config ./config.json --outputTransport streamableHttp
```

3. In the MCP SuperAssistant sidebar, use:

```text
Connection Type: Streamable HTTP
Server URI: http://localhost:3006/mcp
```

For the older SSE path, start the proxy with `--outputTransport sse` and configure the extension URL as `http://localhost:3006/sse`.

Keep the exposed workspace small and use a low-risk `coding-tools-mcp` permission mode first. The example uses `--tool-profile read-only`, which omits mutation tools such as `apply_patch`, `exec_command`, `write_stdin`, and `kill_session`.

## Nested MCP Gateway

For full integration, keep the browser connected to one `coding-tools-mcp` server and let that server aggregate other MCP servers through `--upstream-config`.

1. Copy the nested examples:

```bash
cp examples/mcp-superassistant/config.nested.json config.json
cp examples/mcp-superassistant/upstream.mcp-servers.example.json upstream.mcp-servers.json
```

2. Edit `config.json` so `--workspace` points to the coding workspace and `--upstream-config` points to your edited `upstream.mcp-servers.json`.

3. Start MCP SuperAssistant proxy:

```bash
npx -y @srbhptl39/mcp-superassistant-proxy@latest --config ./config.json --outputTransport streamableHttp
```

Nested upstream tools are exposed with namespace prefixes:

```text
filesystem__read_file
remote_docs__search
nested_gateway__inner__search
```

Aliases cannot contain `__`, so a multi-level name such as `nested_gateway__inner__search` is unambiguous: `nested_gateway` is the upstream registered in `coding-tools-mcp`, and `inner__search` is the remote tool name returned by that upstream.

Use `include_tools` on every upstream server exposed to a browser chat client. This keeps the browser tool list small and prevents accidental exposure of high-risk tools from nested MCP servers.

## Direct Streamable HTTP

MCP SuperAssistant can also connect directly to `coding-tools-mcp` with its `streamable-http` transport. Direct browser extension requests send a browser extension `Origin`, so the server only accepts them when the origin is explicitly allowlisted.

Start `coding-tools-mcp` for direct Chrome Web Store MCP SuperAssistant access:

```bash
coding-tools-mcp \
  --workspace /path/to/repo \
  --host 127.0.0.1 \
  --port 8765 \
  --tool-profile read-only \
  --allowed-origin chrome-extension://kngiafgkdnlkgmefdafaibkibegkcaef
```

Then configure MCP SuperAssistant:

```text
Connection Type: Streamable HTTP
Server URI: http://127.0.0.1:8765/mcp
```

You can also use the environment variable:

```bash
CODING_TOOLS_MCP_ALLOWED_ORIGINS=chrome-extension://kngiafgkdnlkgmefdafaibkibegkcaef \
coding-tools-mcp --workspace /path/to/repo --tool-profile read-only
```

For development builds, Chrome unpacked extensions have a different `chrome-extension://<id>` origin. Firefox uses `moz-extension://<uuid>` origins. Add the exact origin shown by the browser; wildcards are intentionally unsupported.

If `/mcp` requires bearer authentication, prefer the MCP SuperAssistant proxy path unless your browser client can configure custom `Authorization` headers. The direct extension path is intended for loopback development with explicit origin allowlisting.

## Security Notes

`--allowed-origin` is an exact CORS allowlist. It does not grant authentication by itself; it only lets the browser send requests to `/mcp`.

Prefer `--tool-profile read-only` for browser chat clients. Enable the full profile only for a trusted local browser extension, a low-risk workspace, and a permission mode you are prepared to expose to that client.

Use `--tool-profile compat-readonly-all` only for clients that incorrectly hide tools unless every tool is annotated as read-only. It changes annotations for compatibility; it does not make mutating tools safe.

Do not use a public tunnel with an anonymous full profile. For remote browser clients, put the server behind bearer/OAuth-capable infrastructure or keep using the local MCP SuperAssistant proxy on the same machine.
