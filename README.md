# jarvise

Paper analytics for an intelligent crypto trader. **No order placement** in this phase.

Repository: https://github.com/chatoe-norm/jarvise

## Quick start (Phase 1 — ingest)

```bash
# Python 3.11+ (repo uses 3.12 locally)
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'

./bin/jarvise ingest --symbol BTCUSDT --timeframe 1h --limit 50 --skip-derivatives --json
```

With CoinGlass:

```bash
export COINGLASS_API_KEY=...
./bin/jarvise ingest --symbol BTCUSDT --timeframe 1h --json
```

SQLite DB: `data/analytics/jarvise.db` (gitignored). Doctrine extract: `data/analytics/`.

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

## Guardrails

- Deployment ladder: paper → manual approval → autonomy
- Ingest HTTP is **GET-only** market data
- OpenClaw MCP: research/signal context only until manual-approval phase

## Specs

- [docs/superpowers/specs/2026-09-21-paper-ingest-design.md](docs/superpowers/specs/2026-09-21-paper-ingest-design.md)
