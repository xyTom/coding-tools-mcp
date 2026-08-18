#!/usr/bin/env bash

prompt_install() {
  local tool="$1"
  if [[ "${CODING_TOOLS_MCP_AUTO_INSTALL_TUNNEL:-}" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "$tool is not installed and stdin is not interactive." >&2
    echo "Set CODING_TOOLS_MCP_AUTO_INSTALL_TUNNEL=1 to install automatically." >&2
    return 1
  fi
  local answer
  read -r -p "$tool is not installed. Install it now? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" || "$answer" == "yes" || "$answer" == "YES" ]]
}

ensure_local_bin_on_path() {
  mkdir -p "$HOME/.local/bin"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
  esac
  if [[ -d "$HOME/.dotnet/tools" ]]; then
    case ":$PATH:" in
      *":$HOME/.dotnet/tools:"*) ;;
      *) export PATH="$HOME/.dotnet/tools:$PATH" ;;
    esac
  fi
}

download_to_file() {
  local url="$1"
  local output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$output"
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO "$output" "$url"
    return
  fi
  echo "Need curl or wget to download $url" >&2
  return 1
}

install_cloudflared() {
  if ! prompt_install cloudflared; then
    return 1
  fi
  if command -v brew >/dev/null 2>&1; then
    brew install cloudflared
    return
  fi
  ensure_local_bin_on_path
  local os arch suffix
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os:$arch" in
    Linux:x86_64|Linux:amd64) suffix="linux-amd64" ;;
    Linux:aarch64|Linux:arm64) suffix="linux-arm64" ;;
    Darwin:x86_64) suffix="darwin-amd64" ;;
    Darwin:arm64) suffix="darwin-arm64" ;;
    *)
      echo "Unsupported platform for automatic cloudflared install: $os $arch" >&2
      return 1
      ;;
  esac
  download_to_file \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-$suffix" \
    "$HOME/.local/bin/cloudflared"
  chmod +x "$HOME/.local/bin/cloudflared"
}

install_ngrok() {
  if ! prompt_install ngrok; then
    return 1
  fi
  if command -v brew >/dev/null 2>&1; then
    brew install ngrok/ngrok/ngrok
    return
  fi
  if command -v npm >/dev/null 2>&1; then
    npm install -g ngrok
    return
  fi
  echo "Automatic ngrok install needs Homebrew or npm." >&2
  echo "Install manually from https://ngrok.com/download and rerun this script." >&2
  return 1
}

install_devtunnel() {
  if ! prompt_install devtunnel; then
    return 1
  fi
  if command -v winget >/dev/null 2>&1; then
    winget install Microsoft.devtunnel
    return
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "Automatic devtunnel install needs curl." >&2
    return 1
  fi
  curl -fsSL https://aka.ms/DevTunnelCliInstall | bash
  ensure_local_bin_on_path
}

ensure_tunnel_command() {
  local tool="$1"
  if command -v "$tool" >/dev/null 2>&1; then
    return 0
  fi
  case "$tool" in
    cloudflared) install_cloudflared ;;
    ngrok) install_ngrok ;;
    devtunnel) install_devtunnel ;;
    *)
      echo "Unknown tunnel tool: $tool" >&2
      return 1
      ;;
  esac
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is still not available on PATH after install." >&2
    return 1
  fi
}

require_oauth_env() {
  if [[ -z "${CODING_TOOLS_MCP_OAUTH_PASSWORD:-}" ]]; then
    CODING_TOOLS_MCP_OAUTH_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  fi
  export CODING_TOOLS_MCP_OAUTH_PASSWORD
}

# Validates $AUTH_MODE and sets $TOKEN (exporting CODING_TOOLS_MCP_AUTH_TOKEN in bearer mode).
resolve_auth_credentials() {
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
}

start_coding_tools_mcp() {
  local workspace="$1"
  local port="$2"
  local auth_mode="$3"
  local token="$4"
  local server_bin="$5"
  local args=(
    --workspace "$workspace"
    --host 127.0.0.1
    --port "$port"
  )
  case "$auth_mode" in
    bearer) args+=(--auth-token "$token") ;;
    oauth) args+=(--oauth-mode) ;;
  esac

  "$server_bin" "${args[@]}" &
  SERVER_PID=$!
  trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
}

print_tunnel_config() {
  local label="$1"
  local host_placeholder="$2"
  local port="$3"
  local auth_mode="$4"
  local token="$5"

  cat <<EOF
coding-tools-mcp is listening on http://127.0.0.1:$port/mcp
Auth mode: $auth_mode

$label will print an HTTPS URL.
EOF

  case "$auth_mode" in
    bearer)
      cat <<EOF

Generic MCP clients that support custom headers should use:
URL: https://<$host_placeholder>/mcp
Header: Authorization: Bearer $token

Remote MCP clients that cannot send custom bearer headers should use OAuth or
an external authentication proxy. Do not expose the fixed mutation-capable
tool set through an unauthenticated public tunnel.
EOF
      ;;
    oauth)
      local base="${CODING_TOOLS_MCP_SERVER_URL:-https://<$host_placeholder>}"
      base="${base%/}"
      cat <<EOF

OAuth 2.1 Authorization Code + PKCE is active. Configure your MCP client
with the HTTPS URL printed by $label after it starts. The server derives
its OAuth issuer from that request URL unless CODING_TOOLS_MCP_SERVER_URL
is preset.

OAuth password: $CODING_TOOLS_MCP_OAUTH_PASSWORD
Client registration: $base/oauth/register (RFC 7591)

Authorization metadata: $base/.well-known/oauth-authorization-server
Protected resource:     $base/.well-known/oauth-protected-resource
MCP endpoint:           $base/mcp
EOF
      ;;
    *)
      cat <<EOF

Remote MCP client URL:
https://<$host_placeholder>/mcp

No Authorization header is used. The fixed tool set includes mutation and
command execution; do not expose this tunnel publicly without authentication.
EOF
      ;;
  esac
}

# Shared preamble for the provider scripts: resolves the standard env
# defaults, starts the MCP server, and prints the connection config. The
# caller then runs its provider-specific tunnel command using $PORT.
tunnel_setup() {
  local tool="$1" label="$2" host_placeholder="$3" workspace_arg="${4:-}"
  WORKSPACE="${workspace_arg:-${CODING_TOOLS_MCP_WORKSPACE:-$PWD}}"
  PORT="${CODING_TOOLS_MCP_PORT:-8765}"
  SERVER_BIN="${CODING_TOOLS_MCP_SERVER_BIN:-coding-tools-mcp}"
  AUTH_MODE="${CODING_TOOLS_MCP_AUTH_MODE:-bearer}"

  resolve_auth_credentials

  ensure_tunnel_command "$tool"
  start_coding_tools_mcp "$WORKSPACE" "$PORT" "$AUTH_MODE" "$TOKEN" "$SERVER_BIN"
  print_tunnel_config "$label" "$host_placeholder" "$PORT" "$AUTH_MODE" "$TOKEN"
}
