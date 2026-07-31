#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/tunnel-common.sh"

tunnel_setup devtunnel "Microsoft Dev Tunnel" "devtunnel-host" "${1:-}"
devtunnel host --port "$PORT" --protocol http --allow-anonymous
