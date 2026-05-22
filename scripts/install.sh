#!/usr/bin/env bash
set -euo pipefail

PACKAGE_NAME="coding-tools-mcp"
SCRIPT_NAME="coding-tools-mcp"
METHOD="${CODING_TOOLS_MCP_INSTALL_METHOD:-auto}"
VERSION="${CODING_TOOLS_MCP_VERSION:-}"
WITH_IMAGE=0
VERIFY=1
ACTION="install"
TUNNEL_PROVIDER="${CODING_TOOLS_MCP_TUNNEL_PROVIDER:-cloudflared}"
WORKSPACE="${CODING_TOOLS_MCP_WORKSPACE:-$PWD}"
PORT="${CODING_TOOLS_MCP_PORT:-8765}"
PROFILE="${CODING_TOOLS_MCP_TOOL_PROFILE:-}"
AUTH_MODE="${CODING_TOOLS_MCP_AUTH_MODE:-}"
AUTH_TOKEN="${CODING_TOOLS_MCP_AUTH_TOKEN:-}"
SERVER_BIN="${CODING_TOOLS_MCP_SERVER_BIN:-}"
SERVER_PID=""
TUNNEL_TOOL=""

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options] [workspace]

Install coding-tools-mcp from PyPI, optionally start the MCP server, and
optionally expose it through a tunnel.

Default action:
  Install or update the published coding-tools-mcp command from PyPI.

Actions:
  --start                       Install, then start local HTTP MCP.
  --tunnel [provider]           Install, start local HTTP MCP, then expose it.
                                Providers: cloudflared, ngrok, devtunnel.
  --install-only                Install only. This is the default.

Install options:
  --version VERSION             Install an exact package version.
  --with-image                  Install the optional image extra.
  --method auto|uv|pip          Choose installer. Default: auto.
  --no-verify                   Skip the post-install command check.

Server options:
  --workspace PATH              Workspace to expose. Default: current dir.
  --port PORT                   Local HTTP port. Default: 8765.
  --profile PROFILE             Tool profile. Defaults: full local, read-only tunnel.
  --auth-mode bearer|noauth|oauth
                                Defaults: noauth local, bearer tunnel. OAuth
                                tunnel mode requires CODING_TOOLS_MCP_SERVER_URL
                                (local --start defaults it to loopback).
                                CLIENT_ID, CLIENT_SECRET, and PASSWORD are
                                generated and printed if unset.
  --auth-token TOKEN            Bearer token. Generated if needed.
  --server-bin PATH             Use an existing coding-tools-mcp binary.

Tunnel options:
  --provider PROVIDER           Same as --tunnel PROVIDER after --tunnel.
  --auto-install-tunnel         Install missing tunnel CLI without prompting.

Environment:
  CODING_TOOLS_MCP_VERSION=0.1.4
  CODING_TOOLS_MCP_INSTALL_METHOD=auto|uv|pip
  CODING_TOOLS_MCP_WORKSPACE=/path/to/repo
  CODING_TOOLS_MCP_TUNNEL_PROVIDER=cloudflared|ngrok|devtunnel
  CODING_TOOLS_MCP_AUTO_INSTALL_TUNNEL=1
  PYTHON=python3

Examples:
  scripts/install.sh
  scripts/install.sh --start --workspace /path/to/repo
  scripts/install.sh --tunnel cloudflared --workspace /path/to/repo
  scripts/install.sh --tunnel ngrok --auto-install-tunnel /path/to/repo
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

log() {
  echo "==> $*" >&2
}

run() {
  echo "+ $*" >&2
  "$@"
}

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
}

package_spec() {
  local name="$PACKAGE_NAME"
  if [[ "$WITH_IMAGE" == "1" ]]; then
    name="${name}[image]"
  fi
  if [[ -n "$VERSION" ]]; then
    printf "%s==%s\n" "$name" "$VERSION"
  else
    printf "%s\n" "$name"
  fi
}

