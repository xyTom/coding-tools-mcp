# Remote MCP

`coding-tools-mcp` exposes Streamable HTTP at `/mcp`. Keep it bound to loopback
and publish it through an HTTPS tunnel. The fixed tool set includes
`apply_patch` and `exec_command`; there is no reduced read-only catalog, so every
public deployment must use bearer auth, OAuth, or an external authenticated
proxy.

## One-command bearer tunnel

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh \
  | bash -s -- --tunnel cloudflared --auto-install-tunnel --workspace /path/to/repo
```

The script generates a bearer token, starts the server on `127.0.0.1`, and
prints the HTTPS tunnel URL and header:

```text
URL: https://<tunnel-host>/mcp
Header: Authorization: Bearer <token>
```

From a checkout, the equivalent commands are:

```bash
export CODING_TOOLS_MCP_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
CODING_TOOLS_MCP_AUTH_MODE=bearer integrations/tunnels/tunnel.sh cloudflared /path/to/repo
```

The scripts also support `ngrok` and `devtunnel`.

## OAuth 2.1 + dynamic registration

For clients that cannot set a static `Authorization` header but support MCP
OAuth discovery:

```bash
CODING_TOOLS_MCP_AUTH_MODE=oauth \
integrations/tunnels/tunnel.sh cloudflared /path/to/repo
```

The server implements Authorization Code + PKCE S256 and RFC 7591 dynamic
client registration. A client discovers and registers itself; operators do not
need to invent a client ID or copy a client secret into the MCP host. The script
prints the password that the operator enters on the authorization page.

Discovery and OAuth endpoints:

- `GET /.well-known/oauth-protected-resource`
- `GET /.well-known/oauth-authorization-server`
- `POST /oauth/register`
- `GET /oauth/authorize`
- `POST /oauth/authorize`
- `POST /oauth/token`

Registration rules:

- `redirect_uris` are required, unique, and matched exactly.
- HTTPS redirects are accepted. HTTP is accepted only for `localhost`,
  `127.0.0.1`, or `::1` loopback callbacks.
- Supported token authentication methods are `none`, `client_secret_post`, and
  `client_secret_basic`. A client must use the method it registered.
- Client secrets are stored as digests. Public clients rely on mandatory PKCE.
- Registrations and authorization codes are process-local. A restart requires
  dynamic clients to register again.

Authorization codes are single-use and expire after five minutes. Access tokens
default to 24 hours and are bound to the registered client and exact MCP
resource URL.

## OAuth configuration

```bash
# Generated and printed when omitted:
CODING_TOOLS_MCP_OAUTH_PASSWORD=<authorize-page-password>

# Optional stable public origin, without /mcp:
CODING_TOOLS_MCP_SERVER_URL=https://mcp.example.com

# Optional stable HS256 key; hex-encoded bytes:
CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET=<hex-key>

# Optional token lifetime in seconds; default 86400:
CODING_TOOLS_MCP_OAUTH_TOKEN_TTL=86400
```

With an ephemeral tunnel, omit `CODING_TOOLS_MCP_SERVER_URL`; the server derives
the external origin from the request. For a stable hostname, pin it so issuer,
audience, resource, and discovery URLs remain constant.

The server ignores `Forwarded` and `X-Forwarded-*` by default. Set
`CODING_TOOLS_MCP_TRUST_PROXY_HEADERS=1` only behind a proxy you control. You can
also set exact browser origins with the comma-separated
`CODING_TOOLS_MCP_ALLOWED_ORIGINS` variable.

### Optional pre-registered client

Dynamic registration is the default. An operator may additionally pre-register
one known client:

```bash
CODING_TOOLS_MCP_OAUTH_CLIENT_ID=<client-id>
CODING_TOOLS_MCP_OAUTH_REDIRECT_URIS=https://client.example/callback,http://127.0.0.1/callback
CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET=<optional-confidential-secret>
```

If a client ID is configured, its redirect URI list is required operational
configuration; do not rely on the loopback fallback for a production client.

## HTTP session behavior

There are none. Since 0.3.0 this endpoint is stateless: no response carries an
`Mcp-Session-Id`, an `Mcp-Session-Id` a client kept from an older server is
ignored rather than refused, and `DELETE /mcp` returns `405` with `Allow: POST`
because there is nothing to terminate. Every request is answered by the one
runtime that owns the workspace, so a client may reconnect, change transport,
or run beside another client without losing anything. Commands are workspace
resources with their own timeout, count, output, and retention limits: any
authenticated client of the workspace can continue one with the `command_id`
that `exec_command` returned.

A handshake-era client needs no change for this. It still sends `initialize`,
still gets the same `InitializeResult`, and simply has no session header to
echo back.

A `2026-07-28` client sends no handshake at all. Each request states its
version in `params._meta` and mirrors that version and its method in headers
(`Mcp-Name` as well, for `tools/call`, `resources/read`, and `prompts/get`):

```bash
curl "$BASE_URL/mcp" \
  -H "Authorization: Bearer $CODING_TOOLS_MCP_AUTH_TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: server/discover" \
  --data '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'

curl "$BASE_URL/mcp" \
  -H "Authorization: Bearer $CODING_TOOLS_MCP_AUTH_TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/call" \
  -H "Mcp-Name: read_file" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"README.md"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

A header that contradicts the body, or a missing one, is `400` with `-32020`.
An unknown method in this era is `404` with `-32601`. Handshake-era errors stay
`200` with the JSON-RPC error, as they always did.

This implementation returns `405` for `GET /mcp` because it does not provide an
SSE stream. It rejects JSON-RPC batches and accepts `notifications/cancelled`
in both eras, answering with nothing; the notification does not terminate the
command the cancelled request started, which `kill_command` does.

## Local checks

Replace `BASE_URL` with the HTTPS origin, without `/mcp`:

```bash
curl "$BASE_URL/.well-known/mcp.json"
curl "$BASE_URL/.well-known/oauth-protected-resource"
curl "$BASE_URL/.well-known/oauth-authorization-server"
```

For bearer mode, an unauthenticated request must return `401` and a correct token
must reach MCP initialization:

```bash
curl "$BASE_URL/mcp" \
  -H "Authorization: Bearer $CODING_TOOLS_MCP_AUTH_TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'
```

## Security notes

- Never publish `CODING_TOOLS_MCP_AUTH_MODE=noauth`. It is suitable only for a
  loopback-only local process.
- Use HTTPS, rotate static bearer tokens, and keep OAuth passwords/signing keys
  out of committed files.
- Keep the MCP runtime in `safe` or `trusted`; use `dangerous` only inside an
  isolated container or VM with a trusted client.
- An HTTPS tunnel authenticates transport, not code execution. The server's
  policy and Landlock protections do not replace an external sandbox for
  untrusted repositories.
- Avoid `--dangerously-fake-readonly-annotations` on a published endpoint. It
  reports mutating tools as read-only, so a client on the far side of the tunnel
  cannot tell from `tools/list` that `apply_patch` and `exec_command` are exposed.
  The server requires authentication before allowing it over HTTP, but on a shared
  endpoint the operator who set it and the client who connects may not be the same
  party. Check `server_info.annotation_override` or the server card's
  `tools.annotationOverride` to see whether an endpoint is doing this.
