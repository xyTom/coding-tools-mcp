# Remote MCP

This guide exposes `coding-tools-mcp` to remote MCP clients through an HTTPS tunnel.

The server implements Streamable HTTP at `/mcp`, publishes remote discovery metadata at `/.well-known/mcp.json` and `/.well-known/mcp/server-card.json`, and supports three auth modes on `/mcp`:

- `none` — no authentication; only acceptable for local/testing tunnels with the `read-only` profile.
- `bearer` — static `Authorization: Bearer <token>` for clients that can send custom headers.
- `oauth2` — OAuth 2.1 Authorization Code + PKCE for MCP clients that perform the standard discovery + authorization-code flow. Discovery metadata is published at `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource`.

## Profile Choice

Use `--tool-profile read-only` first. It exposes inspection and git read tools plus `set_default_cwd` for navigation, and omits workspace mutation tools such as `apply_patch`, `exec_command`, `write_stdin`, and `kill_session`.

Use `--tool-profile full` only for trusted MCP clients that support write tools and truthful annotations. Avoid `full` and `compat-readonly-all` for anonymous tunnel testing.

## One-Command Tunnel

Install the published package from PyPI, start the local server, and expose a read-only bearer-token tunnel:

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh \
  | bash -s -- --tunnel cloudflared --auto-install-tunnel --workspace /path/to/repo
```

The script prints the local MCP URL, the tunnel provider's HTTPS URL, and the bearer header to configure in clients that support custom headers.

## Anonymous Read-Only Tunnel

```bash
CODING_TOOLS_MCP_AUTH_MODE=noauth \
CODING_TOOLS_MCP_TOOL_PROFILE=read-only \
scripts/tunnel.sh cloudflared /path/to/repo
```

Configure the remote MCP client with the HTTPS tunnel URL:

```text
https://<tunnel-host>/mcp
```

The discovery metadata reports auth type `none` in this mode. Anyone who can reach the tunnel URL can use the exposed read-only tools, so avoid sensitive workspaces and stop the tunnel when testing is done.

## MCP Clients With Bearer Auth

For clients that can send custom headers:

```bash
export CODING_TOOLS_MCP_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

CODING_TOOLS_MCP_AUTH_MODE=bearer \
CODING_TOOLS_MCP_TOOL_PROFILE=read-only \
scripts/tunnel.sh cloudflared /path/to/repo
```

Use:

```text
URL: https://<tunnel-host>/mcp
Header: Authorization: Bearer <token>
```

## MCP Clients With OAuth 2.1

For MCP clients that perform OAuth 2.1 Authorization Code + PKCE discovery on the server URL, run the tunnel script with `CODING_TOOLS_MCP_AUTH_MODE=oauth`. The only env var you must set yourself is `CODING_TOOLS_MCP_SERVER_URL` (the script cannot know your tunnel's public URL); `CLIENT_ID`, `CLIENT_SECRET`, and `PASSWORD` are generated and printed for you on startup:

```bash
export CODING_TOOLS_MCP_SERVER_URL="https://<stable-tunnel-host>"

CODING_TOOLS_MCP_AUTH_MODE=oauth \
CODING_TOOLS_MCP_TOOL_PROFILE=read-only \
scripts/tunnel.sh cloudflared /path/to/repo
```

The script adds `--oauth-mode` to the server and prints the OAuth metadata URLs and the generated credentials (copy them before they scroll out of view; they regenerate on every run unless you preset the env vars). The same flow works with `scripts/install.sh --tunnel <provider> --auth-mode oauth`. For local-only OAuth testing without a tunnel, `scripts/install.sh --start --auth-mode oauth` defaults `CODING_TOOLS_MCP_SERVER_URL` to `http://127.0.0.1:<port>`.

Required:

- `CODING_TOOLS_MCP_SERVER_URL` — public base URL (no trailing `/mcp`); used as the `issuer`/`aud` claim in issued tokens and in discovery metadata. **Must match the tunnel's actual external URL.**

Auto-generated if unset (override to keep stable values across restarts):

- `CODING_TOOLS_MCP_OAUTH_CLIENT_ID` — the only client_id the server accepts.
- `CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET` — paired with the client_id on `/oauth/token` (accepts `client_secret_post` or HTTP Basic).
- `CODING_TOOLS_MCP_OAUTH_PASSWORD` — the password an operator types on the `/oauth/authorize` HTML form to grant the authorization code.

Optional:

- `CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET` — hex-encoded HS256 signing key. Without it, a random key is generated per process and all tokens are invalidated on restart. Generate one with `python3 -c "import secrets; print(secrets.token_bytes(32).hex())"`.
- `CODING_TOOLS_MCP_OAUTH_TOKEN_TTL` — access-token lifetime in seconds (default `2592000`, i.e. 30 days).

Endpoints exposed when `--oauth-mode` is active:

- `GET /.well-known/oauth-authorization-server` — RFC 8414 authorization-server metadata.
- `GET /.well-known/oauth-protected-resource` — RFC 9728 protected-resource metadata.
- `GET /oauth/authorize` — renders an HTML password prompt; only `response_type=code` and `code_challenge_method=S256` are accepted. Authorization codes expire after 5 minutes.
- `POST /oauth/authorize` — accepts the password, issues a one-time code, and 302s back to `redirect_uri`.
- `POST /oauth/token` — exchanges `grant_type=authorization_code` + `code_verifier` for a Bearer JWT.

