# Autotrade stack gap vs Jarvise

**Research overlay — not trading doctrine.** Do not index this page, the research notebook, Freqtrade/ccxt/Nautilus/Hummingbot vendor docs, GitHub issues, or Reddit into Qdrant `jarvise_doctrine`. Do not silent-merge into SQLite or the paper ledger.

This is a **gap report**, not a build list. Do not start P5, do not set `JARVISE_LIVE_TRADING=true`, do not add ccxt/Freqtrade/Nautilus as dependencies, and do not retune analyzer floors or `same_side_hold` to raise fill frequency.

Queried **2026-10-05**. NotebookLM profile `chatoe`.

| Surface | Id / URL |
|---------|----------|
| Doctrine (rules only; not a stack source) | [`jarvise`](https://notebook.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e) `14e11c63-e2ee-4b49-898f-b0cc4c61cb4e` |
| Research notebook (this comparison) | [Jarvise autotrade stack gap](https://notebook.google.com/notebook/46cb8581-a191-4a90-a359-6e82dd1d4c04) `46cb8581-a191-4a90-a359-6e82dd1d4c04` |

Official URLs added to the research notebook: Freqtrade docs + stoploss, ccxt README, NautilusTrader architecture, Hummingbot docs + strategies, Binance Spot REST + user-data-stream, [Stooq historical data](https://stooq.com/db/h/), [IBKR TWS API](https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/). Deep research also imported cited pages (Nautilus overview, Binance local order book, Freqtrade configuration, ccxt Manual). **Weight official docs only.** Ignore GitHub issues, Reddit, Smithery, and release-note spam that landed in the same import.

---

## Closed `auto_*` n=0 is not a missing library

As of 2026-10-05, closed paper `auto_*` is still **n=0** while **BTC and ETH `auto_claude` positions are open** with stops. That is expected under current rules:

- One open position per symbol; same-side longs resolve as `auto:rule:same_side_hold` (no pyramiding) in [`src/jarvise_paper/auto_decide.py`](../../src/jarvise_paper/auto_decide.py).
- Capital preservation prefers FLAT / hold over extra activity ([doctrine ladder](https://notebook.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e): paper → manual approval → autonomy).
- Multi-month copy-DB replay (`--paper-policy rules`) remaining EV-negative must not drive floor/rule retunes.

A Freqtrade-style extra strategy plugin would not close those legs. Wait for stops / `flat_exit` / owner reject, then prove EV on **closed** `auto_*`.

---

## Doctrine vs industry docs (what we asked each)

**Doctrine notebook** (system rules, not Python libraries): paper fills first; human approval before live; autonomy only with kill-switch, spend caps, and confidence filters; capital preservation over activity; FLAT on low confidence; 0.25–1% equity risk; stops ≥ ~1.5× ATR; drawdown halt then **written** human review (never auto-resume); notebook is doctrine, not a live book; live keys view+trade only, no withdraw/transfer.

**Industry docs** describe *how production bots talk to venues*: local book sync, user-data WebSocket, LIMIT/OCO, `clientOrderId`, on-exchange stoploss, unified adapters, RPC/UI. Those are capability catalogs. They do **not** override Jarvise’s ladder.

---

## Jarvise inventory (from code, 2026-10-05)

**Declared deps** ([`pyproject.toml`](../../pyproject.toml)): `httpx`, `typer`, `pydantic`, `cryptography`. Optional: FastAPI/uvicorn, Redis, Qdrant, sentence-transformers. **Not present:** ccxt, Freqtrade, NautilusTrader, Hummingbot, TA-Lib, pandas as a runtime requirement.

| Package | Role today |
|---------|------------|
| `jarvise_ingest` | GET-only writers: Binance klines, `bookTicker`+depth, CoinGecko global, Alternative.me F&G, Binance Futures / CoinGlass derivs, Stooq `GET /q/d/l/` for SPY/QQQ paper equity (not IBKR TWS). Indicators: ATR/RSI/EMA/SMA/MACD/BB recomputed from the full series. `adx_14` column exists; **not computed**. Volume stored; **not an analyzer input**. |
| `jarvise_analyze` | Regime / confidence / invalidation / size; optional HTF confirm (`JARVISE_ANALYZE_MTF`). |
| `jarvise_paper` | Ledger, approval queue, auto-decide (rules + OpenRouter), paper stops, timeout → FLAT, feedback / soft EV gate. |
| `jarvise_risk` | Fail-closed Redis kill-switch, order + portfolio caps, 1% risk-at-stop, market_safety. |
| `jarvise_exchange` | `VenueClient` protocol; Binance signed GET balances; Eterna **fixture/blocker**. |
| `jarvise_trade` | Gated P4-C: spot MARKET + protective `STOP_LOSS_LIMIT`; deterministic `newClientOrderId`; GET reconcile. **No cancel / withdraw / transfer paths.** Flag `JARVISE_LIVE_TRADING` default off. |
| `jarvise` jobs | ingest, rag-refresh, paper-run/expire/digest/auto-decide, ingest-health, risk-monitor, live-reconcile. |
| `jarvise_web` / `web/` | Command Dashboard. |
| `jarvise_notify` / `jarvise_obs` | Telegram pending (soft-fail); Prometheus text + optional Grafana. |

Ladder SoT: [`PROJECT_CONTEXT.md`](../../PROJECT_CONTEXT.md) §5.1 — paper autotrade **code complete**; live door **gated off**; unattended live (**P5**) **absent by design**.

---

## Layer comparison

| Layer | Official docs say a serious bot typically has | Jarvise today |
|-------|-----------------------------------------------|---------------|
| Market data | REST + WS klines/trades; **local L2 book** with snapshot + `U`/`u` diffs ([Binance order book](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)); `fetchOHLCV` ([ccxt](https://docs.ccxt.com/)) | REST klines + REST book snapshots. No WS book sync, no user-land tick bars. |
| Signals | Strategy callbacks / pandas+TA-Lib (Freqtrade); Hummingbot controllers; Nautilus `Strategy` | Deterministic analyzer + doctrine RAG + LLM reviewer. No strategy plugin runtime. |
| Risk | Pre-trade `RiskEngine` (Nautilus); Freqtrade `stoploss_on_exchange` / `emergency_exit`; stake buffers | Kill-switch, caps, market_safety, paper stops, live protective stop on Approve. |
| OMS / execution | LIMIT, MARKET, STOP_*, OCO, TIF, `unfilledtimeout`, STP ([Binance Spot REST](https://developers.binance.com/docs/binance-spot-api-docs/rest-api)) | Paper OMS. Live: MARKET + STOP_LOSS_LIMIT + `clientOrderId`. No LIMIT/OCO/TIF timeout loop. |
| Reconcile | User data `executionReport` + REST query; cancel on timeout (Freqtrade `unfilledtimeout`) | GET `/api/v3/order` reconcile. Cancel of Jarvise's own stops + kill-switch flatten (gated, 2026-10-05). **No live flatten on approval timeout.** |
| User stream | `listenKey` + keep-alive + private WS ([user-data-stream](https://developers.binance.com/docs/binance-spot-api-docs/user-data-stream)) | **Absent.** |
| Multi-venue | ccxt unified API; Nautilus `DataClient`/`ExecutionClient`; Hummingbot connectors | `VenueClient` RO + one Binance adapter. No execution router. |
| Autonomy | Bot process places live orders on schedule (Freqtrade dry-run vs live) | Paper scheduler + auto-decide. **No P5 live scheduler.** |
| Ops | Logs, Telegram `/start` `/stop` `/forcebuy`, FreqUI, systemd notify | Jobs + Dashboard + Telegram **alerts** (not force-trade RPC). `/metrics` + Grafana profile. |

---

## Bucket A — already present

Do not install a second copy of these.

- Closed-candle OHLCV ingest and full-series indicator recompute.
- Paper OMS: enqueue → approve/reject/expire → simulated fill, fees/slip, one position per symbol.
- Kill-switch (fail-closed), daily loss / drawdown lock, portfolio caps, market_safety FLAT.
- Paper stop monitor + 1% equity risk-at-stop.
- Approval queue with TTL; paper timeout → `timeout_flat`.
- Auto-decide with doctrine snippets and Claude confirm (paper only; refuses when live is on).
- Idempotent live `newClientOrderId` and GET fill reconcile (when live path is used).
- Read-only spot balances; key permission probe (no withdraw-scoped keys).
- Observability: job single-flight locks, Prometheus, optional Grafana, Telegram pending digest.
- Owner UI: Command Dashboard (Home / Paper / Decisions / Exchange / Ops).

---

## Bucket B — implemented but gated or stubbed

| Item | Where | Why it is not “missing” |
|------|--------|-------------------------|
| Live spot MARKET on Approve | `jarvise_trade` | `JARVISE_LIVE_TRADING` default **false**; not smoked as owner live. |
| Live protective `STOP_LOSS_LIMIT` | `submit.py` | Same gate. Fee-net and `LOT_SIZE`-floored since 2026-10-05. |
| Live kill-switch flatten | `jarvise_trade/flatten.py`, risk-monitor job, `jarvise trade flatten` | Same gate. Cancels Jarvise's own stops by client id, then one idempotent MARKET SELL per symbol ([spec](../superpowers/specs/2026-10-05-live-killswitch-flatten-design.md)). |
| P5 unattended live scheduler | roadmap | **Absent by design** until P4-C is stable after paper EV. |
| Eterna as second venue | `eterna_spot.py` | Fixture/blocker; no safe GET-only REST. |
| HTF / MTF confirm | `analyze_mtf` | Code on; VPS sampling may set `JARVISE_ANALYZE_MTF=false`. |
| Soft EV gate | `auto_ev_gate` | Needs ~10+ closed `auto_*` before it is meaningful. |
| `OPENCLAW_PAPER_ONLY` | compose / skills | Convention only — not enforced in Python ([`PROJECT_CONTEXT.md`](../../PROJECT_CONTEXT.md)). |

---

## Bucket C — serious autotrade server functions Jarvise does not have

Each row: **function**, **why official docs treat it as core**, **Jarvise**, **do not build this sprint** if it skips the paper-EV ladder.

| Function | Why docs call it core | Jarvise | Build now? |
|----------|----------------------|---------|------------|
| **Private user-data WebSocket** (`listenKey`, `executionReport`, balance updates) | Binance user-data-stream: live fill/balance truth without polling | None | No — not needed to close paper `auto_*`. After gated live smoke, consider as P4-C hardening. |
| **Local WS order book** (snapshot + diff `U`/`u`) | Binance “manage a local order book correctly”; Nautilus book actors | REST `bookTicker`/depth snapshots only | No — market_safety already fail-closes on stale/unsafe book. |
| **Live flatten on approval timeout** | Freqtrade `unfilledtimeout` + emergency market exit | Paper timeout flattens paper. **Kill-switch flatten shipped 2026-10-05 (gated, Bucket B)**; live has no venue flatten on approval TTL ([`PROJECT_CONTEXT.md`](../../PROJECT_CONTEXT.md) §5.1) | No — the kill-switch path covers the doctrine halt. Revisit with P5. |
| **LIMIT / OCO / TIF OMS** | Binance Spot REST order types; ccxt `createOrder`; Freqtrade TIF | Live MARKET + stop-limit only | No — spot MARKET matches current P4-C design. |
| **Unattended live scheduler (P5)** | Freqtrade live vs dry-run; Nautilus live node | Absent | **No.** Prove closed `auto_*` EV, then checklist live smoke, then a separate P5 spec. |
| **Second-venue execution adapter / smart routing** | ccxt unified API; Nautilus ports; Hummingbot connectors | RO `VenueClient` + Binance only | No. Next venue = GET-only `VenueClient` first, never Eterna MCP orders. |
| **ADX and volume as analyzer triggers** | Doctrine analyzer stack names ADX/volume; Freqtrade/TA-Lib strategies use them | `adx_14` unused; volume unused in `analyze_snapshot` | Optional later; **do not** add to raise trade count while `auto_*` n=0. |
| **Remaining macro writers** (ETF flows, netflow) | Doctrine on-chain list (Glassnode/CryptoQuant class) | CoinGecko global + F&G + derivs only | Backlog; not required for paper EV sample. |
| **Python-enforced `OPENCLAW_PAPER_ONLY`** | OpenClaw audit spec; industry bots deny live tools in paper mode | Env convention only | Small safety patch later; not a missing OMS. |
| **Broker/live equity as trading book** | Freqtrade stake from exchange balance; IBKR TWS API for stocks/ETFs | Paper $10k ledger; Stooq OHLCV for SPY/QQQ only; Exchange page is RO crypto spot; **no IBKR client** | Not until live crypto path is proven. Do not add TWS for fills. |
| **Freqtrade-style forcebuy RPC / strategy plugins / TA-Lib/ccxt runtime** | Their products *are* the bot | Jarvise is a custom paper-first stack | **Do not adopt.** Overlay research only. |

---

## Dependencies: do not copy their requirements.txt

Freqtrade docs assume pandas + TA-Lib + ccxt. Nautilus assumes PyO3/Rust engine crates. Hummingbot assumes its connector package. **Jarvise already has an ingest/analyze/paper/risk/trade split on httpx.** Pulling those frameworks in would duplicate OMS and fight the kill-switch and paper-first gates.

If a future spec needs a unified REST *read* client, evaluate it as a `VenueClient` implementation — still GET-only first.

---

## What to do next (ops, not this report)

1. Let open BTC/ETH paper legs resolve (stop / flat_exit / time). Do not pyramid.
2. Collect ~10 closed `auto_*` and read `by_decision_source_30d`.
3. Only then: optional gated live checklist (now includes the kill-switch flatten smoke). Only after that: user-data stream. Only after that: P5.

---

## Sources (official, cited in research query)

- [Freqtrade](https://www.freqtrade.io/en/stable/), [Stoploss](https://www.freqtrade.io/en/stable/stoploss/)
- [ccxt Manual](https://docs.ccxt.com/)
- [NautilusTrader architecture](https://nautilustrader.io/docs/latest/concepts/architecture), [overview](https://nautilustrader.io/docs/latest/concepts/overview/)
- [Hummingbot](https://docs.hummingbot.org/)
- [Binance Spot REST](https://developers.binance.com/docs/binance-spot-api-docs/rest-api), [user data stream](https://developers.binance.com/docs/binance-spot-api-docs/user-data-stream), [local order book](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)
- [Stooq historical data](https://stooq.com/db/h/) (Jarvise uses public CSV `GET /q/d/l/` for paper equity bars)
- [IBKR TWS API](https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/) (industry live equity OMS — **not** wired)

Related Jarvise maps (also overlays): [`docs/exchange/binance-for-jarvise.md`](../exchange/binance-for-jarvise.md), [`docs/exchange/eterna-for-jarvise.md`](../exchange/eterna-for-jarvise.md), [`docs/market-data/coingecko-for-jarvise.md`](../market-data/coingecko-for-jarvise.md).
