#!/bin/bash
# Rebuild the paper stack from origin/main. Run as root on the VPS.
set -euo pipefail
cd /opt/jarvise
git fetch origin main
if ! git ls-files --error-unmatch infra/deploy/vps-deploy.sh >/dev/null 2>&1; then
  rm -f infra/deploy/vps-deploy.sh
fi
git checkout -B main FETCH_HEAD
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
tailscale_ip="$(awk -F= '/^TAILSCALE_IP=/{print $2; exit}' .env)"
curl -fsS "http://${tailscale_ip}:8080/healthz"
printf '\n'
