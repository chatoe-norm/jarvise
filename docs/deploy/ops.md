# Jarvise VPS ops (Docker + Tailscale)

Paper-only. Companion to [hostinger-vps.md](hostinger-vps.md).

## Health

```bash
export TAILSCALE_IP=$(tailscale ip -4)
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl -fsS "http://${TAILSCALE_IP}:8080/healthz"
curl -fsS -u "$N8N_BASIC_AUTH_USER:$N8N_BASIC_AUTH_PASSWORD" "http://${TAILSCALE_IP}:5678/healthz" || true
```

All long-running services use `restart: unless-stopped`.

## Logs

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f --tail=200 web openclaw n8n
```

Configure Docker log rotation on the VPS (`/etc/docker/daemon.json`):

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
```

Then `systemctl restart docker`.

## Backups

Volumes: `jarvise_redis`, `jarvise_qdrant`, `jarvise_n8n`, plus bind mounts `data/analytics/`, `data/openclaw/`.

```bash
# Example: snapshot Qdrant + analytics to a dated tarball
BACKUP_DIR=~/jarvise-backups/$(date +%F)
mkdir -p "$BACKUP_DIR"
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop qdrant
docker run --rm -v jarvise_qdrant:/qdrant/storage -v "$BACKUP_DIR":/backup alpine \
  tar czf /backup/qdrant.tgz -C /qdrant/storage .
docker compose -f docker-compose.yml -f docker-compose.prod.yml start qdrant
tar czf "$BACKUP_DIR/analytics.tgz" -C ~/jarvise data/analytics
tar czf "$BACKUP_DIR/openclaw.tgz" -C ~/jarvise data/openclaw
```

Restore by extracting into the same volume/bind paths, then `up -d`.

## Update path

A push to `main` deploys after CI. The self-hosted runner lives at `/opt/actions-runner` and calls `/opt/jarvise/infra/deploy/vps-deploy.sh`. It does not listen on a public port.

Manual fallback:

```bash
sudo /opt/jarvise/infra/deploy/vps-deploy.sh
```

## Kill switch

Control UI → Engage kill switch, or:

```bash
docker compose exec redis redis-cli set jarvise:kill_switch 1
```

`jarvise rag refresh` exits with code 3 when engaged.

## Verify Tailscale path (laptop)

```bash
tailscale ping <vps-magicdns-or-ip>
curl -fsS "http://<vps-ip>:8080/healthz"
```

Open web `:8080`, n8n `:5678`, OpenClaw `:18789` only over Tailscale.
