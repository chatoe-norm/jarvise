# OpenClaw ↔ intelligence audit

**Date:** 2026-09-22  
**Status:** Phase 2 (drop-folder sync) + Phase 3 (native plugin) shipped; remaining items are ops caveats only  
**Paper-only:** research/signal context — no order placement

## What exists today

| Piece | Path / surface | Role |
|-------|----------------|------|
| OpenClaw container | `docker-compose.yml` service `openclaw` | Image `ghcr.io/openclaw/openclaw:2026.9.5`, port `18789`, Tailscale-only in prod |
| Seed config | `config/openclaw/openclaw.json.example` → `data/openclaw/openclaw.json` via `infra/docker/seed-openclaw-config.sh` | OpenRouter model `openrouter/openrouter/auto`, paper-only env vars, `plugins.load.paths` |
| Native plugin | `plugins/jarvise-openclaw/` | Skills via `skills.load.extraDirs` (not empty-extension `plugins.load`) |
| Drop-folder sync | `jarvise rag sync-openclaw` / `jarvise rag refresh` | Exports → `sources/openclaw/` → `kind=openclaw` |
| MCP example | `mcp/jarvise-mcp.json.example` | `openclaw mcp serve --url ws://127.0.0.1:18789` |
| Guardrails | compose + example `_jarvise` | `OPENCLAW_PAPER_ONLY=true`, `JARVISE_PAPER_ONLY=true`, no live execution tools |

OpenClaw receives `QDRANT_URL` and `REDIS_URL` on the Docker network. Application code does not call OpenClaw as an RPC client; research notes enter doctrine RAG only through the filesystem drop-folder sync.

## What “intelligence” means in Jarvise

Three adjacent layers (not one API):

1. **Doctrine RAG** — `jarvise rag` → local `data/analytics/sources/` → Qdrant collection `jarvise_doctrine` (`src/jarvise/rag.py`)
2. **Paper market ingest** — `jarvise ingest` → SQLite `data/analytics/jarvise.db` (numbers, not RAG)
3. **Live Gemini Notebook** — Cursor / `nlm` notebook query (doctrine at query time; parallel to RAG)

Agents and the control web use Redis status keys (`jarvise:rag:*`, `jarvise:ingest:last`) and Qdrant point counts. There is no package named `intelligence`.

## OpenClaw model (2026 docs)

Aligned with [docs.openclaw.ai](https://docs.openclaw.ai/):

- **Gateway** — single process (Control UI on `:18789`) for sessions, routing, and channel connections.
- **State** — host `data/openclaw/` → `/home/node/.openclaw` (`openclaw.json`, credentials, per-agent SQLite). This is **not** the agent workspace.
- **Workspace** — separate persona/bootstrap tree (`AGENTS.md`, `SOUL.md`, …). Optional for Jarvise paper path; not required to sync exports into RAG.
- **Plugin skills** — Jarvise ships skills under `plugins/jarvise-openclaw/skills/` and loads them with `skills.load.extraDirs` (extraDirs tier). Do not register this pack via `plugins.load.paths` unless it has a real `openclaw.extensions` entry; do not add undocumented root keys like `_jarvise` to `openclaw.json`.

```text
OpenClaw (research notes)
  → data/openclaw/exports/*.md
  → jarvise rag sync-openclaw
  → data/analytics/sources/openclaw/
  → jarvise rag index / refresh
  → Qdrant jarvise_doctrine (kind=openclaw)
```

## Remaining gaps (ops caveats)

1. **`OPENCLAW_PAPER_ONLY` is a convention** — documented in env/skills/config; not enforced in Python beyond docs/env.
2. **MCP is operator-configured** — copy `mcp/jarvise-mcp.json.example` into Cursor MCP settings; gateway must be up and token set when required.
3. **Deploy does not seed/onboard** — `vps-deploy.sh` only compose up; first boot needs hostinger-vps one-time steps.
4. **Config mount vs seed path** — compose mounts `./config/openclaw` at `/config:ro`, but seed writes runtime config to `data/openclaw/openclaw.json` → `/home/node/.openclaw`. Upgrade in place must merge `plugins` from the example.
5. **n8n RAG schedule inactive** until NotebookLM `nlm` auth on the VPS (separate from OpenClaw).
6. **No tests/CI** covering OpenClaw or openclaw-kind indexing.

## Non-goals

- Live or paper order execution from OpenClaw
- Embedding SQLite candles into Qdrant
- Building `jarvise analyze`
- Auto-activating n8n RAG without `nlm` credentials
- Implementing a full custom OpenClaw MCP server beyond documented `openclaw mcp serve` / gateway bridge

## Shipped (Phase 2 + 3)

| Item | Location |
|------|----------|
| Audit | this file |
| Drop-folder sync | `jarvise rag sync-openclaw` / included in `jarvise rag refresh` |
| Kind tag | `openclaw` in `src/jarvise/rag.py` `SOURCE_KINDS` |
| Native plugin | `plugins/jarvise-openclaw/` (`openclaw.plugin.json` + skills; load via `skills.load.extraDirs`) |
| Compose mount | `./plugins/jarvise-openclaw:/plugins/jarvise-openclaw:ro` |
| Seed example | `config/openclaw/openclaw.json.example` → `skills.load.extraDirs` |
| MCP | `mcp/jarvise-mcp.json.example` → `openclaw mcp serve --url ws://127.0.0.1:18789` |
| Healthcheck | HTTP probe of gateway `:18789` |

### Verify plugin → RAG

1. `bash infra/docker/seed-openclaw-config.sh` (fresh install) or merge `skills.load.extraDirs` from the example into `data/openclaw/openclaw.json`.
2. `docker compose up -d openclaw` — Control UI on Tailscale `:18789` with gateway token; `openclaw skills list` shows Jarvise skills.
3. Write a note under `data/openclaw/exports/` (or via skill `jarvise-paper-research`).
4. `jarvise rag sync-openclaw` then `jarvise rag index` (or `refresh`).
5. Search Qdrant for payload `kind=openclaw`.
