# PROJECT_CONTEXT.md — Jarvise

**Purpose:** single source of truth against context drift. Read this before proposing or writing any code.
**Generated:** 2026-09-25; **status refreshed:** 2026-10-03.
**Snapshot:** MVP ladder **P0–P4-C (gated)** + paper excellence **Tier 0–2** shipped on `main` ([PR #83](https://github.com/chatoe-norm/jarvise/pull/83); flat_exit [#85](https://github.com/chatoe-norm/jarvise/pull/85)); VPS Deploy green. Live submit **code** stays gated off. **Coverage verdict:** intelligence + hardened paper autotrade + Approve→live *door* — **not** unattended live (P5). **Next:** prove paper EV → optional checklist live smoke → stabilize P4-C → only then P5.
**Maintenance rule:** update §3 (status) and §5 (next steps) whenever a roadmap phase or PR lands. Doctrine/preference changes go to `AGENTS.md` first, then here.

### Status at a glance (2026-10-03)

| Area | State |
|------|--------|
| Ladder | **P0–P4-C (gated)** on `main` + paper excellence Tier 0–2 (#60, #83). |
| Autotrade coverage | **Paper + gated Approve path: yes.** **Unattended live (P5): no** — see §5.1. |
| Live trading | **Off by default** — `jarvise_trade` + `live_orders`; flag `JARVISE_LIVE_TRADING=false`. |
| Shipped UX | Command Dashboard SPA (`web/`: Home / Paper / Decisions / Exchange / Ops) + FastAPI JSON; approval-first Home. |
| Paper path | `paper run` → enqueue → manual Approve **or** auto-decide (+ soft EV gate) → paper fill when flag off. |
| Equity paper | Universe `paper_equity` (SPY/QQQ) via Stooq 1d; paper-only; no broker live. |
| VPS schedules | n8n active: ingest ~15m, rag ~6h, **paper-run 4h**, **paper-expire 1h**, pending digest / auto-decide as configured. |
| Next | Prove auto-decide EV via feedback; optional checklist live enable; stabilize before P5. |
| Paper EV sampling | VPS `JARVISE_ANALYZE_MTF=false` temporarily (compose-wired) to accumulate `auto_*` closes while HTF chaotic; restore `true` after N≈10. Live stays false. |
| Risk | Per-order + portfolio book caps (open/gross/symbol/bucket) + market safety; timeout → FLAT (paper). |
| Obs | `GET /metrics` (web + jobs); optional compose profile `obs` (Prometheus/Grafana, Tailscale-bound). |
| P4-C | Implemented: Approve → caps → `jarvise_trade` MARKET POST → `live_orders`; no paper mirror when live. |
| VPS | Deploy green after #83/#85; healthz `paper_only:true`. Live flag stays false. |

---

## 0. Framing check (read first)

The request that produced this file described the project as a *"Day Trading App using Python/FastAPI for retail traders."* The codebase, roadmap, and `AGENTS.md` describe something narrower and more specific. Where they differ, **the repository wins** until the owner explicitly changes the roadmap:

- **Not day trading.** Signals are computed on **closed 1h / 4h candles** (`15m` and `1d` are also allowed intervals), with a doctrine of *slow, steady gains*, capital preservation, and FLAT-by-default. There is no intraday scalping, tick data, or order-book execution logic.
- **Not for retail traders / not multi-tenant.** Jarvise is an **owner-operated** system for one person's capital. "Public marketing site or multi-tenant SaaS" is an explicit roadmap non-goal. The UI is private (Tailscale-only) and has no accounts/signup.
- **Python: yes. FastAPI: partly.** The **primary interface is a Typer/argparse CLI** (`jarvise ...`). FastAPI powers the private control/analytics web (`src/jarvise_web/app.py`) and is an optional extra (`pip install -e '.[web]'`). A stdlib `http.server` powers the internal jobs API.
- **Crypto first; equity paper now wired.** Crypto (BTCUSDT, ETHUSDT via Binance public klines) remains the primary book; **paper** stocks/ETFs (`paper_equity` SPY/QQQ via Stooq 1d) ingest and paper ledger are shipped — **no broker live**.

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

*Not required for MVP (post-MVP):* ADX/VWAP, volume confirmation, derivatives as analyzer *triggers* (they already feed safety context). HTF multi-timeframe confirm is shipped post-MVP (Tier 2).

### 2.2 Trading / Execution features — minimum

- **Paper ledger** (`paper_account`, `paper_orders`, `paper_positions`): simulated fills with default **10 bps** fee (Binance spot taker; override `JARVISE_PAPER_FEE_BPS`) and slip `max(5 bps, half bid-ask spread)` when book data exists; one position per symbol; MTM equity; starting equity $10,000.
- **Approval queue** (`approval_queue`): `paper run` enqueues by default; owner Approves/Rejects on Command Dashboard / CLI (or auto-decide); TTL (`JARVISE_APPROVAL_TIMEOUT_MIN`, default 60) → `timed_out`; one pending row per (symbol, timeframe); atomic claim; kill-switch fail-closed.
- **Kill-switch** (Redis `jarvise:kill_switch`) honored by schedules, paper run/approve, and the web toggle.
- **Exchange read-only**: Binance spot balances (`GET /api/v3/account`, HMAC or Ed25519/RSA) → `exchange_balances`, shown beside the paper ledger; secrets only in VPS `.env`; soft-fail hides the panel.
- **Performance gate**: expectancy / drawdown metrics from the paper ledger sufficient to judge "clearly positive EV" (doctrine prerequisite for live).
- **P4-C live path (last MVP rung, owner-gated)**: on Approve when `JARVISE_LIVE_TRADING=true`, size-capped live **spot** MARKET via `jarvise_trade` → `live_orders` audit; hard caps + kill-switch before submit; live flag default **off**.
- 24/7 operation on the VPS: n8n → jobs API for ingest, RAG refresh, **and** paper run/expire.

### 2.3 On-chain / Sentiment features — minimum

Per doctrine these are **context that lowers/raises confidence or vetoes**, never a trigger. The roadmap defers on-chain *writers* to "later" (§7), so the MVP minimum is small:

- Derivatives context — funding/OI (and liquidations when CoinGlass keyed) as crowding context for market safety.
- Order-book microstructure + BTC dominance / global mcap writers (Binance public book + CoinGecko global) with `jarvise_risk.market_safety` FLAT/kill-switch gate — see `docs/superpowers/specs/2026-10-01-market-safety-ingest-design.md`.
- Agent-side **Binance Web3 intel** (`jarvise-binance-intel` skill, no keys): token search/meta, **security audit** (audit `HIGH`, `riskType: RISK`, or sell tax → **FLAT veto**), market rank, social hype, smart-money inflow, tokenized US stocks info, Academy risk education.
- Agent-side **CoinGecko AI tools** (MCP / Docs MCP / CLI — optional, not wired by default): research overlay only; map in `docs/market-data/coingecko-for-jarvise.md`; never doctrine RAG.
- Doctrine RAG + OpenClaw research notes as the "sentiment/narrative" layer.

*Post-MVP (schema reserved):* remaining `macro_onchain_sentiment` fields (fear/greed, altcoin season, exchange netflow/reserve, ETF flows) and spoof-wall heuristics on `order_book_microstructure`.

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
| P4-C | Size-capped **live** spot order on Approve + `live_orders` + hard caps | **Shipped gated off** (`JARVISE_LIVE_TRADING=false`) | `jarvise_trade` + approve branch |
| P5 | Autonomy (auto live orders behind default-off flag) | **Not started, by design** | — |
| Paper excellence Tier 0–1 | Fail-closed kill-switch, WAL/backups, job locks, retries, CI/containers | **Shipped on `main`** | PR [#60](https://github.com/chatoe-norm/jarvise/pull/60) |
| Paper excellence Tier 2 | Feedback/outcomes + soft EV gate, portfolio caps, MTF, `/metrics`+obs, Eterna VenueClient stub, Stooq equity | **Shipped on `main`** | PR [#83](https://github.com/chatoe-norm/jarvise/pull/83); Deploy green 2026-10-03 |

### 3.2 Built and working

**Data / ingest** (`src/jarvise_ingest/`)
- `jarvise ingest`: Binance public klines (`GET /api/v3/klines`), timeframes `15m/1h/4h/1d`, `--limit`, paged `--since/--until` backfill, closed candles only, gap report, `--universe paper_core`, `--dry-run`, `--json`. GET-only, no auth.
- Indicators (`indicators.py`): ATR-14, RSI-14, EMA-20, EMA-200 recomputed over the full stored series; values withheld until seed influence < 1%.
- Derivatives router (`providers/derivatives.py`): **Binance Futures public** funding + OI hist by default (no key); CoinGlass v4 when `COINGLASS_API_KEY` set (OI/funding/liquidations + best-effort L/S) → `derivatives_analytics` bitemporal.
- Binance public book (`providers/binance_book.py`): `ticker/bookTicker` + `depth` → `order_book_microstructure` (spread, ±1% depth USD).
- CoinGecko global (`providers/coingecko_global.py`): BTC dominance + total market cap → `macro_onchain_sentiment`. CoinGecko AI Integration (MCP/CLI/SKILL) is an optional research overlay only — see `docs/market-data/coingecko-for-jarvise.md`; not a SQLite writer.
- Market-safety gate (`jarvise_risk.market_safety`): FLAT + block enqueue/approve on unsafe/stale/anomalous data; kill-switch on critical failures when `JARVISE_MARKET_SAFETY=1` (default on). Flags: `--skip-book`, `--skip-macro`, `--skip-derivatives`.
- Point-in-time universe (`universe.py`): `paper_core` = BTCUSDT, ETHUSDT; `paper_equity` = SPY, QQQ → `universe_membership`; blocks survivorship bias.
- Stooq equity OHLCV (`providers/stooq_ohlcv.py`): free CSV GET for `paper_equity` 1d; book/deriv streams auto-skipped for equities; weekend 1d holes not reported as gaps.
- SQLite schema + migrations (`db.py`, `user_version` 7; documented in `data/analytics/mvas-schema.sql`) including `paper_decision_outcomes` / `paper_auto_runs`.

**Analysis** (`src/jarvise_analyze/`)
- `jarvise analyze --symbol|--universe --timeframe [--confidence-threshold] [--dry-run] --json`: classifies the **latest closed candle** into `analysis_output` (regime, action, confidence, invalidation, size, thesis, deterministic `analysis_id`).
- **MTF (Tier 2):** `analyze_mtf` — LTF = `JARVISE_PAPER_TIMEFRAME`, HTF confirm from `JARVISE_ANALYZE_HTF` (default `1d`); directional LTF forced flat on HTF range/chaotic/conflict or missing HTF (`mtf_reason`). Disable with `JARVISE_ANALYZE_MTF=false`.

**Paper trading** (`src/jarvise_paper/`)
- `engine.apply_signal`: flat closes; long/short closes opposite then opens sized by `size_pct_equity`; same side = hold; fees/slippage; MTM; writes `paper_orders`, `paper_positions`, `paper_account`, `performance_risk_metrics.daily_pnl_usd`; fills stamp `approval_id` + `decision_source`.
- `approval.py` (**on `main`**): enqueue / approve / reject / expire; statuses `pending | approved | rejected | timed_out | failed`; approve uses the latest closed candle at approve time; atomic claim-before-fill; unique pending index per (symbol, timeframe); kill-switch fail-closed; portfolio book caps on enqueue + approve.
- `auto_decide.py` + `feedback.py`: optional auto Approve/Reject; outcomes sync; metrics by `decision_source`; soft EV gate (`JARVISE_AUTO_DECIDE_MIN_EV` / `_MIN_EV_N`) skips auto when 30d auto EV is below floor.
- CLI: `jarvise paper run [--auto-fill] | queue [--all] | approve <id> | reject <id> [--reason] | expire | status | metrics`. Default `run` **enqueues** (no fill); `--auto-fill` is the escape hatch. Kill-switch → exit 3, nothing written.

**Risk** (`src/jarvise_risk/`)
- Per-order notional / daily loss / drawdown lock; portfolio caps `JARVISE_MAX_OPEN_POSITIONS`, `JARVISE_MAX_GROSS_NOTIONAL_PCT`, `JARVISE_MAX_SYMBOL_NOTIONAL_PCT`, `JARVISE_MAX_CORRELATED_BUCKET_PCT` (soft block, no kill-switch); market-safety FLAT/kill on critical; expose caps on Ops.

**Exchange read-only** (`src/jarvise_exchange/`)
- `VenueClient` protocol + **registry**; `BinanceSpotClient.list_spot_balances()` (signed GET only); `resolve_binance_auth()` prefers PEM private key over HMAC secret; zero balances filtered; `exchange_balances` snapshots; `jarvise exchange sync-balances [--dry-run] --json`; Exchange page soft-fail. **Eterna** spot: fixture/blocker only (research overlay; no safe GET-only REST — see `docs/exchange/eterna-for-jarvise.md`). **No order POSTs in this package.**

**Live trade (gated)** (`src/jarvise_trade/`)
- Spot MARKET POST allowlist only (`POST /api/v3/order`); trade keys `BINANCE_TRADE_*` distinct from read keys; wired from `approve_approval` when `JARVISE_LIVE_TRADING=true`; audits `live_orders`; no paper ledger mirror on live path; withdraw-scoped keys refused.

**Web** (`src/jarvise_web/app.py` + `web/` SPA, FastAPI, port 8080, optional basic auth, Tailscale-bound in prod)
- Command Dashboard: Home (approval-first KPIs), Paper (ledger + feedback card), Decisions, Exchange (RO balances), Ops (risk caps, Grafana link, kill-switch).
- JSON: `GET /api/analysis`, `/api/paper`, `/api/paper/metrics`, `/api/status`, `/healthz`, `GET /metrics` (Prometheus text).
- Legacy `/analytics` HTML may still exist for compatibility; SPA is the owner surface.

**Observability** (`src/jarvise_obs/`)
- In-process Prometheus text (kill-switch, queue depth, job runs/duration); optional compose profile `obs` → Prometheus `:9090` + Grafana `:3000` (127.0.0.1 / Tailscale, 256m limits).

**Notify** (`src/jarvise_notify/`)
- Telegram pending-approval alerts + hourly digest; soft-fail if `TELEGRAM_*` unset.

**Doctrine RAG** (`src/jarvise/rag.py`)
- `jarvise rag sync-notebook | sync-openclaw | ingest-sources | index | refresh`; source kinds `notebook, fetch, firecrawl, openclaw`; allowlist `config/rag-sources.json`; Qdrant `jarvise_doctrine`; Redis status keys `jarvise:rag:*`.

**Workspace CLI** (`src/jarvise/cli.py`, Typer): `status | init | config get|set|import`; every command has flags, `--help` examples, idempotent re-runs, no prompts.

**Background plane / infra**
- `src/jarvise/jobs.py`: `GET /healthz`, `GET /metrics`, `POST /jobs/ingest` (1h + paper TF + HTF), `POST /jobs/rag-refresh`, `POST /jobs/paper-run`, `POST /jobs/paper-expire`, `POST /jobs/paper-pending-digest`, `POST /jobs/ingest-health`, `POST /jobs/paper-auto-decide`, `POST /jobs/live-reconcile`; kill-switch → HTTP 409; optional `X-Jarvise-Token`; single-flight locks.
- `docker-compose.yml` (+ `.prod.yml` Tailscale binding): Redis, Qdrant, n8n, OpenClaw (OpenRouter `openrouter/auto`, paper-only), worker/jobs, web; optional `--profile obs`.
- CI: `.github/workflows/ci.yml` (pytest on `ubuntu-latest`, PRs + main) → `deploy.yml` (self-hosted runner `srv1269762`, label `jarvise`, runs `/opt/jarvise/infra/deploy/vps-deploy.sh` on green main).
- OpenClaw plugin `plugins/jarvise-openclaw/` (skills: binance-intel, doctrine-rag, paper-research) → notes drop-folder → RAG `kind=openclaw`.

**Agent surface**: Cursor skills `.cursor/skills/jarvise-notebook`, `.cursor/skills/jarvise-binance-intel`, `.cursor/skills/jarvise-eterna`, `.cursor/skills/jarvise-ux-ui`; MCP example `mcp/jarvise-mcp.json.example` (Gemini Notebook, optional TradingView research MCP, OpenClaw with execution tools disabled); `scripts/ask-repo.ts` (secondary).

**Verification snapshot (2026-10-03):** local pytest ~**380** passed at Tier 2 implement; [PR #83](https://github.com/chatoe-norm/jarvise/pull/83) merged; VPS Deploy green with healthz `{"ok":true,"paper_only":true}`. Operational DB is on the VPS.

### 3.3 Partially built

- **Performance metrics**: `jarvise paper metrics` / `/api/paper/metrics` compute EV, win rate, MDD from closed round-trips; split by `decision_source` (manual / auto_*); Sharpe/Sortino after ≥30; optional persist into `performance_risk_metrics`. Soft EV gate needs enough closed auto trades before it blocks.
- **Analyzer inputs**: close/ATR/RSI/EMA20/EMA200 + optional HTF confirm. Derivatives are ingested but **not consumed as triggers**; no volume confirmation. Schema columns `vwap`, `adx_14` exist but are never computed.
- **Historical replay**: `jarvise analyze --replay --since …` walks stored closed candles; `--apply-paper` fills only on an isolated `--db` (refuses default live ledger).
- **24/7 paper operation**: VPS n8n **activated** 2026-09-29 (`Jarvise paper run` / `Jarvise paper expire`). Jobs ingest refreshes `1h` + paper TF + HTF so enqueue/MTF have candles.
- **Timeout semantics**: paper timeout → `timed_out` + FLAT open positions (`resolve_reason=timeout_flat`). Live venue flatten is out of P4-C.
- **Exchange panel**: raw balances + **~USD** (Binance public USDT ticker; stables face value; unpriced shown as —).
- **Eterna venue**: registry + fixture path only; no production REST balances.
- **OpenClaw paper-only**: `OPENCLAW_PAPER_ONLY` is a convention enforced by config/docs, not by Python code.
- **Doctrine text:** local extracts aligned 2026-10-01 with AGENTS worldwide venue policy (Binance = first adapter). Notebook sources may still need a later refresh.
- **Live enablement**: code present but flag off; trade keys unset; no withdraw; ops checklist before turning on.

### 3.4 Missing entirely

- **Live timeout / venue flatten** (paper timeout→FLAT only).
- **P5 autonomy** flag and scheduler path (correctly absent).
- **On-chain / sentiment writers** for remaining `macro_onchain_sentiment` fields (fear/greed, ETF, netflow); spoof-wall heuristics.
- **Broker equity live** (paper Stooq only).
- **Real second-venue REST** balances/orders (Eterna remains fixture/blocker; Binance is the only live-capable adapter, still gated).
- **Multi-venue routing** / smart order routing.

---

## 4. Current File Structure

Curated tree (omits `.git`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, gitignored runtime files, and `.superpowers/` SDD scratch).

```text
jarvise/
├── AGENTS.md                         # learned owner preferences + workspace facts (authoritative)
├── PROJECT_CONTEXT.md                # this file
├── README.md                         # quick start, MCP plane, guardrails, spec index
├── pyproject.toml                    # package `jarvise` 0.1.0; extras: dev, rag, web; 4 console scripts
├── docker-compose.yml                # redis, qdrant, n8n, openclaw, worker/jobs, web; profile obs
├── docker-compose.prod.yml           # Tailscale-only port binding overlay
├── web/                              # Command Dashboard SPA (Vite/React, shadcn-style)
├── .env.example                      # all env vars (keys, risk/portfolio caps, auto-decide EV gate, obs)
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
│   ├── observability/                # prometheus.yml + Grafana provisioning (profile obs)
│   └── n8n/                          # host helpers + workflows (ingest, rag, paper-run/expire, …)
├── mcp/jarvise-mcp.json.example      # Cursor MCP: notebook, TradingView research, OpenClaw (exec tools off)
├── plugins/jarvise-openclaw/         # OpenClaw skills pack (binance-intel, doctrine-rag, paper-research)
├── scripts/
│   ├── ask-repo.ts                   # Cursor SDK repo Q&A (secondary)
│   ├── export_notebook_sources.py    # NotebookLM → data/analytics/sources
│   └── rag_index_doctrine.py         # one-off Qdrant indexer
├── secrets/                          # PEM keys on VPS only (gitignored)
├── src/
│   ├── jarvise/                      # Typer root CLI + shared plumbing
│   │   ├── cli.py, config.py, rag.py
│   │   └── jobs.py                   # n8n jobs API (ingest, rag, paper-*, auto-decide, metrics)
│   ├── jarvise_ingest/               # OHLCV + book/macro/derivs, indicators, schema (uv=7)
│   │   ├── cli.py, db.py, indicators.py, series.py, universe.py (paper_core, paper_equity)
│   │   └── providers/ binance_klines, binance_book, coingecko_global, derivatives, stooq_ohlcv, …
│   ├── jarvise_analyze/              # regime/confidence/invalidation/size + analyze_mtf
│   ├── jarvise_paper/                # ledger, approval, auto_decide, feedback, metrics
│   ├── jarvise_risk/                 # caps (order + portfolio), market_safety, kill-switch
│   ├── jarvise_exchange/             # VenueClient registry, Binance spot RO, eterna_spot stub
│   ├── jarvise_trade/                # gated live spot MARKET + live_orders
│   ├── jarvise_obs/                  # Prometheus text helpers
│   ├── jarvise_notify/               # Telegram soft-fail alerts
│   └── jarvise_web/app.py            # FastAPI JSON API + static SPA
└── tests/                            # ~380 passing at Tier 2 (#83); unit + integration
```

Console scripts: `jarvise` (canonical), `jarvise-ingest`, `jarvise-analyze`, `jarvise-paper` (compatibility aliases).

---

## 5. Next Immediate Steps

Repo convention: **spec → plan → TDD implementation → review → PR to `main`**; the VPS redeploys automatically on green `main`.

### 5.1 Coverage verdict — crypto autotrade after Tier 0–2

**Verdict (2026-10-03):** Tier 0–1 hardening + Tier 2 paper excellence make Jarvise **ready as hardened intelligence + paper autotrade**, with an **Approve→live door** (P4-C code, flag off). They do **not** complete end-state **unattended live crypto autotrade (P5)**. Both plans explicitly left live flag and P5 out of scope.

| Layer | Meaning | After T0–1 + T2 |
|-------|---------|-----------------|
| Intelligence | OHLCV/book/macro/derivs, analyze, doctrine RAG, MTF HTF | **Covered for crypto paper** |
| Paper auto | enqueue → auto-decide → fills → feedback / soft EV gate | **Code complete**; VPS `auto_*` EV sample still thin |
| Manual live (P4-C) | Approve → size-capped spot | **Code complete, flag off**; not smoked on VPS |
| Autonomy (P5) | scheduler places live without per-trade Approve | **Absent by design** |

**Still not “full autotrade”:** prove `by_decision_source_30d` for `auto_*` (N≈10+); owner live checklist smoke; P5 flag/scheduler; live venue flatten on timeout; real second-venue REST (Eterna = fixture); ADX/volume triggers; remaining macro writers; CoinGecko Pro/public URL mismatch on HTF ingest when it fails.

**Do not skip the ladder:** paper EV → optional gated live → stabilize P4-C → **then** a separate P5 plan.

### MVP checklist (complete; live flip still owner-gated)

- [x] **1–7.** P4-B → paper 24/7 → expectancy → replay → risk caps → P4-C design → P4-C implementation (gated). Evidence in §3 / §6.
- [x] **8. Ops hygiene before any live trade** (code + checklists ready; **flag remains false**).
  - [x] **8a.** Exchange balance ~USD valuation.
  - [x] **8b.** Pending-approval Telegram (enqueue + hourly digest; soft-fail).
  - [x] **8c.** Doctrine extracts (worldwide venue; drop Binance-TH-only lock).
  - [x] **8d.** Key permission probe + live submit refuses withdraw-scoped keys. Checklist: [`docs/ops/live-enable-checklist.md`](docs/ops/live-enable-checklist.md).

### Owner / ops next (not code by default)

1. **Prove paper EV** — run auto-decide long enough for `by_decision_source_30d` on `auto_*`; set soft EV gate (`JARVISE_AUTO_DECIDE_MIN_EV`) only when N is honest; do not raise autonomy on thin data.
2. **Optional live enable** — only via checklist (`BINANCE_TRADE_*` + `JARVISE_LIVE_TRADING=true`); keep paper-only until then.
3. **Stabilize gated live** — smoke Approve → live path on a tiny size; then consider P5. **Do not start P5 before this.**

### Post-MVP backlog (do not start without a roadmap update)

P5 autonomy flag + scheduler; remaining `macro_onchain_sentiment` fields (fear/greed, ETF flows, netflow) and ADX/volume analyzer inputs; broker equity live; real second-venue REST (beyond Eterna fixture); multi-venue routing; public HTTPS UI; Python-enforced `OPENCLAW_PAPER_ONLY`.

*(Shipped post-MVP, not backlog: HTF MTF confirm, Stooq `paper_equity` paper ingest, Telegram alerts, VenueClient registry + Eterna stub, Prometheus/Grafana profile, decision feedback + portfolio caps, flat_exit rule — see Tier 2 / #83, #85.)*

---

## 6. Project history (condensed)

- **2026-08-11** — repo initialized (README + .gitignore).
- **2026-09-21** — Paper ingest (Binance klines + CoinGlass) and MCP background plane (Redis, Qdrant, n8n, OpenClaw); unified Typer CLI; Gemini Notebook config + skill; `ask-repo` helper; Hostinger VPS stack; n8n ingest schedule; self-hosted runner deploy. (PRs #1–#2)
- **2026-09-22** — Indicator reproducibility (full-series recompute, warm-up withholding); paged klines backfill + gap reporting; pinned images/deps; OpenClaw plugin + drop-folder RAG sync; doctrine extracts refreshed; VPS RAG schedule; **bitemporal derivatives**; **point-in-time universe** (PRs #3–#15); **paper analyzer** (regime/confidence/invalidation/size) committed, merged with #17.
- **2026-09-23** — Product roadmap P0–P5 written; **P1 `/analytics` UI**; **P2 paper ledger + `jarvise paper`**; venue-agnostic/TradingView-overlay docs; **P3 read-only Binance balances** (HMAC, then Ed25519/RSA); Redis status robustness; **P4 paper-slice design**. (PRs #17–#22)
- **2026-09-25** — P4 implementation plan; approval queue schema, engine, CLI, `/analytics` card, docs; atomic claim + fail-closed kill-switch. **PR #23 merged to `main`; Deploy green.**
- **2026-09-26–29** — Paper jobs 24/7; expectancy; replay; risk caps + timeout→FLAT; VPS paper schedules activated.
- **2026-09-27** — P4 paper approval smoke on VPS; **P4-C live-submit design** (PR #24).
- **2026-10-01** — P4-C APPROVED + implemented (`jarvise_trade`, `live_orders`, Approve branch); `JARVISE_LIVE_TRADING` default false; market-safety ingest.
- **2026-10 (pre-#83)** — Command Dashboard SPA; paper auto-decide; Telegram pending alerts; hardening Tier 0–1 (PR #60: fail-closed kill-switch, WAL/backups, job locks, retries).
- **2026-10-03** — Paper excellence **Tier 2** (PR [#83](https://github.com/chatoe-norm/jarvise/pull/83)): decision feedback + soft EV gate, portfolio caps, MTF, `/metrics`+obs profile, Eterna VenueClient stub, Stooq `paper_equity`. Merged to `main`; **VPS Deploy green**.

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
- `docs/product-usage.md` — how to run the shipped product today (includes T2.1–T2.6)
- `docs/ops/live-enable-checklist.md` — owner steps before flipping live
- `docs/superpowers/specs/2026-09-23-p4-manual-approval-paper-slice-design.md` + plans — **P4-B shipped**
- `docs/superpowers/specs/2026-09-26-paper-jobs-24h-design.md` + plans — **shipped** (paper-run / paper-expire jobs + n8n)
- `docs/superpowers/specs/2026-09-27-p4c-live-submit-design.md` + `plans/2026-10-01-p4c-live-submit.md` — **P4-C shipped gated off**
- `docs/exchange/binance-for-jarvise.md` — allowed vs forbidden exchange capabilities
- `docs/exchange/eterna-for-jarvise.md` — Eterna research overlay + VenueClient stub status (T2.5)
- `docs/ux-ui/shadcn-for-jarvise.md` — shadcn → Command Dashboard SPA (`web/`)
- `docs/deploy/hostinger-vps.md`, `docs/deploy/ops.md` — VPS bring-up and kill-switch ops
- `data/analytics/mvas-schema.sql` — full SQLite schema (`user_version` 7)
