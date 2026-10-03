#!/bin/bash
# Rebuild the paper stack from origin/main. Run as root on the VPS (self-hosted runner → sudo).
# Guards: refuse unsafe .env (default n8n password, missing Tailscale IP); fix bind-mount
# ownership for the non-root container user; print image digests for rollback.
set -euo pipefail
cd /opt/jarvise

env_get() { awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,""); print; exit}' .env; }
die() { echo "deploy refused: $*" >&2; exit 2; }

[[ -f .env ]] || die ".env missing"
tailscale_ip="$(env_get TAILSCALE_IP)"
[[ -n "$tailscale_ip" ]] || die "TAILSCALE_IP is empty in .env (control plane must bind to Tailscale)"
n8n_pw="$(env_get N8N_BASIC_AUTH_PASSWORD)"
[[ -n "$n8n_pw" && "$n8n_pw" != "change-me" ]] || die "N8N_BASIC_AUTH_PASSWORD is unset or still 'change-me'"
if [[ "$(env_get JARVISE_LIVE_TRADING | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
  [[ -n "$(env_get BINANCE_TRADE_API_KEY)" ]] || die "JARVISE_LIVE_TRADING=true but BINANCE_TRADE_API_KEY is empty"
  echo "WARNING: JARVISE_LIVE_TRADING=true — live spot submit on Approve is enabled" >&2
fi

git fetch origin main
git checkout -f -B main FETCH_HEAD

# Containers run as a non-root user (Dockerfile ARG JARVISE_UID/GID, default 1000). Bind mounts
# they must write (SQLite, HF cache, nlm session, OpenClaw notes) are chowned to that uid; secrets
# are made readable by it and nobody else. Co-tenant stacks under /opt/t4trip* are untouched.
uid="$(env_get JARVISE_UID)"; uid="${uid:-1000}"
gid="$(env_get JARVISE_GID)"; gid="${gid:-1000}"
mkdir -p data/analytics data/openclaw data/nlm data/.cache secrets
chown -R "$uid:$gid" data
if [[ -n "$(ls -A secrets 2>/dev/null)" ]]; then
  chown -R "$uid:$gid" secrets
  find secrets -type d -exec chmod 0500 {} +
  find secrets -type f -exec chmod 0400 {} +
fi

export JARVISE_UID="$uid" JARVISE_GID="$gid"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

echo "image digests (for rollback via docker tag / compose image pin):"
docker compose -f docker-compose.yml -f docker-compose.prod.yml images 2>/dev/null || true
for svc in jobs web; do
  cid="$(docker compose -f docker-compose.yml -f docker-compose.prod.yml ps -q "$svc" 2>/dev/null || true)"
  [[ -n "$cid" ]] && docker inspect --format "  $svc {{.Image}} {{.Config.User}}" "$cid" || true
done

curl -fsS "http://${tailscale_ip}:8080/healthz"
printf '\n'
