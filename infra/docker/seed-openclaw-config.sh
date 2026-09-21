#!/usr/bin/env bash
# Seed OpenClaw config once (paper-only OpenRouter defaults).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/data/openclaw/openclaw.json"
EXAMPLE="$ROOT/config/openclaw/openclaw.json.example"
mkdir -p "$ROOT/data/openclaw"
if [[ ! -f "$DEST" ]]; then
  cp "$EXAMPLE" "$DEST"
  echo "seeded $DEST — set OPENROUTER_API_KEY in .env / openclaw env"
else
  echo "already present: $DEST"
fi
