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

Ensure primary model is `openrouter/openrouter/auto`. Keep `OPENCLAW_PAPER_ONLY=true` — research/signal only, **no order placement tools**. Set `OPENCLAW_GATEWAY_TOKEN` in `.env` (compose passes it through).

Deploy does **not** auto-seed or onboard OpenClaw (keeps secrets out of CI).

### Bind layout (state vs Jarvise exports)

Per [OpenClaw docs](https://docs.openclaw.ai/concepts/agent-workspace): agent **workspace** (persona files like `AGENTS.md` / `SOUL.md`) is separate from Gateway **state**.

- Host `data/openclaw/` → container `/home/node/.openclaw` = OpenClaw **state** (`openclaw.json`, credentials, agent DB, managed skills). Not the agent workspace.
- Skills pack: compose mounts `plugins/jarvise-openclaw` → `/plugins/jarvise-openclaw`, loaded via `skills.load.extraDirs` in the seeded config (do not add undocumented root keys like `_jarvise`).
- Jarvise paper drop-folder (convention, not an OpenClaw core path): `data/openclaw/exports/*.md` → container `/home/node/.openclaw/exports/` → `jarvise rag sync-openclaw`.

### Post-seed checks

1. Open Control UI at `http://$TAILSCALE_IP:18789/` and paste the gateway token from `.env` (`OPENCLAW_GATEWAY_TOKEN`) into Settings.
2. Confirm skills inside the container:

```bash
docker compose exec openclaw openclaw skills list    # expect jarvise-paper-research, jarvise-doctrine-rag
```
3. Sync paper notes into doctrine RAG:

```bash
docker compose --profile tools run --rm worker jarvise rag sync-openclaw
# or wait for jarvise rag refresh (includes openclaw → index)
```

Then `jarvise rag index` / `refresh` tags those files as `kind=openclaw` in Qdrant `jarvise_doctrine`.

## 6. NotebookLM credentials (RAG sync)

Worker image includes `notebooklm-mcp-cli` (`nlm`). Credentials live under `data/nlm` (mounted at `/root/.nlm` via `NOTEBOOKLM_MCP_CLI_PATH`).

Prefer refreshing auth on a laptop (browser login), then syncing to the VPS:

```bash
# On laptop (after nlm login --profile chatoe is valid):
nlm login --check --profile chatoe
rsync -az --exclude chrome-profiles ~/.notebooklm-mcp-cli/profiles/chatoe/ jarvise-vps-ts:/opt/jarvise/data/nlm/profiles/chatoe/
# Ensure config default_profile = chatoe under data/nlm/config.toml
```

Or login on the VPS if a browser/CDP path is available:

```bash
mkdir -p data/nlm
# Host or one-off worker with nlm:
nlm login --profile chatoe   # stores under NOTEBOOKLM_MCP_CLI_PATH / data/nlm
```

Smoke:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tools run --rm worker \
  nlm login --check --profile chatoe
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tools run --rm worker \
  jarvise rag sync-notebook --output json
```

Then ingest/index as needed:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tools run --rm worker jarvise rag ingest-sources
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tools run --rm worker jarvise rag index
```

## 7. Schedules

n8n workflow `jarvise-ingest-schedule` posts to `http://jobs:8090/jobs/ingest` every 15 minutes. The jobs service writes `jarvise:ingest:last`. Do not also cron that same ingest.

After NotebookLM auth works, enable **both**:

1. **n8n** workflow `Jarvise RAG refresh` (every 6h → `POST http://jobs:8090/jobs/rag-refresh`)
2. **Host cron** calling [`infra/n8n/jarvise-rag-index.sh`](../../infra/n8n/jarvise-rag-index.sh) (wrapper → `run-rag-refresh.sh`) as a fallback if n8n is down — pick one primary to avoid double-refresh; prefer n8n, keep cron commented unless needed.

Import workflows once inside the n8n container:

```bash
docker exec jarvise-n8n-1 n8n import:workflow --separate --input=/workflows
```

Activate ingest + RAG in the n8n UI (`http://$TAILSCALE_IP:5678/`), or:

```bash
# Example: list then activate by id after import
docker exec jarvise-n8n-1 n8n list:workflow
# activate via UI is simplest for first enable
```

Cron fallback (optional):

```bash
# root crontab — every 6 hours
0 */6 * * * cd /opt/jarvise && bash infra/n8n/jarvise-rag-index.sh >>/var/log/jarvise-rag.log 2>&1
```

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
