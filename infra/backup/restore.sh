#!/bin/bash
# Restore a Jarvise backup directory produced by backup.sh.
#
# Usage:
#   infra/backup/restore.sh --from /opt/jarvise/backups/2026-10-03 --verify          # checksums + sqlite integrity only
#   infra/backup/restore.sh --from /opt/jarvise/backups/2026-10-03 --sqlite           # restore jarvise.db (stack stopped)
#   infra/backup/restore.sh --from DIR --sqlite --qdrant --redis --openclaw           # full restore
#
# Restores stop the affected services, replace data, and start them again. Nothing is
# deleted until the archive verifies. Kill-switch state comes back with Redis — check
# Ops after restore and clear it only after a written review.
set -euo pipefail

ROOT="${JARVISE_ROOT:-/opt/jarvise}"
COMPOSE=(docker compose -f "$ROOT/docker-compose.yml" -f "$ROOT/docker-compose.prod.yml")
FROM=""; VERIFY=0; DO_SQLITE=0; DO_QDRANT=0; DO_REDIS=0; DO_OPENCLAW=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --verify) VERIFY=1; shift ;;
    --sqlite) DO_SQLITE=1; shift ;;
    --qdrant) DO_QDRANT=1; shift ;;
    --redis) DO_REDIS=1; shift ;;
    --openclaw) DO_OPENCLAW=1; shift ;;
    --all) DO_SQLITE=1; DO_QDRANT=1; DO_REDIS=1; DO_OPENCLAW=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -d "$FROM" ]] || { echo "--from DIR required and must exist" >&2; exit 2; }
log() { printf '[restore %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

compose_volume() {
  docker volume ls -q \
    --filter "label=com.docker.compose.project=jarvise" \
    --filter "label=com.docker.compose.volume=$1" | head -n 1
}

log "verifying checksums in $FROM"
( cd "$FROM" && sha256sum -c SHA256SUMS )
if [[ -f "$FROM/jarvise.db.gz" ]]; then
  TMP="$(mktemp -d)"
  gunzip -c "$FROM/jarvise.db.gz" > "$TMP/jarvise.db"
  python3 - "$TMP/jarvise.db" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as c:
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]
print(f"sqlite integrity={ok} user_version={ver} tables={len(tables)}")
sys.exit(0 if ok == "ok" else 1)
PY
fi
if [[ -f "$FROM/qdrant.tgz" ]]; then
  log "qdrant archive entries: $(tar tzf "$FROM/qdrant.tgz" | wc -l)"
fi
cat "$FROM/MANIFEST" 2>/dev/null || true
[[ "$VERIFY" -eq 1 && $((DO_SQLITE+DO_QDRANT+DO_REDIS+DO_OPENCLAW)) -eq 0 ]] && { log "verify only — done"; exit 0; }

if [[ "$DO_SQLITE" -eq 1 ]]; then
  [[ -n "${TMP:-}" ]] || { echo "no jarvise.db.gz in archive" >&2; exit 1; }
  log "restoring sqlite (stopping jobs, web, n8n)"
  "${COMPOSE[@]}" stop jobs web n8n >/dev/null
  DB="$ROOT/data/analytics/jarvise.db"
  [[ -f "$DB" ]] && cp -a "$DB" "$DB.pre-restore.$(date -u +%s)"
  rm -f "$DB-wal" "$DB-shm"
  install -m 0644 "$TMP/jarvise.db" "$DB"
  "${COMPOSE[@]}" start jobs web n8n >/dev/null
fi

if [[ "$DO_QDRANT" -eq 1 ]]; then
  QV="$(compose_volume jarvise_qdrant)"
  [[ -n "$QV" ]] || { echo "qdrant compose volume not found; run compose up once first" >&2; exit 1; }
  log "restoring qdrant volume ($QV)"
  "${COMPOSE[@]}" stop qdrant >/dev/null
  docker run --rm -v "$QV":/qdrant/storage -v "$FROM":/backup:ro alpine \
    sh -c 'rm -rf /qdrant/storage/* && tar xzf /backup/qdrant.tgz -C /qdrant/storage'
  "${COMPOSE[@]}" start qdrant >/dev/null
fi

if [[ "$DO_REDIS" -eq 1 ]]; then
  RV="$(compose_volume jarvise_redis)"
  [[ -n "$RV" ]] || { echo "redis compose volume not found; run compose up once first" >&2; exit 1; }
  log "restoring redis volume ($RV) (kill-switch state returns with it)"
  "${COMPOSE[@]}" stop redis >/dev/null
  docker run --rm -v "$RV":/data -v "$FROM":/backup:ro alpine \
    sh -c 'rm -rf /data/* && tar xzf /backup/redis.tgz -C /data'
  "${COMPOSE[@]}" start redis >/dev/null
fi

if [[ "$DO_OPENCLAW" -eq 1 && -f "$FROM/openclaw.tgz" ]]; then
  log "restoring openclaw notes"
  "${COMPOSE[@]}" stop openclaw >/dev/null
  tar xzf "$FROM/openclaw.tgz" -C "$ROOT"
  "${COMPOSE[@]}" start openclaw >/dev/null
fi

log "restore complete — run: ${COMPOSE[*]} ps && curl -fsS http://\$TAILSCALE_IP:8080/healthz"
log "then open Ops (:8080/ops): confirm kill-switch state before any paper/live action"
