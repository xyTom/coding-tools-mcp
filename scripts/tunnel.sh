#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROVIDER="${1:-cloudflared}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$PROVIDER" in
  cloudflared|cf)
    exec "$SCRIPT_DIR/tunnel-cloudflared.sh" "$@"
    ;;
  ngrok)
    exec "$SCRIPT_DIR/tunnel-ngrok.sh" "$@"
    ;;
  devtunnel|dev-tunnel|ms-devtunnel)
    exec "$SCRIPT_DIR/tunnel-devtunnel.sh" "$@"
    ;;
  -h|--help|help)
    cat <<EOF
Usage: scripts/tunnel.sh [cloudflared|ngrok|devtunnel] [workspace]

Defaults:
  provider: cloudflared
  workspace: CODING_TOOLS_MCP_WORKSPACE or current directory

Environment:
  CODING_TOOLS_MCP_AUTO_INSTALL_TUNNEL=1  install missing tunnel CLI without prompting
  CODING_TOOLS_MCP_AUTH_MODE=bearer       bearer, noauth, or oauth
  CODING_TOOLS_MCP_PORT=8765
  CODING_TOOLS_MCP_TOOL_PROFILE=read-only
  CODING_TOOLS_MCP_AUTH_TOKEN=<existing-token>
  CODING_TOOLS_MCP_SERVER_BIN=coding-tools-mcp

OAuth mode requires CODING_TOOLS_MCP_SERVER_URL. CLIENT_ID, CLIENT_SECRET,
and PASSWORD are generated and printed at startup if unset. See
docs/remote-mcp.md.
EOF
    ;;
  *)
    echo "Unknown provider: $PROVIDER" >&2
    echo "Use one of: cloudflared, ngrok, devtunnel" >&2
    exit 2
    ;;
esac
