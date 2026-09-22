# jarvise

Paper analytics for an intelligent crypto trader. **No order placement** in this phase.

Repository: https://github.com/chatoe-norm/jarvise

How to run the shipped product day-to-day (CLI, VPS schedules, agent/MCP): [docs/product-usage.md](docs/product-usage.md).

## Quick start (Phase 1 — ingest + paper analyze)

One Typer entrypoint: `jarvise` (`status` / `init` / `config` / `ingest` / `analyze`). Paper ingest and analyze are delegated from that CLI; `jarvise-ingest` / `jarvise-analyze` remain compatibility aliases.

```powershell
# Python 3.11+ (repo uses 3.12 locally)
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

.venv\Scripts\jarvise ingest --symbol BTCUSDT --timeframe 1h --limit 50 --skip-derivatives --json
.venv\Scripts\jarvise analyze --symbol BTCUSDT --timeframe 4h --json
```

On macOS/Linux, use `.venv/bin/pip` and `.venv/bin/jarvise` instead.

With CoinGlass:

```powershell
$env:COINGLASS_API_KEY="..."
.venv\Scripts\jarvise ingest --symbol BTCUSDT --timeframe 1h --json
```

SQLite DB: `data/analytics/jarvise.db` (gitignored). Doctrine extract: `data/analytics/`.

On Windows, prefer `.venv\Scripts\jarvise` over bash `bin/jarvise status` — the bash shim and Typer CLI use separate state files.

## Background MCP plane (Phase 2)

```bash
cp .env.example .env   # set N8N_BASIC_AUTH_PASSWORD, etc.
docker compose up -d
docker compose ps

# Doctrine RAG into Qdrant
.venv/bin/pip install -e '.[rag]'
.venv/bin/python scripts/rag_index_doctrine.py --query "expectancy FLAT"

# Host cron / n8n helper (writes Redis jarvise:ingest:last)
chmod +x infra/n8n/run-ingest-host.sh
./infra/n8n/run-ingest-host.sh
```

Import `infra/n8n/workflows/jarvise-ingest-schedule.json` in the n8n UI (http://localhost:5678). Wire Redis credentials to `redis://redis:6379`. Prefer the **host** helper if the n8n container cannot run the local venv.

Cursor MCP example (paper-only, OpenClaw execution tools disabled): [mcp/jarvise-mcp.json.example](mcp/jarvise-mcp.json.example).

## Gemini Notebook

Trading doctrine is in notebook **Jarvise : Crypto Trader** (`14e11c63-e2ee-4b49-898f-b0cc4c61cb4e`). Cursor MCP `gemini-notebook-mcp` is already registered. Alias: `jarvise`. Google account: `chatoe@gmail.com` (`nlm` profile `chatoe`).

IDs live in [`config/notebook.json`](config/notebook.json). Agent workflow is [`.cursor/skills/jarvise-notebook/SKILL.md`](.cursor/skills/jarvise-notebook/SKILL.md).

```bash
nlm login switch chatoe
nlm login --profile chatoe
nlm notebook get jarvise
```

Sessions last about 20 minutes. If MCP calls fail with auth errors, run `nlm login --profile chatoe` then MCP `refresh_auth`. Do not use the `default` nlm profile for this notebook.

## Local workspace CLI

Agent-friendly Typer CLI under `src/jarvise/`. Commands: `status`, `init`, `config`, `ingest`, and `rag` (NotebookLM/fetch/Firecrawl → Qdrant). `jarvise-ingest` is still installed as a compatibility alias. Every command takes flags, prints copy-pasteable examples on `--help`, and treats a second successful run as a no-op.

```powershell
.venv\Scripts\jarvise --help
.venv\Scripts\jarvise status --output json
.venv\Scripts\jarvise init --yes
.venv\Scripts\jarvise init --dry-run
.venv\Scripts\jarvise config set --key name --value jarvise
.venv\Scripts\jarvise config get --key name --output json
Get-Content config.json | .venv\Scripts\jarvise config import --stdin
.venv\Scripts\jarvise rag refresh --dry-run --output json
```

Config lives in `.jarvise/config.json` under `--path` (default: the current directory). `init` and `config set` accept `--dry-run`. Missing flags exit immediately with an example invocation.

## Hostinger VPS (Tailscale + OpenRouter)

24/7 Docker stack: Redis, Qdrant, n8n, OpenClaw (OpenRouter), worker, control web. **Private Tailscale mesh only** — no public Redis/Qdrant.

See [docs/deploy/hostinger-vps.md](docs/deploy/hostinger-vps.md) and [docs/deploy/ops.md](docs/deploy/ops.md).

```bash
cp .env.example .env   # OPENROUTER_API_KEY, N8N_BASIC_AUTH_PASSWORD, TAILSCALE_IP
export TAILSCALE_IP=$(tailscale ip -4)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Trusted RAG allowlist: [config/rag-sources.json](config/rag-sources.json).

```powershell
.venv\Scripts\python -m pytest
```

## Ask the repo (optional)

Secondary npm helper for ad-hoc repo Q&A; the Python CLI above is the primary local interface. Requires Node ≥22.13.

`scripts/ask-repo.ts` runs a local Cursor agent against this working tree. Set `CURSOR_API_KEY` first (user key or team service-account key from https://cursor.com/dashboard/cloud-agents). Copy `.env.example` to `.env` or export the variable in your shell. Do not commit the key.

```bash
npm install
npm run ask-repo -- "summarize the top-level layout and the main entrypoints"
```

Exit codes: `0` finished, `1` startup or missing config, `2` the agent ran and failed or was cancelled.

## Guardrails

- Deployment ladder: paper → manual approval → autonomy
- Ingest HTTP is **GET-only** market data
- OpenClaw MCP: research/signal context only until manual-approval phase
- OpenClaw paper notes: `data/openclaw/exports` → `jarvise rag sync-openclaw` → Qdrant `kind=openclaw` (plugin: `plugins/jarvise-openclaw`)

## Specs

- [docs/product-usage.md](docs/product-usage.md) — product invoke paths (paper phase)
- [docs/superpowers/specs/2026-09-23-product-roadmap-design.md](docs/superpowers/specs/2026-09-23-product-roadmap-design.md) — full product roadmap (P0–P5)
- [docs/superpowers/plans/2026-09-23-p1-analytics-ui.md](docs/superpowers/plans/2026-09-23-p1-analytics-ui.md) — P1 Analytics UI implementation
- [docs/superpowers/specs/2026-09-21-paper-ingest-design.md](docs/superpowers/specs/2026-09-21-paper-ingest-design.md)
- [docs/superpowers/specs/2026-09-22-openclaw-intelligence-audit.md](docs/superpowers/specs/2026-09-22-openclaw-intelligence-audit.md)
