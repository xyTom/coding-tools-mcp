# Coding Tools MCP Agent Workbench and Remote Runner Guide

> For clickable in-browser navigation, start the HTTP server and open
> `http://127.0.0.1:8765/wiki`. This Markdown file is the repository fallback.

This guide is for users who want to operate an Agent in the browser, manage
multiple Workspaces, or run tools on another machine. If you only need to
connect Coding Tools MCP to an MCP client such as Claude, Codex, or Cursor,
start with the [Quickstart](quickstart.md).

## Choose the right endpoint

| Endpoint | Purpose | Credential |
| --- | --- | --- |
| `/mcp` | The stable 25-tool MCP interface | Ordinary bearer or OAuth token |
| `/app` | Browser Agent workbench for sessions, approvals, validation, and handoff | The same ordinary user identity as `/mcp` |
| `/admin` | Workspace, OAuth, Gateway, Secret, and Runner administration | A dedicated Admin token |

An Admin token cannot sign in to `/app`, and an ordinary bearer/OAuth token
cannot call the Admin API. Always use different tokens for these roles.

### `/app` product boundary

- `/app` is an optional Codex Agent workbench, not a GPT/ChatGPT webpage.
- The execution host must have Codex CLI installed and signed in; for a Runner
  Workspace, that prerequisite applies to its Runner host.
- App bearer, Admin token, and Codex sign-in credentials are three separate
  credential sets and are not interchangeable.
- Coding Tools MCP SQLite stores Agent Session metadata, while the Codex thread
  store keeps model-thread and conversation continuity; recovery depends on both.

## Start a local server

Python 3.11 or newer is required. Configure separate ordinary-user and Admin
tokens, then start the HTTP server. This example uses port `8765` explicitly:

```powershell
$env:CODING_TOOLS_MCP_AUTH_TOKEN = "<operator-token>"
$env:CODING_TOOLS_MCP_ADMIN_TOKEN = "<different-admin-token>"
coding-tools-mcp --workspace "G:\path\to\repo" --host 127.0.0.1 --port 8765
```

Use the equivalent environment-variable syntax on macOS or Linux. Do not put
real tokens in the repository, URLs, QR codes, screenshots, or chat messages.

After startup, open or configure:

- Agent workbench: `http://127.0.0.1:8765/app`
- Admin WebUI: `http://127.0.0.1:8765/admin`
- MCP endpoint: `http://127.0.0.1:8765/mcp`

Keep local-only deployments bound to loopback. For access from a phone or
another computer, publish the service through an authenticated HTTPS tunnel.
Never expose a `noauth` service publicly.

## Use the Agent workbench

1. Open `/app`, choose **Connect identity**, and enter an ordinary bearer or
   OAuth token. The browser keeps it only in the current page memory.
2. Select a Workspace available to that identity.
3. Create an Agent Session, enter the task instructions, and send the first turn.
4. When the Agent requests approval, review the exact command and impact before
   approving or declining it.
5. Run structured Validation when you need repository evidence. Validation uses
   fixed recipes and never installs dependencies automatically.
6. Before transferring the task, inspect Handoff for the branch, repository
   fingerprint, active task, Runner jobs, and latest validation evidence.

Agent Sessions are durable. Refreshing or closing the browser does not delete a
Session. Reopening it attempts to resume the backend and continues from the
event cursor. After a connection loss, do not blindly resubmit a previous turn
that may already have changed state.

## Configure Workspaces

Add local Workspaces through the **Workspaces** page in `/admin`. A Session is
bound to one Workspace when created and cannot switch roots later.

A Runner Workspace uses the following fields in server settings. Its `root` is
an opaque path or namespace understood by the Runner; the Control Plane never
resolves it on its own filesystem:

```json
{
  "id": "chem-project",
  "name": "Chem project on lab workstation",
  "root": "G:\\Research\\chem-project",
  "target": "runner",
  "runner_id": "lab-win-01",
  "enabled": true,
  "default": false
}
```

The `id` must match the Workspace ID advertised by the Runner, and `runner_id`
must match the Runner whose credential was issued. When that Runner is
unavailable, the operation returns a retryable error and never falls back to a
local Control Plane directory.

## Connect a remote Runner

Use a Runner when code exists only on another workstation, when tools must run
on that machine, or when the Control Plane must not mount the remote directory.

1. Call `POST /admin/api/runners/{runner_id}/credential` with Admin authority to
   issue or rotate a Runner credential. Plaintext is returned once.
2. Put it in `CODING_TOOLS_MCP_RUNNER_CREDENTIAL` on the Runner machine, or use
   a protected file with `--credential-file`.
3. Add a matching `id`, `runner_id`, and `root` to the Control Plane Workspace
   catalog.
4. Start the Runner:

```powershell
$env:CODING_TOOLS_MCP_RUNNER_CREDENTIAL = "<issued-once-credential>"
coding-tools-mcp-runner `
  --server "wss://control.example/runner/ws" `
  --runner-id "lab-win-01" `
  --workspace "chem-project=G:\Research\chem-project"
```

Production connections must use `wss://`. Only a local development or tunnel
loopback hop may explicitly use insecure WebSocket:

```powershell
coding-tools-mcp-runner --allow-insecure-ws --server "ws://127.0.0.1:8765/runner/ws" ...
```

Non-loopback `ws://` is rejected even with `--allow-insecure-ws`. Use
`--upstream-config <path>` when the Runner needs its own upstream MCP Gateway;
the configuration is loaded on the Runner and each Runtime keeps an immutable
published tool catalog.

## Common states

| State or symptom | Meaning | Normal action |
| --- | --- | --- |
| `http_session_capacity` | Global Session capacity is full | Close confirmed-stale Sessions and retry a new initialize after `Retry-After` |
| `http_session_identity_quota` | This identity reached its quota | Close stale Sessions for that identity; never share Session IDs across identities |
| `http_session_initialization_limit` | Too many initializations are in progress | Wait and retry only the new initialize |
| `UPSTREAM_BACKING_OFF` | An upstream returned 5xx, timed out, or disconnected | Wait for backoff or fix the upstream; do not replay an ambiguous mutating call |
| 404/410 stale session | The remote Session no longer exists | Clear the stale Session ID and initialize a new Session |
| Runner unavailable | The original Runner is offline | Reconnect the same Runner; there is no local fallback |
| `close_pending` | Close intent is waiting for the offline Runner | Let the original Runner reconnect and reconcile; do not repeat close requests |
| Validation unavailable | The required toolchain is unavailable | Install or configure it, then retry; Validation does not auto-install |

See [Runner and Session troubleshooting](webcodex-runner-troubleshooting.md)
for detailed diagnostics.

## Security and operations checklist

- Keep Operator, Admin, and Runner credentials separate.
- Keep tokens out of URLs, QR codes, command-line arguments, Git, and screenshots.
  Prefer environment variables, Secret Vault, or a protected credential file.
- Use HTTPS/WSS for public access and preserve authentication at the tunnel or
  reverse proxy.
- Do not treat `permission_mode=dangerous` as a sandbox. Use it only inside an
  externally isolated container or VM.
- Before upgrading, quiesce the service and back up Settings, Vault, OAuth,
  Agent Session, and Transcript databases.
- Use a SQLite-aware backup mechanism for a live database instead of copying a
  file while it is being written.

See [Coding Tools MCP Runtime Platform Migration and Rollback](webcodex-migration-rollback.md)
for upgrade and backup procedures, [Admin API](admin-api.md) for management
endpoints, and [Client configuration](mcp-client-config.md) for MCP clients.
