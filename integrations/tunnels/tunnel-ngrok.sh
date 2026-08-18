#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/tunnel-common.sh"

tunnel_setup ngrok "ngrok" "ngrok-host" "${1:-}"
ngrok http "http://127.0.0.1:$PORT"
