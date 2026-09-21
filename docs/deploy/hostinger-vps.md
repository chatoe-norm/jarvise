# Jarvise on Hostinger VPS (Docker + Tailscale + OpenRouter)

Paper-only stack. No public ports for Redis/Qdrant. Control UIs bind to the Tailscale IP only.

## This VPS

| Item | Value |
|------|--------|
| Provider | Hostinger KVM 2 |
| KVM ID | 1269762 |
| Hostname | `srv1269762.hstgr.cloud` |
| Location | Malaysia (Kuala Lumpur) |
| OS | Ubuntu 24.04 LTS |
| CPU / RAM / disk | 2 cores / 8 GB / 100 GB |
| Bandwidth | 8 TB |
| SSH | `root@72.62.244.54` |
| Backup | Weekly |
| Plan expires | 2027-01-14 (auto-renew on) |

8 GB RAM is enough for OpenClaw plus local embeddings. Control ports stay on Tailscale. Do not publish them on `72.62.244.54`.

## Requirements

| Item | Minimum |
|------|---------|
| VPS | Hostinger KVM / VPS, Ubuntu 22.04+ |
| RAM | **4 GB** (OpenClaw + embeddings) |
| CPU | 2 vCPU |
| Disk | 40 GB+ |
| Access | Tailscale account; OpenRouter API key |

## 1. Provision VPS

This host is already provisioned (KVM 1269762). SSH in as root, then install Docker Engine + Compose v2:

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker "$USER"
# re-login after usermod
docker compose version
```

## 2. Tailscale (private mesh)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh
tailscale ip -4   # note this IPv4
```

On your laptop: install Tailscale, same account, enable MagicDNS if desired.

**Do not** open Hostinger firewall ports 5678 / 8080 / 18789 / 6333 / 6379 to the public internet.

## 3. Clone and configure

```bash
git clone git@github.com-chatoe-norm:chatoe-norm/jarvise.git
# or: git clone https://github.com/chatoe-norm/jarvise.git
cd jarvise
cp .env.example .env
```

Edit `.env`:

- `OPENROUTER_API_KEY=sk-or-...`
- `N8N_BASIC_AUTH_PASSWORD=` strong password
- `FIRECRAWL_API_KEY=` if using Firecrawl RAG ingest
- `WEB_BASIC_AUTH_USER` / `WEB_BASIC_AUTH_PASSWORD` optional second factor
- `TAILSCALE_IP=` output of `tailscale ip -4`

## 4. Start the stack

```bash
export TAILSCALE_IP=$(tailscale ip -4)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

From your laptop (on Tailscale):

| Service | URL |
|---------|-----|
| Control web | `http://$TAILSCALE_IP:8080/` |
| n8n | `http://$TAILSCALE_IP:5678/` |
| OpenClaw | `http://$TAILSCALE_IP:18789/` |

## 5. OpenClaw + OpenRouter (one-time)

```bash
bash infra/docker/seed-openclaw-config.sh
docker compose exec openclaw openclaw onboard --auth-choice openrouter-api-key
# or set OPENROUTER_API_KEY in .env and edit data/openclaw/openclaw.json
```

Ensure primary model is `openrouter/openrouter/auto`. Keep `OPENCLAW_PAPER_ONLY=true` — research/signal only, **no order placement tools**.

## 6. NotebookLM credentials (RAG sync)

On the VPS, authenticate `nlm` once and mount credentials:

```bash
# Prefer running nlm login on the host, credentials under data/nlm
mkdir -p data/nlm
# Install nlm CLI, then:
nlm login --profile chatoe
```

Worker mounts `./data/nlm` → `/root/.nlm`. Then:

```bash
docker compose --profile tools run --rm worker jarvise rag sync-notebook
docker compose --profile tools run --rm worker jarvise rag ingest-sources
docker compose --profile tools run --rm worker jarvise rag index
```

## 7. Import n8n workflows

1. Open n8n UI over Tailscale.
2. Import `infra/n8n/workflows/jarvise-ingest-schedule.json` (every 15m `POST http://jobs:8090/jobs/ingest`).
3. Import `infra/n8n/workflows/jarvise-rag-refresh.json` (every 6h `POST http://jobs:8090/jobs/rag-refresh`).
4. Redis credential: `redis://redis:6379`.

The `jobs` service stays on the Docker network only. It runs paper ingest and RAG refresh and stops both when `jarvise:kill_switch` is set.

## 8. Updates

Pushing to `main` runs CI on a GitHub-hosted runner. After CI succeeds, the self-hosted runner on this VPS (`ghrunner`, label `jarvise`) runs [infra/deploy/vps-deploy.sh](../../infra/deploy/vps-deploy.sh). The runner only makes outbound connections to GitHub. Pull requests never run on it.

Manual fallback, from the VPS:

```bash
sudo /opt/jarvise/infra/deploy/vps-deploy.sh
```

## 9. Backups

See [ops.md](ops.md) for volume backup and restore.

## Hard rules

- Paper mode only until live is explicitly gated elsewhere.
- Tailscale is the perimeter; optional web basic auth is a second factor.
- Never publish Redis or Qdrant on `0.0.0.0`.
