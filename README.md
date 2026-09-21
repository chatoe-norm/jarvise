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

## Gemini Notebook

Trading doctrine is in notebook **Jarvise : Crypto Trader** (`14e11c63-e2ee-4b49-898f-b0cc4c61cb4e`). Cursor MCP `gemini-notebook-mcp` is already registered. Alias: `jarvise`. Google account: `chatoe@gmail.com` (`nlm` profile `chatoe`).

IDs live in [`config/notebook.json`](config/notebook.json). Agent workflow is [`.cursor/skills/jarvise-notebook/SKILL.md`](.cursor/skills/jarvise-notebook/SKILL.md).

```bash
nlm login switch chatoe
nlm login --profile chatoe
nlm notebook get jarvise
```

Sessions last about 20 minutes. If MCP calls fail with auth errors, run `nlm login --profile chatoe` then MCP `refresh_auth`. Do not use the `default` nlm profile for this notebook.

## Local workspace CLI (WIP)

Agent-friendly local status/config CLI under `src/jarvise/` (separate from `jarvise-ingest`). Every command takes flags, prints copy-pasteable examples on `--help`, and treats a second successful run as a no-op.

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

On macOS or Linux, use `.venv/bin/pip install -e ".[dev]"`.

```bash
jarvise --help
jarvise status --output json
jarvise init --yes
jarvise init --dry-run
jarvise config set --key name --value jarvise
jarvise config get --key name --output json
cat config.json | jarvise config import --stdin
```

Config lives in `.jarvise/config.json` under `--path` (default: the current directory). `init` and `config set` accept `--dry-run`. Missing flags exit immediately with an example invocation.

```bash
pytest
```

## Ask the repo

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

## Specs

- [docs/superpowers/specs/2026-09-21-paper-ingest-design.md](docs/superpowers/specs/2026-09-21-paper-ingest-design.md)