find_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf "%s\n" "$PYTHON"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  return 1
}

select_method() {
  case "$METHOD" in
    auto)
      if command -v uv >/dev/null 2>&1; then
        printf "uv\n"
      else
        printf "pip\n"
      fi
      ;;
    uv|pip)
      printf "%s\n" "$METHOD"
      ;;
    *)
      die "CODING_TOOLS_MCP_INSTALL_METHOD must be auto, uv, or pip"
      ;;
  esac
}

find_installed_command() {
  if [[ -n "$SERVER_BIN" ]]; then
    printf "%s\n" "$SERVER_BIN"
    return
  fi
  if command -v "$SCRIPT_NAME" >/dev/null 2>&1; then
    command -v "$SCRIPT_NAME"
    return
  fi
  if [[ -x "$HOME/.local/bin/$SCRIPT_NAME" ]]; then
    printf "%s\n" "$HOME/.local/bin/$SCRIPT_NAME"
    return
  fi
  return 1
}

warn_path() {
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
      cat >&2 <<EOF

Installed command was not found on PATH.
Add this to your shell profile if your installer placed it in ~/.local/bin:

  export PATH="\$HOME/.local/bin:\$PATH"
EOF
      ;;
  esac
}

prompt_install() {
  local tool="$1"
  if [[ "${CODING_TOOLS_MCP_AUTO_INSTALL_TUNNEL:-}" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "$tool is not installed and stdin is not interactive." >&2
    echo "Pass --auto-install-tunnel or install $tool manually." >&2
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
  local provider="$1"
  local tool="$provider"
  case "$provider" in
    cloudflared|cf) tool="cloudflared" ;;
    ngrok) tool="ngrok" ;;
    devtunnel|dev-tunnel|ms-devtunnel) tool="devtunnel" ;;
    *) die "unknown tunnel provider: $provider" ;;
  esac
  if command -v "$tool" >/dev/null 2>&1; then
    TUNNEL_TOOL="$tool"
    return
  fi
  case "$tool" in
    cloudflared) install_cloudflared ;;
    ngrok) install_ngrok ;;
    devtunnel) install_devtunnel ;;
  esac
  if ! command -v "$tool" >/dev/null 2>&1; then
    die "$tool is still not available on PATH after install"
  fi
  TUNNEL_TOOL="$tool"
}

generate_token() {
  local python_bin
  if python_bin="$(find_python)"; then
    "$python_bin" -c 'import secrets; print(secrets.token_urlsafe(32))'
    return
  fi
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n'
    printf "\n"
    return
  fi
  die "need python or openssl to generate an auth token"
}

resolve_runtime_defaults() {
  case "$ACTION" in
    install) ;;
    start)
      PROFILE="${PROFILE:-full}"
      AUTH_MODE="${AUTH_MODE:-noauth}"
      ;;
    tunnel)
      PROFILE="${PROFILE:-read-only}"
      AUTH_MODE="${AUTH_MODE:-bearer}"
      ;;
  esac
  case "$AUTH_MODE" in
    ""|bearer|noauth) ;;
    oauth) require_oauth_env_install ;;
    *) die "--auth-mode must be bearer, noauth, or oauth" ;;
  esac
  if [[ "$AUTH_MODE" == "bearer" && -z "$AUTH_TOKEN" ]]; then
    AUTH_TOKEN="$(generate_token)"
  fi
}

