#!/usr/bin/env bash
# Seed OpenClaw config once (paper-only OpenRouter defaults + Jarvise plugin path).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/data/openclaw/openclaw.json"
EXAMPLE="$ROOT/config/openclaw/openclaw.json.example"
mkdir -p "$ROOT/data/openclaw/exports"
mkdir -p "$ROOT/data/analytics/sources/openclaw"
if [[ ! -f "$DEST" ]]; then
  cp "$EXAMPLE" "$DEST"
  echo "seeded $DEST — set OPENROUTER_API_KEY in .env / openclaw env"
  echo "plugin load path: /plugins/jarvise-openclaw (compose mount)"
  echo "exports: $ROOT/data/openclaw/exports → jarvise rag sync-openclaw"
else
  echo "already present: $DEST"
  echo "ensure plugins.load.paths includes /plugins/jarvise-openclaw (see $EXAMPLE)"
fi
