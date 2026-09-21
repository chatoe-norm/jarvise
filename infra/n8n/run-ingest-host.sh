#!/usr/bin/env bash
# Host-side helper for n8n / cron: paper ingest then Redis status.
# No order placement.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
OUT="$("$ROOT/bin/jarvise" ingest --symbol BTCUSDT,ETHUSDT --timeframe 1h --limit 200 --skip-derivatives --json)"
echo "$OUT"
if command -v redis-cli >/dev/null; then
  redis-cli -h 127.0.0.1 SET jarvise:ingest:last "$OUT" >/dev/null
  echo "redis: jarvise:ingest:last updated (redis-cli)"
elif command -v docker >/dev/null; then
  printf '%s' "$OUT" | docker compose -f "$ROOT/docker-compose.yml" exec -T redis redis-cli -x SET jarvise:ingest:last >/dev/null
  echo "redis: jarvise:ingest:last updated (docker redis)"
else
  echo "redis-cli/docker not available; skipped status key write"
fi
