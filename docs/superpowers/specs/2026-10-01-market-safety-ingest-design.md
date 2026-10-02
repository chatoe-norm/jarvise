# Jarvise — market safety ingest (CoinGlass + book + macro) — design

**Date:** 2026-10-01  
**Status:** APPROVED  
**Parent:** Capital preservation / positive EV doctrine  
**Depends on:** P0 ingest + paper ledger + `jarvise_risk` kill-switch

## Goal

Extend controlled GET-only ingest so derivatives (CoinGlass), order-book spreads/depth (Binance public), and BTC dominance / global market cap (CoinGecko) land in SQLite. A market-safety gate forces **FLAT**, blocks enqueue/approve/live submit on unsafe/stale/anomalous data, and engages Redis kill-switch only for **critical** failures when the gate is enabled.

## Non-goals

- TradingView HTML/API scrape or writing TV into `market_technicals` / paper fills
- Websocket tick books
- Spoof-wall heuristics, fear/greed, ETF flows (leave NULL)
- Changing `JARVISE_LIVE_TRADING` defaults

## Locked decisions

1. **Sources:** CoinGlass V4 (derivatives), Binance public `bookTicker` + `depth` (microstructure), CoinGecko `/global` (dominance + market cap). TV MCP stays research overlay only.
2. **Failure policy:** Fail-closed trading (FLAT + block) for **required** streams. Kill-switch only when the gate is enabled and a **required** stream fails critically. `--skip-derivatives` / `--skip-book` / `--skip-macro` (and `JARVISE_MARKET_SAFETY_REQUIRE_MACRO=0`, the default) exclude that stream from kill-switch trips — e.g. a CoinGecko `/global` 400 must not engage KS when macro is optional.
3. **Cadence:** Existing ingest / n8n poll — not realtime websockets.
4. **EV:** Spread-aware paper slip `max(5 bps, half bid-ask spread in bps)` so expectancy includes realistic spread cost.

## Schemas

- `derivatives_analytics` — existing; fill `long_short_ratio` when CoinGlass provides it
- `order_book_microstructure` — writer for spread + ±1% depth USD
- `macro_onchain_sentiment` — writer for `btc_dominance_pct` + `global_market_cap_usd` (migration)

## Gate thresholds (env `JARVISE_MARKET_SAFETY_*`)

| Check | FLAT + block | Kill-switch if gate on |
|-------|--------------|------------------------|
| Required provider HTTP/parse fail | yes | yes |
| Optional provider HTTP/parse fail (e.g. macro when not required) | no | no |
| Both sided depth &lt; floor USD | yes | yes |
| Spread &gt; max bps | yes | no |
| Required CoinGlass metrics null | yes | yes |
| Book/deriv stale beyond max age | yes | yes |

Gate default **on** (`JARVISE_MARKET_SAFETY=1`). Never auto-clear kill-switch.

## Surfaces

- `jarvise ingest` — book + macro flags; safety summary in JSON
- `analyze_snapshot` / paper run — veto to FLAT
- `enqueue_approval` — refuse unsafe non-flat actions
- `jarvise_paper.engine.fill_price` — spread-aware slip