require_oauth_env_install() {
  if [[ -z "${CODING_TOOLS_MCP_SERVER_URL:-}" ]]; then
    if [[ "$ACTION" == "start" ]]; then
      export CODING_TOOLS_MCP_SERVER_URL="http://127.0.0.1:$PORT"
    else
      {
        echo "--auth-mode oauth requires CODING_TOOLS_MCP_SERVER_URL"
        echo "(the public base URL the tunnel will terminate at, e.g. https://mcp.example.com)."
        echo "See docs/remote-mcp.md for details."
      } >&2
      exit 2
    fi
  fi
  if [[ -z "${CODING_TOOLS_MCP_OAUTH_CLIENT_ID:-}" ]]; then
    CODING_TOOLS_MCP_OAUTH_CLIENT_ID="$(generate_token)"
  fi
  if [[ -z "${CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET:-}" ]]; then
    CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET="$(generate_token)"
  fi
  if [[ -z "${CODING_TOOLS_MCP_OAUTH_PASSWORD:-}" ]]; then
    CODING_TOOLS_MCP_OAUTH_PASSWORD="$(generate_token)"
  fi
  export CODING_TOOLS_MCP_OAUTH_CLIENT_ID CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET CODING_TOOLS_MCP_OAUTH_PASSWORD
}

server_args() {
  local args=(
    --workspace "$WORKSPACE"
    --host 127.0.0.1
    --port "$PORT"
    --tool-profile "$PROFILE"
  )
  case "$AUTH_MODE" in
    bearer) args+=(--auth-token "$AUTH_TOKEN") ;;
    oauth) args+=(--oauth-mode) ;;
  esac
  printf "%s\0" "${args[@]}"
}

print_local_config() {
  cat <<EOF
coding-tools-mcp will listen on http://127.0.0.1:$PORT/mcp
Workspace: $WORKSPACE
Tool profile: $PROFILE
Auth mode: $AUTH_MODE
EOF
  case "$AUTH_MODE" in
    bearer)
      cat <<EOF
Header: Authorization: Bearer $AUTH_TOKEN
EOF
      ;;
    oauth)
      local base="${CODING_TOOLS_MCP_SERVER_URL%/}"
      cat <<EOF
OAuth issuer: $base
CODING_TOOLS_MCP_OAUTH_CLIENT_ID=$CODING_TOOLS_MCP_OAUTH_CLIENT_ID
CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET=$CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET
CODING_TOOLS_MCP_OAUTH_PASSWORD=$CODING_TOOLS_MCP_OAUTH_PASSWORD
Authorization metadata: $base/.well-known/oauth-authorization-server
Protected resource:     $base/.well-known/oauth-protected-resource
EOF
      ;;
  esac
}

print_tunnel_config() {
  local label="$1"
  local host_placeholder="$2"
  cat <<EOF
coding-tools-mcp is listening on http://127.0.0.1:$PORT/mcp
Workspace: $WORKSPACE
Tool profile: $PROFILE
Auth mode: $AUTH_MODE

$label will print an HTTPS URL.
EOF
  case "$AUTH_MODE" in
    bearer)
      cat <<EOF

Generic MCP clients that support custom headers should use:
URL: https://<$host_placeholder>/mcp
Header: Authorization: Bearer $AUTH_TOKEN
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

The tunnel below must terminate at $base. Ephemeral tunnels do not work
with OAuth -- use a named cloudflared tunnel, an ngrok reserved domain,
or a persistent devtunnel so the external URL matches across restarts.
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

install_package() {
  local spec installer
  spec="$(package_spec)"
  installer="$(select_method)"
  log "Installing ${spec} with ${installer}"
  case "$installer" in
    uv)
      command -v uv >/dev/null 2>&1 || die "uv is not installed; rerun with --method pip or install uv"
      run uv tool install --force "$spec"
      ;;
    pip)
      local python_bin
      python_bin="$(find_python)" || die "python3 or python is required for pip install"
      run "$python_bin" -m pip install --user --upgrade "$spec"
      ;;
    *)
      die "unknown installer: $installer"
      ;;
  esac
  if [[ "$VERIFY" == "1" ]]; then
    local bin
    if bin="$(find_installed_command)"; then
      run "$bin" --help >/dev/null
      log "Installed command: $bin"
    else
      warn_path
      die "installed package but could not locate ${SCRIPT_NAME}"
    fi
  fi
}

