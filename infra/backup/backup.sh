#!/bin/bash
# Nightly Jarvise backup: SQLite (online .backup), Qdrant volume, Redis AOF, OpenClaw notes.
# Run as root on the VPS (cron). Never touches co-tenant stacks (T4Trip) or prunes Docker volumes.
#
# Usage: infra/backup/backup.sh [--dest DIR] [--keep N] [--no-offbox]
# Env (from /opt/jarvise/.env when present):
#   JARVISE_BACKUP_DEST   rsync target for off-box copy (e.g. user@host:/backups/jarvise or /mnt/usb/jarvise)
#   JARVISE_BACKUP_KEEP   local days to keep (default 14)
set -euo pipefail

ROOT="${JARVISE_ROOT:-/opt/jarvise}"
COMPOSE=(docker compose -f "$ROOT/docker-compose.yml" -f "$ROOT/docker-compose.prod.yml")
DEST_BASE="${JARVISE_BACKUP_DIR:-$ROOT/backups}"
KEEP="${JARVISE_BACKUP_KEEP:-14}"
OFFBOX=1

if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source <(grep -E '^(JARVISE_BACKUP_DEST|JARVISE_BACKUP_KEEP|JARVISE_BACKUP_DIR)=' "$ROOT/.env" || true); set +a
  DEST_BASE="${JARVISE_BACKUP_DIR:-$DEST_BASE}"
  KEEP="${JARVISE_BACKUP_KEEP:-$KEEP}"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) DEST_BASE="$2"; shift 2 ;;
    --keep) KEEP="$2"; shift 2 ;;
    --no-offbox) OFFBOX=0; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

STAMP="$(date -u +%F)"
OUT="$DEST_BASE/$STAMP"
mkdir -p "$OUT"
log() { printf '[backup %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# Compose prefixes volumes with the project name (jarvise_jarvise_qdrant); resolve by label so we
# never tar an empty look-alike volume by accident.
compose_volume() {
  docker volume ls -q \
    --filter "label=com.docker.compose.project=jarvise" \
    --filter "label=com.docker.compose.volume=$1" | head -n 1
}
QDRANT_VOL="$(compose_volume jarvise_qdrant)"
REDIS_VOL="$(compose_volume jarvise_redis)"
[[ -n "$QDRANT_VOL" && -n "$REDIS_VOL" ]] || { echo "could not resolve compose volumes (qdrant='$QDRANT_VOL' redis='$REDIS_VOL')" >&2; exit 1; }

# 1. SQLite: online, consistent copy via the backup API (works under WAL without stopping jobs/web).
DB="$ROOT/data/analytics/jarvise.db"
if [[ -f "$DB" ]]; then
  log "sqlite .backup → jarvise.db"
  python3 - "$DB" "$OUT/jarvise.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
    ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    ver = d.execute("PRAGMA user_version").fetchone()[0]
print(f"integrity={ok} user_version={ver}")
if ok != "ok":
    sys.exit(1)
PY
  gzip -f "$OUT/jarvise.db"
else
  log "no sqlite db at $DB (skipped)"
fi

# 2. Qdrant: snapshot the named volume while the container is stopped briefly (consistent files).
log "qdrant volume ($QDRANT_VOL) → qdrant.tgz"
"${COMPOSE[@]}" stop qdrant >/dev/null
docker run --rm -v "$QDRANT_VOL":/qdrant/storage:ro -v "$OUT":/backup alpine \
  tar czf /backup/qdrant.tgz -C /qdrant/storage .
"${COMPOSE[@]}" start qdrant >/dev/null

# 3. Redis: kill-switch + job status. Force an RDB point-in-time save, then copy the data dir.
log "redis ($REDIS_VOL) → redis.tgz"
"${COMPOSE[@]}" exec -T redis redis-cli BGSAVE >/dev/null || true
sleep 2
docker run --rm -v "$REDIS_VOL":/data:ro -v "$OUT":/backup alpine \
  tar czf /backup/redis.tgz -C /data .

# 4. OpenClaw state (agents, sessions, exports, config) + doctrine extracts. Installed plugin
#    trees, caches, and logs are re-created by the image/update path and are excluded.
log "openclaw state + sources → openclaw.tgz / sources.tgz"
tar czf "$OUT/openclaw.tgz" -C "$ROOT" \
  --exclude='data/openclaw/extensions' --exclude='data/openclaw/py-mcp' \
  --exclude='data/openclaw/cache' --exclude='data/openclaw/logs' \
  --exclude='*/node_modules' data/openclaw 2>/dev/null || true
tar czf "$OUT/sources.tgz" -C "$ROOT" data/analytics/sources 2>/dev/null || true

# 5. Manifest + checksums.
( cd "$OUT" && sha256sum -- * > SHA256SUMS )
{
  echo "created_utc=$(date -u +%FT%TZ)"
  echo "git_sha=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "host=$(hostname)"
} > "$OUT/MANIFEST"
log "wrote $OUT"; ls -la "$OUT"

# 6. Retention: keep the newest $KEEP dated directories.
mapfile -t OLD < <(ls -1d "$DEST_BASE"/????-??-?? 2>/dev/null | sort | head -n -"$KEEP")
for d in "${OLD[@]:-}"; do
  [[ -n "$d" ]] && { log "prune $d"; rm -rf -- "$d"; }
done

# 7. Off-box copy (owner-provided rsync target). Skipped when unset.
if [[ "$OFFBOX" -eq 1 && -n "${JARVISE_BACKUP_DEST:-}" ]]; then
  log "rsync → $JARVISE_BACKUP_DEST"
  rsync -a --delete "$DEST_BASE/" "$JARVISE_BACKUP_DEST/"
else
  log "off-box copy skipped (JARVISE_BACKUP_DEST unset or --no-offbox)"
fi
log "done"
