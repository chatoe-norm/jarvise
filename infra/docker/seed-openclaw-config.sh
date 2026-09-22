#!/usr/bin/env bash
# Seed OpenClaw config once (paper-only OpenRouter defaults + Jarvise skills path).
# Do not put undocumented keys (e.g. _jarvise) in openclaw.json — Gateway rejects them.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/data/openclaw/openclaw.json"
EXAMPLE="$ROOT/config/openclaw/openclaw.json.example"
mkdir -p "$ROOT/data/openclaw/exports"
mkdir -p "$ROOT/data/analytics/sources/openclaw"
if [[ ! -f "$DEST" ]]; then
  cp "$EXAMPLE" "$DEST"
  echo "seeded $DEST — set OPENROUTER_API_KEY in .env / openclaw env"
  echo "skills.load.extraDirs: /plugins/jarvise-openclaw/skills (compose mount)"
  echo "exports: $ROOT/data/openclaw/exports → jarvise rag sync-openclaw"
else
  echo "already present: $DEST"
  echo "ensure skills.load.extraDirs includes /plugins/jarvise-openclaw/skills (see $EXAMPLE)"
fi
