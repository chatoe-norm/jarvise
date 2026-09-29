# PROJECT_CONTEXT.md — Jarvise

**Purpose:** single source of truth against context drift. Read this before proposing or writing any code.
**Generated:** 2026-09-25; **status refreshed:** 2026-09-28.
**Snapshot:** branch `main` @ `def1f8f` (PR [#23](https://github.com/chatoe-norm/jarvise/pull/23) merged); P0–P3 + **P4 paper slice B** shipped and Deployed; next work = paper jobs 24/7 (design drafted, not coded).
**Maintenance rule:** update §3 (status) and §5 (next steps) whenever a roadmap phase or PR lands. Doctrine/preference changes go to `AGENTS.md` first, then here.

### Status at a glance (2026-09-28)

| Area | State |
|------|--------|
| Ladder | **P0–P3 + P4-B shipped** on `main` (`def1f8f`). **P4-C / P5** not started. |
| Live trading | **Off** — no order/trade POST code in repo. |
| Shipped UX | `/analytics`: analysis, paper ledger, exchange spot (RO), approval queue, pipeline ingest/rag. |
| Paper path | `paper run` → enqueue → Approve → paper fill; `--auto-fill` escape hatch. |
| VPS schedules | n8n: ingest ~15m, rag ~6h. **Paper run/expire not scheduled yet.** |
| Next MVP | §5.2 paper jobs 24/7 — design file exists; need accept → plan → code. |
| Docs sync | Status refresh + paper-jobs design landed via docs PR (see git history). |

---

## 0. Framing check (read first)

The request that produced this file described the project as a *"Day Trading App using Python/FastAPI for retail traders."* The codebase, roadmap, and `AGENTS.md` describe something narrower and more specific. Where they differ, **the repository wins** until the owner explicitly changes the roadmap:

- **Not day trading.** Signals are computed on **closed 1h / 4h candles** (`15m` and `1d` are also allowed intervals), with a doctrine of *slow, steady gains*, capital preservation, and FLAT-by-default. There is no intraday scalping, tick data, or order-book execution logic.
- **Not for retail traders / not multi-tenant.** Jarvise is an **owner-operated** system for one person's capital. "Public marketing site or multi-tenant SaaS" is an explicit roadmap non-goal. The UI is private (Tailscale-only) and has no accounts/signup.
- **Python: yes. FastAPI: partly.** The **primary interface is a Typer/argparse CLI** (`jarvise ...`). FastAPI powers the private control/analytics web (`src/jarvise_web/app.py`) and is an optional extra (`pip install -e '.[web]'`). A stdlib `http.server` powers the internal jobs API.
- **Crypto first, stocks/ETFs later.** Only crypto (BTCUSDT, ETHUSDT via Binance public klines) is wired; stocks/ETFs are post-MVP per roadmap §7.

If the owner *wants* to pivot to a retail day-trading product, that is a roadmap change and must be written into `docs/superpowers/specs/2026-09-23-product-roadmap-design.md` and `AGENTS.md` before any code moves.

---

## 1. Core Objective

**Jarvise is an owner-protective, paper-first crypto trading system that climbs a fixed ladder — paper → manual approval → autonomy — and never skips a rung.**

End-state product (from `AGENTS.md` and the roadmap):

1. **Analytics UI** — signals, paper portfolio, risk, exchange balances at a glance (private, Tailscale-bound).
2. **Exchange accounts** — venue-agnostic; Binance global spot is the first adapter (read-only today; gated orders later). Not locked to Binance or Binance TH; any venue that is efficient, stable, and secure may be added.
3. **Auto-trade** — only after manual approval has run stably in production, behind an explicit flag that defaults **off**, with kill-switch, hard caps, and drawdown lock.

### Doctrine (non-negotiable, from the Gemini notebook "Jarvise : Crypto Trader")

- Capital preservation beats activity; prefer **FLAT** over a low-confidence guess.
- Structure first (trend/range, swing levels); oscillators are **context, never standalone triggers**.
- Every trade has an **invalidation** (stop) no closer than **1.5× ATR**; hit → FLAT; no revenge adds.
- Size a small fixed fraction of equity (0.25%–1% start); **never full Kelly**.
- **Expectancy must be clearly positive on paper results (after fees/slippage) before anything goes live.**
- **Drawdown halt / kill-switch**: lock the book on a preset loss; resume only after a written human review, never automatically.
- Exchange keys: read + spot-trade only (if/when live); **never withdrawal/transfer permissions**.
- Gemini Notebook is **doctrine, not a price feed**. TradingView MCP is a **research overlay**, never a fill source.
- Numeric truth lives in **SQLite** (`data/analytics/jarvise.db`); indicators are recomputed from the **full stored series** (closed candles, warm-up withheld). Obsidian/markdown are not data stores.

### Explicit non-goals

Multi-tenant SaaS; mobile apps; public HTTPS UI (Tailscale-only for now); Binance-TH-only orientation; live orders before P4 exit criteria; OpenClaw or agents placing orders; installing Binance Skills Hub / `binance-cli` / Binance MCP (execution tooling).

---

## 2. Current MVP Scope

**Definition used here:** *MVP = roadmap phases P0 through P4 complete*, i.e. the owner can run ingest → analyze → paper ledger 24/7, review a queue of candidates in the private UI, and on **Approve** get a **size-capped spot order under hard caps**, with kill-switch and audit. **P5 autonomy is post-MVP** and requires stable P4 in production plus explicit owner judgment.

### 2.1 Core Technical Analysis (TA) features — minimum

- Reproducible OHLCV store: closed candles only, paged backfill (`--since/--until`), gap reporting, point-in-time universe (`paper_core`).
- Indicators recomputed from the full series with warm-up withholding: **ATR-14, RSI-14, EMA-20, EMA-200**.
- Deterministic analyzer per closed candle → `analysis_output`: **regime** (`trend_up` / `trend_down` / `range` / `chaotic`), **action** (`long` / `short` / `flat`), **confidence** (FLAT below **0.55**), **invalidation** (close ∓ 1.5×ATR), **size % equity** (`min(2.0, 1.5 × confidence)`).
- Derivatives context (open interest, funding, liquidations) stored bitemporally (revisions never overwrite what was known earlier).
- Doctrine RAG (NotebookLM + allowlisted fetch/Firecrawl + OpenClaw notes → Qdrant `jarvise_doctrine`) consulted before any trade call.

*Not required for MVP (post-MVP):* ADX/VWAP, multi-timeframe confirmation, volume confirmation, derivatives feeding the analyzer, order-book microstructure.

### 2.2 Trading / Execution features — minimum

- **Paper ledger** (`paper_account`, `paper_orders`, `paper_positions`): simulated fills with 5 bps fee + 5 bps adverse slippage, one position per symbol, MTM equity, starting equity $10,000.
- **Approval queue** (`approval_queue`): `paper run` enqueues by default; owner Approves/Rejects on `/analytics` or CLI; TTL (`JARVISE_APPROVAL_TIMEOUT_MIN`, default 60) → `timed_out`; one pending row per (symbol, timeframe); atomic claim; kill-switch fail-closed.
- **Kill-switch** (Redis `jarvise:kill_switch`) honored by schedules, paper run/approve, and the web toggle.
- **Exchange read-only**: Binance spot balances (`GET /api/v3/account`, HMAC or Ed25519/RSA) → `exchange_balances`, shown beside the paper ledger; secrets only in VPS `.env`; soft-fail hides the panel.
- **Performance gate**: expectancy / drawdown metrics from the paper ledger sufficient to judge "clearly positive EV" (doctrine prerequisite for live).
- **P4-C live path (last MVP rung, owner-gated)**: on Approve, size-capped live **spot** order via `jarvise_exchange` → `live_orders` audit; hard caps (max notional per order, max daily loss, drawdown lock) and kill-switch enforced *before* submit; timeout/reject → FLAT; live flag default **off**.
- 24/7 operation on the VPS: n8n → jobs API for ingest, RAG refresh, **and** paper run/expire.

### 2.3 On-chain / Sentiment features — minimum

Per doctrine these are **context that lowers/raises confidence or vetoes**, never a trigger. The roadmap defers on-chain *writers* to "later" (§7), so the MVP minimum is small:

- Derivatives context via CoinGlass (already stored) — funding/OI/liquidations as crowding context.
- Agent-side **Binance Web3 intel** (`jarvise-binance-intel` skill, no keys): token search/meta, **security audit** (audit `HIGH`, `riskType: RISK`, or sell tax → **FLAT veto**), market rank, social hype, smart-money inflow, tokenized US stocks info, Academy risk education.
- Doctrine RAG + OpenClaw research notes as the "sentiment/narrative" layer.

*Post-MVP (schema already reserved, no writers):* `macro_onchain_sentiment` (fear/greed, altcoin season, BTC dominance, exchange netflow/reserve, ETF flows) and `order_book_microstructure`. Adding a writer for fear/greed + BTC dominance is a reasonable first post-MVP step, **not** an MVP blocker.

---

## 3. Current Implementation Status

### 3.1 Phase ladder

| Phase | Name | Status | Evidence |
|-------|------|--------|----------|
| P0 | Paper baseline (ingest/analyze/rag, VPS schedules, kill-switch) | **Shipped on `main`** | PRs #1–#15; analyzer `08af0e3` landed via #17 |
| P1 | Analytics UI (`/analytics`, filters, PAPER ONLY banner) | **Shipped on `main`** | `da1cfe0`, PR #17 |
| P2 | Paper auto-trade ledger + `jarvise paper run\|status` | **Shipped on `main`** | `7df0643`, PR #17 |
| P3 | Exchange read-only (Binance spot balances, HMAC + Ed25519/RSA) | **Shipped on `main`** | PRs #19, #20 |
| P4-A/B | Manual approval **paper slice** (queue, approve/reject/expire, UI card) | **Shipped on `main`** | PR [#23](https://github.com/chatoe-norm/jarvise/pull/23) merged `def1f8f`; VPS Deploy green |
| P4-C | Size-capped **live** spot order on Approve + `live_orders` + hard caps | **Not started** (spec defers until B stable + risk caps) | — |
| P5 | Autonomy (auto live orders behind default-off flag) | **Not started, by design** | — |

### 3.2 Built and working

**Data / ingest** (`src/jarvise_ingest/`)
- `jarvise ingest`: Binance public klines (`GET /api/v3/klines`), timeframes `15m/1h/4h/1d`, `--limit`, paged `--since/--until` backfill, closed candles only, gap report, `--universe paper_core`, `--dry-run`, `--json`. GET-only, no auth.
- Indicators (`indicators.py`): ATR-14, RSI-14, EMA-20, EMA-200 recomputed over the full stored series; values withheld until seed influence < 1%.
- CoinGlass v4 derivatives (`providers/coinglass.py`, needs `COINGLASS_API_KEY`): OI OHLC, OI-weighted funding, aggregated liquidations → `derivatives_analytics` keyed by `(symbol, timestamp, ingested_at)` (bitemporal).
- Point-in-time universe (`universe.py`): `paper_core` = BTCUSDT, ETHUSDT (listed 2021-01-01) → `universe_membership`; blocks survivorship bias.
- SQLite schema + migrations (`db.py`, documented in `data/analytics/mvas-schema.sql`).

**Analysis** (`src/jarvise_analyze/`)
- `jarvise analyze --symbol|--universe --timeframe [--confidence-threshold] [--dry-run] --json`: classifies the **latest closed candle** into `analysis_output` (regime, action, confidence, invalidation, size, thesis, deterministic `analysis_id`).

**Paper trading** (`src/jarvise_paper/`)
- `engine.apply_signal`: flat closes; long/short closes opposite then opens sized by `size_pct_equity`; same side = hold; fees/slippage; MTM; writes `paper_orders`, `paper_positions`, `paper_account`, `performance_risk_metrics.daily_pnl_usd`.
- `approval.py` (**on `main`**): enqueue / approve / reject / expire; statuses `pending | approved | rejected | timed_out | failed`; approve uses the latest closed candle at approve time; atomic claim-before-fill; unique pending index per (symbol, timeframe); kill-switch fail-closed.
- CLI: `jarvise paper run [--auto-fill] | queue [--all] | approve <id> | reject <id> [--reason] | expire | status`. Default `run` **enqueues** (no fill); `--auto-fill` is the escape hatch. Kill-switch → exit 3, nothing written.

**Exchange read-only** (`src/jarvise_exchange/`)
- `VenueClient` protocol; `BinanceSpotClient.list_spot_balances()` (signed GET only); `resolve_binance_auth()` prefers PEM private key over HMAC secret; zero balances filtered; `exchange_balances` snapshots; `jarvise exchange sync-balances [--dry-run] --json`; panel on `/analytics` with soft-fail. **No order/trade/cancel/withdraw code exists anywhere in the repo.**

**Web** (`src/jarvise_web/app.py`, FastAPI, port 8080, optional basic auth, Tailscale-bound in prod)
- `GET /` control dashboard (kill-switch toggle, Redis/Qdrant health); `POST /kill-switch`.
- `GET /analytics` (symbol/timeframe filters; latest `analysis_output`; ingest/RAG Pipeline status from Redis; paper ledger; exchange panel; **approval queue card** with Approve/Reject + expires); `POST /approvals/approve|reject`.
- JSON: `GET /api/analysis`, `/api/paper`, `/api/status`, `/healthz`.

**Doctrine RAG** (`src/jarvise/rag.py`)
- `jarvise rag sync-notebook | sync-openclaw | ingest-sources | index | refresh`; source kinds `notebook, fetch, firecrawl, openclaw`; allowlist `config/rag-sources.json`; Qdrant `jarvise_doctrine`; Redis status keys `jarvise:rag:*`.

**Workspace CLI** (`src/jarvise/cli.py`, Typer): `status | init | config get|set|import`; every command has flags, `--help` examples, idempotent re-runs, no prompts.

**Background plane / infra**
- `src/jarvise/jobs.py`: `GET /healthz`, `POST /jobs/ingest` (BTCUSDT,ETHUSDT 1h ×200, ~15 min), `POST /jobs/rag-refresh` (~6 h); kill-switch → HTTP 409; optional `X-Jarvise-Token`. **Not yet:** `/jobs/paper-run`, `/jobs/paper-expire`.
- `docker-compose.yml` (+ `.prod.yml` Tailscale binding): Redis, Qdrant, n8n, OpenClaw (OpenRouter `openrouter/auto`, paper-only), worker/jobs, web. n8n workflows in `infra/n8n/workflows/` (ingest + rag only today).
- CI: `.github/workflows/ci.yml` (pytest on `ubuntu-latest`, PRs + main) → `deploy.yml` (self-hosted runner `srv1269762`, label `jarvise`, runs `/opt/jarvise/infra/deploy/vps-deploy.sh` on green main).
- OpenClaw plugin `plugins/jarvise-openclaw/` (skills: binance-intel, doctrine-rag, paper-research) → notes drop-folder → RAG `kind=openclaw`.

**Agent surface**: Cursor skills `.cursor/skills/jarvise-notebook`, `.cursor/skills/jarvise-binance-intel`; MCP example `mcp/jarvise-mcp.json.example` (Gemini Notebook, optional TradingView research MCP, OpenClaw with execution tools disabled); `scripts/ask-repo.ts` (secondary).

**Verification snapshot (2026-09-25, local):** `pytest` → **148 passed, 1 skipped** (network test), 26 test files, ~1.2 s. Local dev DB holds BTCUSDT + ETHUSDT 1h × 217 candles each (2026-09-13 → 2026-09-22); paper/approval/analysis tables are empty locally (the VPS DB is the operational one).

### 3.3 Partially built

- **Performance metrics**: `performance_risk_metrics` receives only `daily_pnl_usd`; `expected_value_ev`, `sharpe_ratio`, `sortino_ratio`, `max_drawdown_pct` are written as `NULL`. Doctrine's "EV clearly positive on paper" gate therefore cannot be evaluated from the DB yet.
- **Analyzer inputs**: uses only close/ATR/RSI/EMA20/EMA200. Derivatives are ingested but **not consumed**; no volume or multi-timeframe confirmation. Schema columns `vwap`, `adx_14` exist but are never computed.
- **Historical replay**: `analyze` classifies the latest closed candle only; there is no backtest/replay command producing an expectancy report, although the data layer (closed candles, warm-up, gaps, `--since`) was built to make one reproducible.
- **24/7 paper operation**: n8n schedules ingest and RAG only. `paper run` / `paper expire` are **CLI/manual** until jobs land. Design drafted: [`docs/superpowers/specs/2026-09-26-paper-jobs-24h-design.md`](docs/superpowers/specs/2026-09-26-paper-jobs-24h-design.md) (enqueue-only 4h run + 1h expire; Redis `jarvise:paper:*`; Approach 1 = subprocess jobs). **No implementation plan or code yet.**
- **Timeout semantics**: roadmap P4 says timeout → FLAT; slice B deliberately implements timeout → `timed_out` only (no position change). Decision still open.
- **Exchange panel**: raw asset balances only; no USD valuation and no reconciliation against the paper ledger.
- **OpenClaw paper-only**: `OPENCLAW_PAPER_ONLY` is a convention enforced by config/docs, not by Python code.
- **Doctrine text drift**: `data/analytics/sources/jarvise-doctrine.txt` still says "Binance TH is a regulated Thai spot venue"; `AGENTS.md` (newer) says trade worldwide, Binance is one option. Re-sync the notebook extract when doctrine is next refreshed.

### 3.4 Missing entirely

- **P4-C live order path**: `live_orders` table, place-order adapter in `jarvise_exchange` (spot only), pre-submit permission check (reject keys with withdrawal/transfer scope), explicit live flag (default off), audit of every attempt.
- **Hard caps / risk module**: max notional per order, max daily loss, drawdown lock that engages the kill-switch — none exist in code (paper sizing is only `MAX_SIZE_PCT = 2.0`).
- **P5 autonomy** flag and scheduler path (correctly absent).
- **On-chain / sentiment writers** for `macro_onchain_sentiment`; **order-book microstructure** writer.
- **Stocks/ETFs** data or execution providers.
- **Multi-venue routing**; venues beyond Binance spot.
- **Alerts/notifications** for pending approvals (owner currently has to open `/analytics` or run `paper queue`).

---

## 4. Current File Structure

Curated tree (omits `.git`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, gitignored runtime files, and `.superpowers/` SDD scratch).

```text
jarvise/
├── AGENTS.md                         # learned owner preferences + workspace facts (authoritative)
├── PROJECT_CONTEXT.md                # this file
├── README.md                         # quick start, MCP plane, guardrails, spec index
├── pyproject.toml                    # package `jarvise` 0.1.0; extras: dev, rag, web; 4 console scripts
├── docker-compose.yml                # redis, qdrant, n8n, openclaw, worker/jobs, web
├── docker-compose.prod.yml           # Tailscale-only port binding overlay
├── .env.example                      # all env vars (Binance read-only keys, CoinGlass, Redis, Qdrant, n8n, OpenClaw, jobs, approval TTL)
├── .gitignore
├── package.json / package-lock.json / tsconfig.json   # secondary `ask-repo` helper only
├── jarvise.code-workspace
├── .github/workflows/
│   ├── ci.yml                        # pytest on ubuntu-latest (PRs + main)
│   └── deploy.yml                    # self-hosted VPS runner after green CI on main
├── .cursor/
│   ├── rules/prefer-auto-composer.mdc
│   └── skills/
│       ├── jarvise-notebook/SKILL.md         # query Gemini Notebook doctrine first
│       └── jarvise-binance-intel/SKILL.md    # read-only Binance Web3 intel (no keys, no orders)
├── .jarvise/status.json              # local workspace CLI state (gitignored)
├── bin/jarvise                       # bash shim (legacy; prefer .venv/bin/jarvise)
├── config/
│   ├── notebook.json                 # Gemini Notebook id/alias (nlm profile `chatoe`)
│   ├── rag-sources.json              # allowlisted fetch/Firecrawl URLs for doctrine RAG
│   └── openclaw/openclaw.json.example
├── data/
│   ├── analytics/
│   │   ├── jarvise.db                # SQLite numeric truth (gitignored)
│   │   ├── mvas-schema.sql           # documented schema (all tables incl. approval_queue, exchange_balances)
│   │   ├── api-map.json              # provider allow/deny list
│   │   ├── stack.md
│   │   └── sources/                  # doctrine extracts → RAG (notebook/, openclaw/, *.txt)
│   ├── nlm/                          # NotebookLM session data for VPS (gitignored except README)
│   └── openclaw/                     # OpenClaw state + exports drop-folder (gitignored)
├── docs/
│   ├── product-usage.md              # day-to-day invoke paths (CLI, VPS, agent)
│   ├── deploy/
│   │   ├── hostinger-vps.md          # VPS bring-up (Tailscale, compose, runner)
│   │   └── ops.md                    # kill-switch, Redis keys, schedules
│   ├── exchange/
│   │   ├── binance-for-jarvise.md    # allowed/forbidden Binance capabilities, Skills Hub matrix
│   │   └── binance-vision-qa-reference.md
│   └── superpowers/
│       ├── specs/
│       │   ├── 2026-09-21-paper-ingest-design.md
│       │   ├── 2026-09-22-openclaw-intelligence-audit.md
│       │   ├── 2026-09-23-product-roadmap-design.md        # P0–P5 ladder (authoritative roadmap)
│       │   ├── 2026-09-23-p3-exchange-readonly-design.md
│       │   └── 2026-09-23-p4-manual-approval-paper-slice-design.md
│       └── plans/
│           ├── 2026-09-21-paper-ingest-mcp.md
│           ├── 2026-09-23-p1-analytics-ui.md
│           ├── 2026-09-23-p2-paper-auto-trade.md
│           ├── 2026-09-23-p3-exchange-readonly.md
│           └── 2026-09-23-p4-manual-approval-paper-slice.md  # Tasks 1–5 all complete on branch
├── infra/
│   ├── deploy/vps-deploy.sh          # force-checkout main + compose up --build
│   ├── docker/                       # Dockerfile.web, Dockerfile.worker, entrypoints, OpenClaw seed
│   └── n8n/                          # host helpers + workflows (ingest-schedule, rag-refresh)
├── mcp/jarvise-mcp.json.example      # Cursor MCP: notebook, TradingView research, OpenClaw (exec tools off)
├── plugins/jarvise-openclaw/         # OpenClaw skills pack (binance-intel, doctrine-rag, paper-research)
├── scripts/
│   ├── ask-repo.ts                   # Cursor SDK repo Q&A (secondary)
│   ├── export_notebook_sources.py    # NotebookLM → data/analytics/sources
│   └── rag_index_doctrine.py         # one-off Qdrant indexer
├── secrets/                          # PEM keys on VPS only (gitignored)
├── src/
│   ├── jarvise/                      # Typer root CLI + shared plumbing
│   │   ├── cli.py                    # status/init/config; delegates ingest/analyze/paper; rag + exchange groups
│   │   ├── config.py
│   │   ├── jobs.py                   # internal HTTP jobs API for n8n (ingest, rag-refresh)
│   │   └── rag.py                    # doctrine sync/index → Qdrant, Redis status
│   ├── jarvise_ingest/               # OHLCV + derivatives ingest, indicators, schema
│   │   ├── cli.py, db.py (schema + all table helpers), indicators.py, series.py
│   │   ├── timeframes.py (15m/1h/4h/1d), universe.py (paper_core)
│   │   └── providers/ binance_klines.py, coinglass.py
│   ├── jarvise_analyze/              # deterministic regime/confidence/invalidation/size
│   │   ├── cli.py, engine.py
│   ├── jarvise_paper/                # simulated ledger + approval queue (no exchange APIs)
│   │   ├── cli.py, engine.py, approval.py
│   ├── jarvise_exchange/             # read-only venue adapters (Binance spot balances)
│   │   ├── protocol.py, models.py, binance_spot.py, db.py, sync.py, cli.py
│   └── jarvise_web/app.py            # FastAPI control + /analytics + JSON API
└── tests/                            # 26 files, 148 passing + 1 skipped network test
    ├── test_cli.py, test_workspace_cli.py, test_jobs.py, test_rag.py, test_web.py
    ├── test_binance_klines.py, test_klines_range.py, test_market_series.py, test_indicators.py
    ├── test_db_upsert.py, test_derivatives_bitemporal.py, test_universe.py
    ├── test_analyze_cli.py, test_analyze_engine.py
    ├── test_paper_cli.py, test_paper_engine.py, test_paper_approval.py, test_approval_db.py
    ├── test_exchange_*.py (binance_map, cli, db, models, signing, sync, web)
    └── test_openclaw_plugin.py
```

Console scripts: `jarvise` (canonical), `jarvise-ingest`, `jarvise-analyze`, `jarvise-paper` (compatibility aliases).

---

## 5. Next Immediate Steps (prioritized checklist to finish the MVP)

Repo convention: **spec → plan → TDD implementation → review → PR to `main`**; the VPS redeploys automatically on green `main`. Each step below is one PR-sized slice. Do not start a lower item before the one above is merged or explicitly deferred.

- [x] **1. Land P4 paper slice B.** Merged [PR #23](https://github.com/chatoe-norm/jarvise/pull/23) (`def1f8f`); Deploy green 2026-09-25. Owner smoke (when on Tailscale): `jarvise paper run --universe paper_core --timeframe 4h --json`, Approve/Reject once on `/analytics` + CLI, confirm kill-switch blocks enqueue and approve.
- [ ] **2. Make paper trading run 24/7.** Spec + plan ready: [`2026-09-26-paper-jobs-24h-design.md`](docs/superpowers/specs/2026-09-26-paper-jobs-24h-design.md) · [`plans/2026-09-26-paper-jobs-24h.md`](docs/superpowers/plans/2026-09-26-paper-jobs-24h.md). **Next: implement Tasks 1–5** (jobs routes, n8n workflows, web Pipeline, docs, VPS activate). Enqueue-only 4h + expire 1h; env `JARVISE_PAPER_UNIVERSE` / `JARVISE_PAPER_TIMEFRAME`.
- [ ] **3. Performance / expectancy metrics (doctrine gate).** From `paper_orders`/`paper_positions` compute realized PnL per closed trade → win rate, avg win/loss, **EV after fees/slippage**, max drawdown %, and (once ≥ 30 trades) Sharpe/Sortino; fill the `NULL` columns in `performance_risk_metrics`; add `jarvise paper metrics --json` and a metrics card on `/analytics`. Without this the "EV clearly positive before live" rule cannot be checked.
- [ ] **4. Historical replay for paper signals.** `jarvise analyze --replay --since … --until …` (or `jarvise paper backtest`) that walks stored closed candles in order, writes `analysis_output` per candle, and feeds the step-3 metrics — reproducible because of closed-candle/warm-up/gap guarantees already in place. No new data sources.
- [ ] **5. Risk caps module (venue-agnostic, shared by paper and later live).** New `jarvise_risk` (or module in `jarvise_paper`) with `max_notional_per_order`, `max_daily_loss_usd`, `drawdown_lock_pct` from env (defaults conservative); enforce on paper approve first; on breach mark `failed` with reason and **engage the kill-switch**; surface caps on `/analytics`. Decide and implement **timeout → FLAT** here (roadmap says FLAT; slice B left it open).
- [ ] **6. P4-C design + plan (documents only).** Spec `live_orders` schema, `VenueClient.place_spot_order()` boundary in `jarvise_exchange`, pre-flight key-permission check (reject withdrawal/transfer scope), `JARVISE_LIVE_ENABLED=false` default, dust/min-notional handling, idempotency keys, full attempt audit. Gate: step 5 merged **and** slice B stable on the VPS for an owner-chosen period.
- [ ] **7. P4-C implementation (owner-gated).** Approve → caps check → kill-switch check → live spot order → `live_orders` row → paper ledger mirror. Explicit owner OK required before this PR is opened; live flag stays off by default in every environment.
- [ ] **8. Ops hygiene before any live trade.** Pending-approval notification (n8n → Slack/Telegram/email); `/analytics` USD valuation of exchange balances; re-sync doctrine extract (remove Binance-TH-only wording); confirm VPS `.env` Binance key has **no** withdrawal permission.

**Post-MVP backlog (do not start without a roadmap update):** P5 autonomy flag + scheduler; `macro_onchain_sentiment` writer (fear/greed, BTC dominance) and derivatives/ADX/MTF inputs to the analyzer; order-book microstructure; stocks/ETFs providers; additional venues/routing; public HTTPS UI; Python-enforced `OPENCLAW_PAPER_ONLY`.

---

## 6. Project history (condensed)

- **2026-08-11** — repo initialized (README + .gitignore).
- **2026-09-21** — Paper ingest (Binance klines + CoinGlass) and MCP background plane (Redis, Qdrant, n8n, OpenClaw); unified Typer CLI; Gemini Notebook config + skill; `ask-repo` helper; Hostinger VPS stack; n8n ingest schedule; self-hosted runner deploy. (PRs #1–#2)
- **2026-09-22** — Indicator reproducibility (full-series recompute, warm-up withholding); paged klines backfill + gap reporting; pinned images/deps; OpenClaw plugin + drop-folder RAG sync; doctrine extracts refreshed; VPS RAG schedule; **bitemporal derivatives**; **point-in-time universe** (PRs #3–#15); **paper analyzer** (regime/confidence/invalidation/size) committed, merged with #17.
- **2026-09-23** — Product roadmap P0–P5 written; **P1 `/analytics` UI**; **P2 paper ledger + `jarvise paper`**; venue-agnostic/TradingView-overlay docs; **P3 read-only Binance balances** (HMAC, then Ed25519/RSA); Redis status robustness; **P4 paper-slice design**. (PRs #17–#22)
- **2026-09-25** — P4 implementation plan; approval queue schema, engine, CLI, `/analytics` card, docs; atomic claim + fail-closed kill-switch. **PR #23 merged to `main`; Deploy green.**
- **2026-09-26** — Brainstorm + design for **paper jobs 24/7** (enqueue + expire schedules); file `docs/superpowers/specs/2026-09-26-paper-jobs-24h-design.md` (local; plan/code not started).
- **2026-09-28** — Status refresh in this file + `AGENTS.md`; ladder P0–P4-B shipped; active next = §5.2 paper jobs.

Cadence: short-lived branches + PR + auto-Deploy on green `main`; every feature has landed via spec/plan first.

---

## 7. Operating facts (pointers; details in `AGENTS.md`)

- **Runtime:** Hostinger KVM 2 `srv1269762` (Ubuntu 24.04, 2 vCPU, 8 GB), stack at `/opt/jarvise`, private ingress via Tailscale `100.93.110.48`; UIs: web `:8080`, n8n `:5678`, OpenClaw `:18789`, jobs `:8090` (Docker network only).
- **Accounts/tooling:** GitHub `chatoe-norm/jarvise` (push via SSH host `github.com-chatoe-norm`; `gh` account `chatoe-norm`); NotebookLM `nlm` profile `chatoe`, notebook alias `jarvise` (`14e11c63-…`); OpenRouter model `openrouter/auto`.
- **Local dev:** Python 3.12 venv; `pip install -e '.[dev]'`; `python -m pytest`; Windows uses `.venv\Scripts\jarvise`.
- **Model/cost preference:** Auto/Composer by default; escalate only when asked or when they fail.
- **Guardrail checklist (roadmap §6):** ladder never skipped; live/autonomy flags default off; kill-switch respected everywhere; caps before any live submit; secrets only in env; ingest stays GET-only; doctrine consulted before trade calls; crypto first, worldwide orientation.

## 8. Key documents

- `AGENTS.md` — owner preferences and workspace facts (authoritative for conventions)
- `docs/superpowers/specs/2026-09-23-product-roadmap-design.md` — P0–P5 ladder (authoritative for scope)
- `docs/product-usage.md` — how to run the shipped product today
- `docs/superpowers/specs/2026-09-23-p4-manual-approval-paper-slice-design.md` + `plans/…p4-manual-approval-paper-slice.md` — **P4-B shipped**
- `docs/superpowers/specs/2026-09-26-paper-jobs-24h-design.md` — **next slice** (design; awaiting accept → plan → code)
- `docs/exchange/binance-for-jarvise.md` — allowed vs forbidden exchange capabilities
- `docs/deploy/hostinger-vps.md`, `docs/deploy/ops.md` — VPS bring-up and kill-switch ops
- `data/analytics/mvas-schema.sql` — full SQLite schema