`/mcp` accepts the issued token as `Authorization: Bearer <token>`; unauthenticated requests get HTTP `401` with a `WWW-Authenticate` header pointing at the protected-resource metadata. `--auth-token` is ignored while OAuth is active.

### Stable Tunnel URLs

OAuth metadata and issued JWT claims pin the public URL at server start. Ephemeral tunnels (e.g. `cloudflared tunnel --url`, default ngrok, default devtunnel) generate a fresh random subdomain each run, so the URL the script advertises to clients will not match the tunnel's actual host after any restart. Use one of:

- **cloudflared named tunnel** — `cloudflared tunnel create <name>` + `cloudflared tunnel route dns <name> mcp.example.com`, then set `CODING_TOOLS_MCP_SERVER_URL=https://mcp.example.com`.
- **ngrok reserved domain** — claim a domain in the ngrok dashboard and either configure it in `~/.config/ngrok/ngrok.yml`, or run `ngrok` yourself (`ngrok http --domain=<reserved> 8765`) and set `CODING_TOOLS_MCP_SERVER_URL=https://<reserved>` before launching the server. The bundled `scripts/tunnel-ngrok.sh` runs `ngrok http http://127.0.0.1:$PORT` without extra flags, so to attach a reserved domain you either point ngrok at it via config or run ngrok separately and start the server with `coding-tools-mcp --oauth-mode` directly.
- **devtunnel persistent tunnel** — `devtunnel create <id>` + `devtunnel port create <id> -p 8765 --protocol http`, then `devtunnel host <id>`.

If you only need ephemeral testing, prefer `bearer` mode over `oauth`.

## Tunnel Scripts

Each script starts `coding-tools-mcp` on `127.0.0.1` and then starts the selected tunnel provider. If the provider CLI is missing, the script asks before installing it.

```bash
scripts/tunnel.sh cloudflared /path/to/repo
scripts/tunnel.sh ngrok /path/to/repo
scripts/tunnel.sh devtunnel /path/to/repo
```

Optional environment variables:

```bash
CODING_TOOLS_MCP_AUTO_INSTALL_TUNNEL=1
CODING_TOOLS_MCP_AUTH_MODE=bearer        # bearer | noauth | oauth
CODING_TOOLS_MCP_PORT=8765
CODING_TOOLS_MCP_TOOL_PROFILE=read-only
CODING_TOOLS_MCP_AUTH_TOKEN=<existing-token>
CODING_TOOLS_MCP_SERVER_BIN=coding-tools-mcp

# Required when CODING_TOOLS_MCP_AUTH_MODE=oauth:
CODING_TOOLS_MCP_SERVER_URL=https://<stable-tunnel-host>
# Auto-generated and printed at startup if unset:
CODING_TOOLS_MCP_OAUTH_CLIENT_ID=<client-id>
CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET=<client-secret>
CODING_TOOLS_MCP_OAUTH_PASSWORD=<authorize-page-password>
# Optional (oauth):
CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET=<hex-encoded-32-bytes>
CODING_TOOLS_MCP_OAUTH_TOKEN_TTL=2592000
```

If the selected tunnel CLI is missing, the scripts prompt before installing it. `cloudflared` installs into `~/.local/bin` when Homebrew is unavailable; `ngrok` uses Homebrew or npm; `devtunnel` uses the Microsoft installer script.

## Local Checks

Replace `BASE_URL` with the tunnel origin, without `/mcp`.

```bash
curl "$BASE_URL/.well-known/mcp.json"
```

For bearer mode only:

```bash
curl "$BASE_URL/mcp" \
  -H "Authorization: Bearer $CODING_TOOLS_MCP_AUTH_TOKEN"

curl "$BASE_URL/mcp" \
  -H "Authorization: Bearer $CODING_TOOLS_MCP_AUTH_TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "MCP-Protocol-Version: 2025-06-18" \
  --data '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
```

Missing or wrong bearer tokens on `/mcp` should return HTTP `401`.

For OAuth mode, the discovery endpoints should respond without auth:

```bash
curl "$BASE_URL/.well-known/oauth-authorization-server"
curl "$BASE_URL/.well-known/oauth-protected-resource"
```

A `401` response from `/mcp` includes:

```text
WWW-Authenticate: Bearer realm="coding-tools-mcp", resource_metadata="<BASE_URL>/.well-known/oauth-protected-resource"
```

## Security Notes

Keep the server bound to `127.0.0.1` and expose only the tunnel URL. Non-loopback binding is rejected unless a bearer token or `--oauth-mode` is configured. Use HTTPS tunnel URLs, rotate bearer tokens and OAuth client secrets if they are shared, set `CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET` so OAuth tokens survive restarts only when you actually want that, and do not use `full` or `compat-readonly-all` with untrusted clients.

Anonymous remote MCP tunnel testing exposes whatever the selected profile permits to anyone who can reach the tunnel URL. Use `read-only`, avoid sensitive workspaces, and stop the tunnel when testing is done.
