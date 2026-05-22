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
  if [[ -z "${CODING_TOOLS_MCP_SERVER_URL:-}" ]]; then
    {
      echo "CODING_TOOLS_MCP_AUTH_MODE=oauth requires CODING_TOOLS_MCP_SERVER_URL"
      echo "(the public base URL the tunnel will terminate at, e.g. https://mcp.example.com)."
      echo "See docs/remote-mcp.md for details."
    } >&2
    return 1
  fi
  if [[ -z "${CODING_TOOLS_MCP_OAUTH_CLIENT_ID:-}" ]]; then
    CODING_TOOLS_MCP_OAUTH_CLIENT_ID="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  fi
  if [[ -z "${CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET:-}" ]]; then
    CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  fi
  if [[ -z "${CODING_TOOLS_MCP_OAUTH_PASSWORD:-}" ]]; then
    CODING_TOOLS_MCP_OAUTH_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  fi
  export CODING_TOOLS_MCP_OAUTH_CLIENT_ID CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET CODING_TOOLS_MCP_OAUTH_PASSWORD
}

start_coding_tools_mcp() {
  local workspace="$1"
  local port="$2"
  local profile="$3"
  local auth_mode="$4"
  local token="$5"
  local server_bin="$6"
  local args=(
    --workspace "$workspace"
    --host 127.0.0.1
    --port "$port"
    --tool-profile "$profile"
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
  local profile="$4"
  local auth_mode="$5"
  local token="$6"

  cat <<EOF
coding-tools-mcp is listening on http://127.0.0.1:$port/mcp
Tool profile: $profile
Auth mode: $auth_mode

$label will print an HTTPS URL.
EOF

  case "$auth_mode" in
    bearer)
      cat <<EOF

Generic MCP clients that support custom headers should use:
URL: https://<$host_placeholder>/mcp
Header: Authorization: Bearer $token

Remote MCP clients that cannot send custom bearer headers should use
CODING_TOOLS_MCP_AUTH_MODE=noauth only with read-only local/testing tunnels,
or rely on an external auth proxy for authenticated production deployments.
EOF
      ;;
    oauth)
      local base="${CODING_TOOLS_MCP_SERVER_URL%/}"
      cat <<EOF

OAuth 2.1 Authorization Code + PKCE is active. Configure your MCP client
with the following values (copy these now -- they are regenerated each run
unless you preset the env vars):

CODING_TOOLS_MCP_SERVER_URL=$base
CODING_TOOLS_MCP_OAUTH_CLIENT_ID=$CODING_TOOLS_MCP_OAUTH_CLIENT_ID
CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET=$CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET
CODING_TOOLS_MCP_OAUTH_PASSWORD=$CODING_TOOLS_MCP_OAUTH_PASSWORD

Authorization metadata: $base/.well-known/oauth-authorization-server
Protected resource:     $base/.well-known/oauth-protected-resource
MCP endpoint:           $base/mcp

The tunnel below must terminate at $base. Ephemeral tunnels (random
subdomain per run) do not work with OAuth -- use a named cloudflared
tunnel, an ngrok reserved domain, or a persistent devtunnel so the
external URL matches CODING_TOOLS_MCP_SERVER_URL across restarts.
EOF
      ;;
    *)
      cat <<EOF

Remote MCP client URL:
https://<$host_placeholder>/mcp

No Authorization header is used. Keep this profile read-only unless you
understand the risk of exposing this tunnel publicly.
EOF
      ;;
  esac
}
