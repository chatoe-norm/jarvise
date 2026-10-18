# Jarvise Execution Context: EV, Doctrine Guardrails & Market Context (aligned 2026-10-18)

**Research overlay — not trading doctrine.** Do not index this page into Qdrant `jarvise_doctrine`, do not paste it as a system prompt that overrides repo rules, and do not use it to change floors **0.55 / 0.70**, the analyzer formula, the 1.5× ATR stop, `flat_exit`, `same_side_hold`, `profit_lock`, the EV gate, the universe, or `JARVISE_LIVE_TRADING`. Rules live in `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/jarvise-trading-rules.mdc`, and `.cursor/rules/model-tiers.mdc`. Status lives in `PROJECT_CONTEXT.md`.

This page replaces an earlier prompt written for a Prisma / Next.js rebuild on Antigravity IDE (also in [jarvise-ev-edge-claude.md](jarvise-ev-edge-claude.md)). That architecture is not Jarvise. What follows keeps the parts that fit the running system, marks what already exists, and lists what is still a candidate. Model choice follows `model-tiers.mdc` (Opus only when the owner asks; T3 is Claude Fable 5.1 or Claude Code).

## 1. Pre-flight protocol

1. **Plan first.** Before editing trading code, present the plan (files, schema, signatures, UI, tests) and wait for approval. This matches `jarvise-trading-rules.mdc`.
2. **Numbers.** The Python paper engine uses `float`; do not introduce a project-wide Decimal layer. Before any live enable, check that the live order path rounds qty and price to the venue's step and tick filters (checklist item, not verified here).
3. **Fail closed.** Missing or stale data, schema failures, unsafe book/derivatives/macro, or confidence below 0.55 end in `FLAT`. Market safety (`jarvise_risk.market_safety`), the kill switch, and the latched EV gate already do this. New code must keep it.
4. **Measure before trusting.** Check the measuring tool before a result (PR #179 found replay stops reading later bars). Pre-register any decision rule before running it (`CLAUDE.md`).

## 2. Architecture as it is

| Layer | Jarvise | Not used |
| --- | --- | --- |
| Data | Python CLI, SQLite `data/analytics/jarvise.db` via `jarvise_ingest.db`, closed candles only, GET-only writers | Prisma, ORMs, repository/`handlePrismaError` pattern |
| API | FastAPI `jarvise_web`, jobs service `jobs:8090` behind `X-Jarvise-Token` | Next.js Server Actions, `revalidatePath`, `router.refresh()` |
| UI | Vite + React SPA (`web/`), zod 4 schemas, 30 s polling | Server Components |
| Scheduling | n8n workflows in `infra/n8n/workflows` (rendered with tokens at import) | TradingView webhook order path (forbidden) |
| AI | OpenClaw on its own network (8 plugins), Qdrant `jarvise_doctrine` read-only, shadow veto via Python → OpenRouter | Third-party execution MCPs, Glassnode, CryptoQuant, IBKR, Coinbase, Alpaca (forbidden) |

## 3. Quant and doctrine

### 3.1 Net expectancy and sizing

$$EV_{\text{net}} = (\text{Win}\% \times \text{AvgWin}) - (\text{Loss}\% \times \text{AvgLoss}) - (\text{Fee} + \text{Spread} + \text{Slippage})$$

- **Measured state (2026-10-17):** paper `auto_claude` n=10, EV −0.65; replay negative in every pre-registered cell on 4h and 1d; gross R before costs ≈ 0. Costs ≈ 0.12R per round trip on 4h and ≈ 0.05R on 1d ([2026-10-17-ledger-cause-and-replay.md](2026-10-17-ledger-cause-and-replay.md)). No signal has a proven positive EV, so a strict "EV > 0 to enter" gate passes nothing. That is the current state: auto-decide off, EV gate latched.
- **EV gate:** latches on a block and never releases on a thin sample; only the owner resets it. It must not be loosened.
- **Risk per trade:** at most 1% of equity at the stop (`HARD_MAX_RISK_PER_TRADE_PCT = 1.0`). Exists.
- **Fractional Kelly:** only as a ceiling once a positive edge is measured on fresh data. With today's numbers Kelly is zero. Not implemented, not needed now.
- **Stop:** at least 1.5× ATR (`ATR_STOP_MULT`). Exists.
- **No required take-profit.** The owner rule is no fixed TP. `profit_lock` moves the stop to breakeven + 20 bps at 0.5R and trails 1R after 1R. The stop only tightens.

### 3.2 Analyzer and the "believability" idea

- **Current analyzer (locked):** EMA20/EMA200 stack with a 1.5% gap for trend, RSI band, ATR chaotic check, regimes `trend_up` / `trend_down` / `range` / `chaotic`, floors 0.55 / 0.70. MTF code exists and is on.
- **Fixed weights (50/30/20) are dropped.** Believability in Dalio's sense means weight by track record. The shadow veto (#194) measures that: net R per verdict and per reason category, on forward data, under a pre-registered pass rule. Weights come from that log, not from a prompt.
- **Indicator lists (Heikin-Ashi, ADX, Parabolic SAR, Bollinger/Keltner/Donchian, OBV, A/D, CMF, POC, VWAP, MACD, Stochastic, CCI, MFI):** not to be added. Columns `vwap` and `adx_14` exist but are never computed. Adding any of them changes the locked formula and needs a pre-registered study on data after 2026-10-17. The 2026-10-17 entry-gate audit found that extra filters raised win rate and lowered EV.
- **On-chain flows / exchange reserves:** no data source. The usual vendors are forbidden. Not available.
- **Derivatives:** funding and OI are ingested (Binance Futures public, CoinGlass when keyed) and used by market safety. Percentile history is short (BTC/ETH since 2026-09-24, ADA/DOGE/XRP since 2026-10-08). Use them only as shadow-veto inputs until enough history exists. CoinGlass V4 endpoint names in the earlier prompt are unverified.
- **Sentiment:** Fear & Greed and Altcoin Season are Home context only. They never filter, size, or move confidence (`AGENTS.md`). VIX/SPX have no writer.

### 3.3 Guardrails and veto rules

| Rule | Status in Jarvise |
| --- | --- |
| Crowding and hype are context, never a trigger | Matches doctrine. Keep. |
| Token audit veto (`riskLevelEnum`, honeypot, sell tax) | Not applicable: the universe is liquid majors on Binance spot, with no Web3 token-audit source. Revisit only if a non-major token is ever added. |
| Tokenized stock halts | Not applicable: no tokenized stocks. `paper_equity` (SPY, QQQ) is only a preset. |
| Daily loss cap, drawdown lock, MTM halt, kill switch | Exists. Live flatten is dormant while live is off. |
| Written review before resuming | Partly: the EV gate latch needs an explicit owner reset. Candidate: require a reason text on reset and store it as a decision log in `jarvise.db`. |
| Correlation of book | Static bucket cap exists (`max_correlated_bucket_pct`). Candidate: realized correlation from stored closes, shown on Ops as information only. |
| Output contract | `analysis_output` already has regime, action, confidence, invalidation, size, thesis. Candidate: 2–3 scenarios with their invalidation on the recommendation card (display only). |

## 4. Market Context PWA

Already shipped: Market Context card and Fear & Greed modal (#162/#163/#165), mobile PWA (#147/#148), 44 px tap targets (#168), service worker v2 with a network-first shell (#166). `AGENTS.md` UX rules apply: F&G is Home context only, desktop modals are centered dialogs with backdrop blur, no raw JSON on owner surfaces, modern minimal control-room look.

| Earlier spec | Decision |
| --- | --- |
| `[Static_Definition] + [Dynamic_Market_Cause] + [System_Action_Suggestion]` | Drop `System_Action_Suggestion`. #167 removed trade advice from F&G on purpose. Keep definition + cause only. |
| Five score bands with fixed colors, "50 = Neutral" | Use the classification label that the source returns (Alternative.me `value_classification`) instead of recomputing bands. A one-point neutral band is not reliable. |
| 48 × 48 px touch targets | Jarvise uses 44 px (`h-11`). Keep unless the owner asks. |
| Mobile bottom sheet, desktop popover | Mobile bottom sheet is a candidate. Desktop stays a centered dialog per `AGENTS.md`. |
| `navigator.vibrate(15)` | Optional. iOS Safari does not support it, so it must be a no-op there. |
| "ไปที่ Approval Queue ↑" shortcut | Small candidate. |
| Count-up animation and neon glow | Low priority; conflicts with the minimal look. |
| Stale-while-revalidate data cache | Not for market data. The shell is network-first and only icons/manifest use stale-while-revalidate. Stale prices on an approval screen are a safety risk. |

## 5. Verification

- Python: `.venv\Scripts\ruff.exe check src tests`, `ruff format --check src tests`, `.venv\Scripts\python.exe -m mypy src`, `.venv\Scripts\python.exe -m pytest`.
- Web (when `web/` changes): `cd web; npm run lint; npm test; npm run build`.
- Tests that must keep passing: 1% risk at stop, stop ≥ 1.5× ATR, FLAT on unsafe data, EV gate latch, kill switch.
- Do not add Playwright to the repo (`npm install -D` prunes it; keep ad-hoc E2E outside the repo).

## 6. Candidate work, in order

Small, no trading-logic change:

1. Reason text required when resetting the EV gate latch (and the kill switch), stored as a decision log.
2. 2–3 scenarios with invalidation on the recommendation card (display only).
3. "ไปที่ Approval Queue ↑" shortcut and a mobile bottom sheet for the F&G detail.

Shadow-veto inputs, batched into one `PROMPT_VERSION` bump after the first v1 window, because any prompt change restarts the count:

- **(4)** Cost-to-R budget: (fee + spread + slip) ÷ stop distance, flagged above a stated threshold.
- **(5)** Static FOMC/CPI calendar as event-risk context.
- **(6)** Funding/OI percentiles once each symbol has enough history.

Information only:

- **(7)** Realized correlation of the book on Ops.

Not to do: Prisma/Next.js rebuild, fixed layer weights, new indicators in the analyzer, F&G or Altseason as a filter or sizing input, a required take-profit, on-chain vendors, IBKR, TradingView order path.
