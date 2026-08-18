# Tunnel integrations

These scripts expose a local coding-tools MCP server through a supported tunnel provider.

They are user-facing runtime integrations rather than repository-maintenance scripts, so they live outside `scripts/`.

```bash
CODING_TOOLS_MCP_AUTH_MODE=bearer ./integrations/tunnels/tunnel.sh cloudflared /path/to/repo
```

Supported providers are `cloudflared`, `ngrok`, and `devtunnel`.
