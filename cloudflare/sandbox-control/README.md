# Cloudflare Sandbox Control Worker

This Worker is the small authenticated control plane for remote coding sandboxes:

1. A caller sends an authenticated request to the Worker.
2. The Worker dispatches `.github/workflows/start-sandbox.yml` through the GitHub API.
3. GitHub Actions starts the Docker sandbox and exposes `coding-tools-mcp` through Cloudflare Tunnel.
4. The MCP client connects to the tunnel URL with the MCP bearer token stored in GitHub Secrets.

The Worker does not run code and does not proxy MCP traffic. It only starts the sandbox workflow.

## Recommended Setup

Use a Cloudflare named tunnel for the MCP endpoint. Quick tunnels are fine for ad hoc tests, but named tunnels give you one reusable hostname such as `mcp.example.com`.

1. Create a Cloudflare Tunnel and publish a hostname that routes to `http://localhost:8765`.
2. Copy the tunnel token and save it as a GitHub repository secret named `CLOUDFLARE_TUNNEL_TOKEN`.
3. Generate a stable MCP bearer token and save it as a GitHub repository secret named `CODING_TOOLS_MCP_AUTH_TOKEN`.
4. Edit `wrangler.toml` and set `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_REF`, and `TUNNEL_HOSTNAME`.
5. Create a GitHub token for the Worker. A fine-grained token should be limited to the target repository with Actions write permission.
6. Configure Worker secrets:

```bash
npx wrangler secret put CONTROL_TOKEN --config cloudflare/sandbox-control/wrangler.toml
npx wrangler secret put GITHUB_TOKEN --config cloudflare/sandbox-control/wrangler.toml
```

7. Deploy the Worker:

```bash
npx wrangler deploy --config cloudflare/sandbox-control/wrangler.toml
```

## Deploying From CI

The `deploy-sandbox-control` workflow deploys this Worker on every push to `main`
that touches `cloudflare/sandbox-control/**` or `.github/workflows/start-sandbox.yml`.
It needs two repository secrets, `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.

Deploy from CI rather than by hand. The Worker builds the `workflow_dispatch` body
that `start-sandbox.yml` has to declare, so the two are one contract: a change that
lands in the workflow but never reaches the deployed Worker makes GitHub reject the
dispatch with `422 Unexpected inputs provided`, before any run is created. CI
deploying both halves together is what keeps them from drifting.
`make check-dispatch-inputs` compares the two locally and also gates the deploy.

`wrangler deploy` replaces the Worker's plain-text variables with the `[vars]` table
in `wrangler.toml`, so that file is the source of truth once CI deploys — values
edited in the Cloudflare dashboard are overwritten. `TUNNEL_HOSTNAME` may instead be
supplied as the repository variable `SANDBOX_TUNNEL_HOSTNAME`, which is useful when
the real hostname should not be committed. The workflow refuses to deploy while that
value is still the `mcp.example.com` placeholder. Worker secrets are unaffected.

## Start A Sandbox

Use the token stored in the Worker `CONTROL_TOKEN` secret:

```bash
curl -X POST "https://<worker-host>/start" \
  -H "Authorization: Bearer $CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "duration_minutes": "120",
    "permission_mode": "trusted",
    "tunnel_type": "named",
    "tunnel_hostname": "mcp.example.com"
  }'
```

When GitHub accepts the dispatch, the Worker response is `202 Accepted`. Newer GitHub API responses include a workflow run ID and URLs; older responses may only confirm that the dispatch was accepted.

Connect the MCP client to:

```text
https://mcp.example.com/mcp
Authorization: Bearer <CODING_TOOLS_MCP_AUTH_TOKEN>
```

## MCP-Style Control Tool

The same Worker also exposes a minimal MCP-compatible JSON-RPC endpoint at `/mcp`. This is the endpoint to configure when you want an agent to start the sandbox itself instead of using `curl /start` manually.

Recommended agent setup:

1. Configure this Worker as a persistent control MCP server. It is always online and exposes control tools such as `start_coding_tools_sandbox` and `get_coding_tools_sandbox_status`.
2. Configure the fixed Cloudflare Tunnel hostname as a second MCP server. It may be offline until the GitHub Actions job starts, but the URL stays the same when you use a named tunnel.

Example MCP client configuration shape:

```json
{
  "mcpServers": {
    "sandbox-control": {
      "url": "https://<worker-host>/mcp",
      "headers": {
        "Authorization": "Bearer <CONTROL_TOKEN>"
      }
    },
    "coding-tools-sandbox": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <CODING_TOOLS_MCP_AUTH_TOKEN>"
      }
    }
  }
}
```

After the control MCP is connected, the agent can call `start_coding_tools_sandbox` with arguments equivalent to the `/start` request:

```json
{
  "duration_minutes": "120",
  "permission_mode": "trusted",
  "tunnel_type": "named",
  "tunnel_hostname": "mcp.example.com"
}
```

Most MCP hosts do not let an MCP tool rewrite the host's MCP configuration at runtime. The practical closed loop is therefore to preconfigure the second `coding-tools-sandbox` server once with the fixed tunnel URL; the agent uses the first server to start it, then uses the second server after the tunnel becomes reachable.

List the control tool manually:

```bash
curl -X POST "https://<worker-host>/mcp" \
  -H "Authorization: Bearer $CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Call the tool:

```bash
curl -X POST "https://<worker-host>/mcp" \
  -H "Authorization: Bearer $CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "start_coding_tools_sandbox",
      "arguments": {"duration_minutes": "120"}
    }
  }'
```

Check the sandbox Action status through MCP:

```bash
curl -X POST "https://<worker-host>/mcp" \
  -H "Authorization: Bearer $CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "get_coding_tools_sandbox_status",
      "arguments": {
        "run_id": "<workflow_run_id>",
        "check_endpoint": true,
        "tunnel_hostname": "mcp.example.com"
      }
    }
  }'
```

If you do not have a `workflow_run_id`, omit `run_id` and the Worker will return recent `workflow_dispatch` runs for the configured workflow/ref:

```json
{
  "ref": "docker-action",
  "per_page": 5,
  "check_endpoint": true
}
```

Status meanings:

- `queued`: GitHub accepted the run, but it has not started yet.
- `action_running`: the workflow is running, but the Worker did not probe the MCP endpoint.
- `action_running_mcp_not_ready`: the workflow is running, but the fixed MCP endpoint is not responding yet.
- `mcp_ready`: the workflow is running and the fixed MCP endpoint responded to the probe.
- `completed_success`: the workflow finished successfully, which usually means the long-running sandbox job has ended and the tunnel is no longer live.

## Safety Notes

- Keep `CONTROL_TOKEN`, `GITHUB_TOKEN`, `CLOUDFLARE_TUNNEL_TOKEN`, and `CODING_TOOLS_MCP_AUTH_TOKEN` out of committed files.
- Keep `ALLOW_DANGEROUS=false` unless the sandbox is isolated and the MCP client is trusted.
- Keep `ALLOW_IMAGE_OVERRIDE=false` unless callers are allowed to choose arbitrary Docker images.
- The fixed MCP hostname is only live while the GitHub Actions job is running.

## Troubleshooting

If GitHub returns `422` with `Unexpected inputs`, the Worker is newer than the workflow file at the ref being dispatched. Fix it by merging the updated `.github/workflows/start-sandbox.yml` into the configured `GITHUB_REF`, changing the Worker `GITHUB_REF` to a branch that already has the new inputs, or passing `"ref": "<branch-with-updated-workflow>"` in the `/start` or MCP tool request.
