# Jarvise Mac handoff — 2026-10-02

Continue from Windows session. Paper-first; live stays off.

## Repo / GitHub

| Item | Value |
|------|--------|
| Remote | `chatoe-norm/jarvise` via SSH host `github.com-chatoe-norm` |
| Branch | `main` @ **`5669079`** (synced with `origin/main`) |
| `gh` account | **`chatoe-norm`** only (not `normstudiox`) |
| Recent PRs | [#42](https://github.com/chatoe-norm/jarvise/pull/42) Command Dashboard SPA · [#43](https://github.com/chatoe-norm/jarvise/pull/43) Decisions pagination |

```bash
git clone git@github.com-chatoe-norm:chatoe-norm/jarvise.git
cd jarvise && git checkout main && git pull
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # or match pyproject extras used locally
cd web && npm install && npm run build   # SPA for Docker / local UI
```

Untracked local junk on Windows (do **not** need on Mac): `.tmp-vps-*.sh`, `.cursor/hooks/state/*`, `mcp/jarvise-mcp.json` (use `mcp/jarvise-mcp.json.example`).

## VPS (runtime)

| Item | Value |
|------|--------|
| Host | Hostinger KVM `srv1269762.hstgr.cloud` · Tailscale **`100.93.110.48`** |
| Stack | `/opt/jarvise` · SSH `root` over Tailscale |
| Deploy tip when checked | **`5669079`** · `web` healthy |
| Command Dashboard | http://100.93.110.48:8080/ |
| Health | http://100.93.110.48:8080/healthz |
| n8n | `:5678` (Tailscale) · OpenClaw `:18789` |

**Deploy note:** full `docker compose … up -d --build` via Actions can hang on buildx bake. Prefer:

```bash
ssh root@100.93.110.48
cd /opt/jarvise
git fetch origin main && git checkout -f -B main FETCH_HEAD
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --force-recreate web
# only rebuild other services when needed
```

Script: [`infra/deploy/vps-deploy.sh`](../infra/deploy/vps-deploy.sh) (full stack). CI Deploy workflow may still run after merge — cancel and web-only recreate if bake stalls.

## What just shipped

1. **Command Dashboard** (Hybrid FastAPI + Vite/React under `web/`)
   - Routes: `/` Home (approvals-first) · `/paper` · `/decisions` · `/exchange` · `/ops`
   - Spec: [`docs/superpowers/specs/2026-10-01-command-dashboard-design.md`](../superpowers/specs/2026-10-01-command-dashboard-design.md)
2. **Decisions pagination** — default 10 rows; sizes 10/15/20/50/100; `/api/analysis?limit=&offset=` returns `total`
3. **Market safety + free Binance Futures derivatives** (earlier PRs) — CoinGlass optional when keyed

## Paper readiness (last VPS check)

| Check | State |
|-------|--------|
| `paper_only` / live | **true** / **false** |
| Kill switch | **clear** |
| Ingest + market_safety | ok; BTC/ETH not force FLAT |
| Paper job | last enqueue ok |
| Ledger | equity/cash **10000**, **0** open positions, **0** closed trades |
| Pending approvals | **2** (BTCUSDT + ETHUSDT **FLAT**, regime `chaotic`, conf ~0.1) |

**Ready to practice Approve/Reject on Home.** Approving current FLAT queue will not meaningfully open paper risk. For a real paper long/short, wait for a non-FLAT enqueue or run analyze/paper manually.

### Owner paper loop

1. Open http://100.93.110.48:8080/ → **Home** → Approve/Reject  
2. Or CLI on VPS/worker: `jarvise paper approve <id>` / `reject`  
3. Schedules (n8n → jobs): ingest ~15m · paper enqueue ~4h · expire ~1h  
4. Docs: [`docs/product-usage.md`](../product-usage.md)

**Do not** set `JARVISE_LIVE_TRADING=true` without [`docs/ops/live-enable-checklist.md`](../ops/live-enable-checklist.md).

## Local Mac day-to-day

```bash
# CLI (from repo root, venv on)
.venv/bin/jarvise --help
.venv/bin/jarvise paper status --json

# UI against local API (API must be on :8080) or proxy:
cd web && npm run dev   # Vite :5173 → proxies /api to :8080
```

Secrets stay in VPS `.env` / local `.env` (never commit). NotebookLM: `nlm` profile **`chatoe`**; notebook alias `jarvise` in `config/notebook.json`.

## Durable preferences (AGENTS.md)

- Paper-first; capital preservation; live gated  
- Free-first market-safety providers (Binance book / Futures derivatives / CoinGecko; CoinGlass when keyed)  
- Command Dashboard: approval-first, charts/KPIs — not raw JSON dumps  
- Prefer Auto/Composer for cost  

## Suggested next on Mac

1. Pull `main`, open Dashboard over Tailscale, Approve/Reject or wait for non-FLAT paper  
2. Optional: smoke `pytest tests/test_web.py tests/test_db_upsert.py` + `cd web && npm run build`  
3. If UI changes: PR → merge → **web-only** VPS recreate  
4. Do **not** start P5 / live until gated P4-C path is intentionally enabled  

## Quick URLs

- Dashboard: http://100.93.110.48:8080/  
- Decisions: http://100.93.110.48:8080/decisions  
- Ops / kill: http://100.93.110.48:8080/ops  
- Repo: https://github.com/chatoe-norm/jarvise  
