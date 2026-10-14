#!/usr/bin/env bash
# Cursor MCP wrapper: n8n API on Tailscale. Paper-only — no order placement.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
export JARVISE_PAPER_ONLY=true
export N8N_API_URL="${N8N_API_URL:-http://100.93.110.48:5678}"
if [[ -z "${N8N_API_KEY:-}" ]]; then
  echo "N8N_API_KEY missing in $ROOT/.env" >&2
  exit 1
fi
export PATH="/Users/gizmo-macbook/.local/node/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:${PATH}"
exec npx -y @n8n/mcp-server
