#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/tunnel-common.sh"

tunnel_setup cloudflared "cloudflared" "cloudflared-host" "${1:-}"
cloudflared tunnel --url "http://127.0.0.1:$PORT"
