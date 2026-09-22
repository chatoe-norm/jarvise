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

### Dependency / image upgrades

Before the first pull that bumps **Qdrant** (e.g. `v1.13.x` → `v1.19.x`) or **Redis** patch pins, take a volume backup (see [Backups](#backups)). Also snapshot Redis:

```bash
BACKUP_DIR=~/jarvise-backups/$(date +%F)
mkdir -p "$BACKUP_DIR"
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop redis
docker run --rm -v jarvise_redis:/data -v "$BACKUP_DIR":/backup alpine \
  tar czf /backup/redis.tgz -C /data .
docker compose -f docker-compose.yml -f docker-compose.prod.yml start redis
```

Pin OpenClaw in the VPS `.env` (do not leave `:latest`):

```bash
OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:2026.9.5
```

Then pull and recreate:

```bash
cd /opt/jarvise
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### Rollback tags

If a bump misbehaves, restore the previous image tags in `docker-compose.yml` / `.env` and `up -d` again (restore volumes from backup if storage migrated badly):

| Service | Previous pin (pre-2026-09-22 upgrade) |
|---------|----------------------------------------|
| Redis | `redis:7-alpine` |
| Qdrant | `qdrant/qdrant:v1.13.4` |
| n8n | `n8nio/n8n:2.39.10` |
| OpenClaw | `ghcr.io/openclaw/openclaw:2026.9.5` (match `.env` / compose pin; never leave `:latest`) |
| Python base | `python:3.12-slim-bookworm` |

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

## OpenClaw plugin + RAG sync

- **State mount:** `data/openclaw/` → `/home/node/.openclaw` (config, credentials, sessions). Backup already covers this tree. Jarvise paper path does **not** require seeding OpenClaw workspace persona files (`AGENTS.md` / `SOUL.md`); those are optional agent workspace, not state.
- **Skills pack:** compose mounts `plugins/jarvise-openclaw` → `/plugins/jarvise-openclaw`. Load via `skills.load.extraDirs: ["/plugins/jarvise-openclaw/skills"]` (not `plugins.load.paths` — empty `openclaw.extensions` packages fail plugin install). Never put `_jarvise` in `openclaw.json` (Gateway rejects unknown root keys).
- **Paper notes:** `data/openclaw/exports/*.md` → `jarvise rag sync-openclaw` → `jarvise_doctrine` (`kind=openclaw`).
- **First boot:** re-seed from `config/openclaw/openclaw.json.example` only when `data/openclaw/openclaw.json` is missing. **Upgrade in place:** merge `skills.load.extraDirs` from the example into the live config.
- **After editing mounted skills:** restart the gateway (`docker compose restart openclaw`) or start a new chat session (`/new`) so skills reload.
- **Verify:** `docker compose exec openclaw openclaw skills list` (expect `jarvise-paper-research`, `jarvise-doctrine-rag`). Control UI is on Tailscale `:18789` when `OPENCLAW_GATEWAY_BIND=lan`.
- **Cursor MCP:** `openclaw mcp serve --url ws://127.0.0.1:18789` (see `mcp/jarvise-mcp.json.example`); pass `OPENCLAW_GATEWAY_TOKEN` when the gateway requires auth.
