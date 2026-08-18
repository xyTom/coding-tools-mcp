# Infrastructure

Deployment and control-plane components that support coding-tools-mcp live here.

- `cloudflare/sandbox-control/`: Cloudflare Worker that dispatches the sandbox GitHub Actions workflow.

Infrastructure code may share contracts with `.github/workflows/`; update both sides atomically when their inputs or deployment paths change.
