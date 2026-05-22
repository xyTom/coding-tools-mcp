#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/tunnel-common.sh"

WORKSPACE="${1:-${CODING_TOOLS_MCP_WORKSPACE:-$PWD}}"
PORT="${CODING_TOOLS_MCP_PORT:-8765}"
PROFILE="${CODING_TOOLS_MCP_TOOL_PROFILE:-read-only}"
SERVER_BIN="${CODING_TOOLS_MCP_SERVER_BIN:-coding-tools-mcp}"
AUTH_MODE="${CODING_TOOLS_MCP_AUTH_MODE:-bearer}"
TOKEN=""

case "$AUTH_MODE" in
  bearer)
    TOKEN="${CODING_TOOLS_MCP_AUTH_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
    export CODING_TOOLS_MCP_AUTH_TOKEN="$TOKEN"
    ;;
  noauth) ;;
  oauth)
    require_oauth_env || exit 2
    ;;
  *)
    echo "CODING_TOOLS_MCP_AUTH_MODE must be bearer, noauth, or oauth" >&2
    exit 2
    ;;
esac

ensure_tunnel_command ngrok
start_coding_tools_mcp "$WORKSPACE" "$PORT" "$PROFILE" "$AUTH_MODE" "$TOKEN" "$SERVER_BIN"
print_tunnel_config "ngrok" "ngrok-host" "$PORT" "$PROFILE" "$AUTH_MODE" "$TOKEN"
ngrok http "http://127.0.0.1:$PORT"