start_local_server() {
  local bin
  bin="$(find_installed_command)" || die "could not locate ${SCRIPT_NAME}; install failed or PATH is missing"
  [[ -d "$WORKSPACE" ]] || die "workspace does not exist: $WORKSPACE"
  print_local_config
  local args=()
  while IFS= read -r -d '' arg; do
    args+=("$arg")
  done < <(server_args)
  exec "$bin" "${args[@]}"
}

start_tunnel() {
  local bin tool
  bin="$(find_installed_command)" || die "could not locate ${SCRIPT_NAME}; install failed or PATH is missing"
  [[ -d "$WORKSPACE" ]] || die "workspace does not exist: $WORKSPACE"
  ensure_tunnel_command "$TUNNEL_PROVIDER"
  tool="$TUNNEL_TOOL"
  local args=()
  while IFS= read -r -d '' arg; do
    args+=("$arg")
  done < <(server_args)
  "$bin" "${args[@]}" &
  SERVER_PID=$!
  trap cleanup EXIT INT TERM
  sleep 1
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    die "coding-tools-mcp exited before the tunnel started"
  fi
  case "$tool" in
    cloudflared)
      print_tunnel_config "cloudflared" "cloudflared-host"
      cloudflared tunnel --url "http://127.0.0.1:$PORT"
      ;;
    ngrok)
      print_tunnel_config "ngrok" "ngrok-host"
      ngrok http "http://127.0.0.1:$PORT"
      ;;
    devtunnel)
      print_tunnel_config "Microsoft Dev Tunnel" "devtunnel-host"
      devtunnel host --port "$PORT" --protocol http --allow-anonymous
      ;;
    *)
      die "unknown tunnel tool: $tool"
      ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || die "--version requires a value"
      VERSION="$2"
      shift
      ;;
    --with-image)
      WITH_IMAGE=1
      ;;
    --method)
      [[ $# -ge 2 ]] || die "--method requires a value"
      METHOD="$2"
      shift
      ;;
    --no-verify)
      VERIFY=0
      ;;
    --start)
      ACTION="start"
      ;;
    --install-only)
      ACTION="install"
      ;;
    --tunnel)
      ACTION="tunnel"
      if [[ $# -ge 2 && "$2" != --* ]]; then
        TUNNEL_PROVIDER="$2"
        shift
      fi
      ;;
    --provider)
      [[ $# -ge 2 ]] || die "--provider requires a value"
      TUNNEL_PROVIDER="$2"
      shift
      ;;
    --workspace)
      [[ $# -ge 2 ]] || die "--workspace requires a value"
      WORKSPACE="$2"
      shift
      ;;
    --port)
      [[ $# -ge 2 ]] || die "--port requires a value"
      PORT="$2"
      shift
      ;;
    --profile)
      [[ $# -ge 2 ]] || die "--profile requires a value"
      PROFILE="$2"
      shift
      ;;
    --auth-mode)
      [[ $# -ge 2 ]] || die "--auth-mode requires a value"
      AUTH_MODE="$2"
      shift
      ;;
    --auth-token)
      [[ $# -ge 2 ]] || die "--auth-token requires a value"
      AUTH_TOKEN="$2"
      shift
      ;;
    --server-bin)
      [[ $# -ge 2 ]] || die "--server-bin requires a value"
      SERVER_BIN="$2"
      shift
      ;;
    --auto-install-tunnel)
      export CODING_TOOLS_MCP_AUTO_INSTALL_TUNNEL=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      die "unknown argument: $1"
      ;;
    *)
      WORKSPACE="$1"
      ;;
  esac
  shift
done

resolve_runtime_defaults
install_package

case "$ACTION" in
  install)
    log "Install completed"
    ;;
  start)
    start_local_server
    ;;
  tunnel)
    start_tunnel
    ;;
  *)
    die "unknown action: $ACTION"
    ;;
esac
