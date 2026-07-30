# Remote MCP

`coding-tools-mcp` exposes Streamable HTTP at `/mcp`. Keep the listener on
loopback and publish it through an authenticated HTTPS tunnel. The fixed local
catalog contains `apply_patch` and `exec_command`; there is no reduced read-only
catalog, so a public endpoint must use static bearer auth, OAuth, or an external
authenticated proxy.

## One-command bearer tunnel

```bash
curl -fsSL https://raw.githubusercontent.com/xyTom/coding-tools-mcp/main/scripts/install.sh \
  | bash -s -- --tunnel cloudflared --auto-install-tunnel --workspace /path/to/repo
```

The script generates a bearer token, starts the server on `127.0.0.1`, and
prints the HTTPS MCP URL and `Authorization` header. From a checkout:

```bash
export CODING_TOOLS_MCP_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
CODING_TOOLS_MCP_AUTH_MODE=bearer scripts/tunnel.sh cloudflared /path/to/repo
```

The tunnel scripts also support `ngrok` and `devtunnel`.

## Persistent OAuth 2.1

Start OAuth mode with a stable Secret Vault master key:

```bash
export CODING_TOOLS_MCP_SECRETS_KEY='<independent-high-entropy-master-key>'
export CODING_TOOLS_MCP_SERVER_URL='https://mcp.example.com'
coding-tools-mcp --oauth-mode --workspace /path/to/repo
```

The server implements:

- Authorization Code + PKCE S256;
- RFC 7591 dynamic client registration;
- exact redirect URI matching;
- `authorization_code` and `refresh_token` grants;
- access-token `jti` tracking and exact revocation;
- refresh-token rotation and family revocation after reuse;
- active, retired, and revoked signing-key states;
- persistent Client/Grant/token/key metadata across restart.

Discovery and OAuth endpoints:

- `GET /.well-known/oauth-protected-resource`
- `GET /.well-known/oauth-authorization-server`
- `POST /oauth/register`
- `GET /oauth/authorize`
- `POST /oauth/authorize`
- `POST /oauth/token`

Authorization codes remain process-local, single-use, and expire after five
minutes. Dynamic Client records persist. Registration requests are narrowed to
the supported grant types (`authorization_code`, `refresh_token`) and response
type (`code`) instead of being widened silently.

### Persistent files

The stable configuration directory is selected by
`CODING_TOOLS_MCP_CONFIG_DIR`, or defaults to:

- Windows: `%APPDATA%\coding-tools-mcp`
- POSIX: `$XDG_CONFIG_HOME/coding-tools-mcp` or `~/.config/coding-tools-mcp`

Relevant files are:

- `server-settings.json`
- `mcp-servers.json`
- `oauth.sqlite3`
- `oauth-secrets.json`
- `server-secrets.json`
- `transcripts.sqlite3`

`oauth.sqlite3` stores identifiers, status, fingerprints, client-secret digests,
and peppered refresh-token hashes—not plaintext bearer or refresh tokens.
Signing material and the authorization-page password are resolved through the
Secret Vault. If the Store, Vault, master key, or referenced secret is missing or
corrupt, OAuth startup/request processing fails closed.

### Optional pre-registered client

```bash
export CODING_TOOLS_MCP_OAUTH_CLIENT_ID='<client-id>'
export CODING_TOOLS_MCP_OAUTH_REDIRECT_URIS='https://client.example/callback,http://127.0.0.1/callback'
export CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET='<optional-confidential-secret>'
export CODING_TOOLS_MCP_OAUTH_WORKSPACE_ID='<workspace-id>'
```

Public clients omit the secret and must use PKCE. Confidential clients must use
the authentication method recorded at registration. Client secrets are returned
only at creation and stored only as digests.

## Workspace mapping

OAuth bearer validation produces `client_id`, `grant_id`, `workspace_id`, and
`jti`. The selected Workspace is frozen when the HTTP Session is initialized.
The Session's cwd, processes, retained output, project instructions, and local
path resolution all use that Workspace.

- With exactly one enabled Workspace, old unbound Clients are migrated to that
  sole default.
- With multiple enabled Workspaces, a Client must have an explicit mapping in
  `oauth_client_workspace_bindings` or `CODING_TOOLS_MCP_OAUTH_WORKSPACE_ID`.
- A Grant copies and freezes the Client's Workspace at authorization time.
- Missing, disabled, or unauthorized mappings reject authorization or Session
  creation.
- Disabling a Workspace rejects new Sessions; existing Sessions retain their
  frozen binding until they are closed.

## HTTP Session behavior

An HTTP client initializes without `Mcp-Session-Id`. A successful response
returns a new unguessable Session ID. Later requests send:

```text
Mcp-Session-Id: <returned-id>
MCP-Protocol-Version: 2025-11-25
Authorization: Bearer <same-authority-context>
```

A second Agent cannot reuse, mutate, or delete another Agent's Session. Each
Session has an independent cwd, process table, output cache, runtime directory,
Workspace binding, and Gateway snapshot. `DELETE /mcp` terminates only the
selected Session.

## Gateway and remote tools

`mcp-servers.json` is read before Runtime initialization. Enabled servers and
allowlists are frozen into each Runtime. Tools are published as
`{alias}__{remote_name}`. Local names are reserved; a collision fails closed.
Saving Gateway configuration through Admin only sets `restart_required`; there
is no hot reload/start/stop path.

An upstream tool is a remote capability. Do not describe it as protected by the
local Workspace path boundary or local command permission mode. Its data access
and side effects are controlled by the upstream MCP server.

Gateway `secret_ref` values require the server Secret Vault and a valid
`CODING_TOOLS_MCP_SECRETS_KEY`; unresolved references fail closed.

## Admin authentication

`/admin` and `/admin/api` use a dedicated Admin token configured by
`--admin-token`, `CODING_TOOLS_MCP_ADMIN_TOKEN`, or `admin_token_secret_ref`.
Ordinary static MCP bearer tokens and OAuth access tokens are not Admin
credentials. The browser keeps the Admin token in page memory and never writes
it to the URL or persistent browser storage.

## Proxy and origin settings

For a stable hostname, set `CODING_TOOLS_MCP_SERVER_URL` so issuer, audience,
resource, and discovery URLs remain stable. Forwarded headers are ignored unless
`CODING_TOOLS_MCP_TRUST_PROXY_HEADERS=1` is set behind a proxy you control.
Browser origins must match the validated `CODING_TOOLS_MCP_ALLOWED_ORIGINS`
list exactly.

## Security notes

- Never publish `CODING_TOOLS_MCP_AUTH_MODE=noauth`.
- Use HTTPS and keep bearer tokens, Admin tokens, Vault master keys, OAuth
  passwords, and signing material out of repositories and logs.
- Use `dangerous` only inside an external container or VM.
- `--dangerously-fake-readonly-annotations` does not block mutation; avoid it on
  shared or public endpoints.
- Back up the complete stable configuration directory before migration or key
  operations. See [migration-v0.1-to-v0.2.2.md](migration-v0.1-to-v0.2.2.md).
