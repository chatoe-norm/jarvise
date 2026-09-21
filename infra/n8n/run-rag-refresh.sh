#!/usr/bin/env bash
# Host-side RAG refresh for n8n / cron. Paper only. No order placement.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

export JARVISE_PAPER_ONLY=true
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"

if command -v docker >/dev/null 2>&1 && docker compose ps --status running 2>/dev/null | grep -q qdrant; then
  docker compose --profile tools run --rm worker jarvise rag refresh --output json
  exit $?
fi

if [[ -x "$ROOT/.venv/bin/jarvise" ]]; then
  "$ROOT/.venv/bin/jarvise" rag refresh --output json
  exit $?
fi

if [[ -x "$ROOT/.venv/Scripts/jarvise.exe" ]]; then
  "$ROOT/.venv/Scripts/jarvise.exe" rag refresh --output json
  exit $?
fi

echo "Error: no worker container or .venv jarvise found" >&2
exit 2
