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

Volumes (compose project `jarvise` prefixes them on disk): `jarvise_jarvise_redis`, `jarvise_jarvise_qdrant`, `jarvise_jarvise_n8n`, plus bind mounts `data/analytics/`, `data/openclaw/`. The scripts resolve the real names by label — never pass `jarvise_qdrant` to `docker run -v` by hand; that creates an empty look-alike volume (two such leftovers from 2026-09-22 exist: `jarvise_qdrant`, `jarvise_redis`; safe to `docker volume rm` after confirming they are empty).

Scripted (preferred): [`infra/backup/backup.sh`](../../infra/backup/backup.sh) nightly via root cron → `/opt/jarvise/backups/YYYY-MM-DD/` (SQLite online `.backup` + integrity check, Qdrant/Redis volume tars, checksums, 14-day retention, optional `rsync` to `JARVISE_BACKUP_DEST`). Restore and verify with [`infra/backup/restore.sh`](../../infra/backup/restore.sh). Full procedure, RPO/RTO, and the post-restore smoke list: [disaster-recovery.md](../ops/disaster-recovery.md).

```bash
cd /opt/jarvise
bash infra/backup/backup.sh                                   # take one now
bash infra/backup/restore.sh --from backups/$(date -u +%F) --verify
```

## Update path

A push to `main` deploys after CI. The self-hosted runner lives at `/opt/actions-runner` and calls `/opt/jarvise/infra/deploy/vps-deploy.sh`. It does not listen on a public port.

Manual fallback:

```bash
sudo /opt/jarvise/infra/deploy/vps-deploy.sh
```

### Dependency / image upgrades

Before the first pull that bumps **Qdrant** (e.g. `v1.13.x` → `v1.19.x`) or **Redis** patch pins, take a volume backup (see [Backups](#backups)). Also snapshot Redis:

```bash
cd /opt/jarvise && bash infra/backup/backup.sh --no-offbox   # SQLite + Qdrant + Redis + OpenClaw state, checksummed
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

`jarvise rag refresh` / paper-run / paper-expire exit with code 3 (HTTP 409 from jobs) when engaged.

Redis status keys (JSON via jobs → `publish_redis_status`):

| Key | Writer |
|-----|--------|
| `jarvise:ingest:last` | `POST /jobs/ingest` |
| `jarvise:rag:last` | `POST /jobs/rag-refresh` |
| `jarvise:paper:last` | `POST /jobs/paper-run` |
| `jarvise:paper:expire:last` | `POST /jobs/paper-expire` |
| `jarvise:paper:digest:last` | `POST /jobs/paper-pending-digest` |
| `jarvise:paper_auto:last` | `POST /jobs/paper-auto-decide` |
| `jarvise:ingest:health` | `POST /jobs/ingest-health` |
| `jarvise:live:reconcile:last` | `POST /jobs/live-reconcile` (read-only) |
| `jarvise:kill_switch` (+ `:reason`) | control UI / `engage_kill_switch` / redis-cli — **reads fail closed**: web approve and jobs treat an unreadable switch as engaged |
| `jarvise:lock:<job>` | single-flight `SET NX EX` held while a job runs (ingest, paper_run, paper_expire, rag, paper_pending_digest, paper_auto_decide, live_reconcile); a second trigger gets HTTP 409 `<job>_running` |
| `jarvise:circuit:<provider>:*` | provider circuit breaker (5 consecutive exhausted GET failures → skip 15 min; shows as `circuit_open` in ingest `provider_errors`) |

Job subprocesses have wall-clock limits (`JARVISE_JOB_TIMEOUT_*_S`; defaults ingest 900s, paper-run 300s, expire 120s, rag 900s). A timeout returns exit code 124, publishes `error: timeout after Ns`, and sends a Telegram job-failure alert.

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
