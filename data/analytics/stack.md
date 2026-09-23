# Jarvise analytics stack

Exported from NotebookLM notebook [Jarvise : Crypto Trader](https://notebooklm.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e) on 2026-09-22 (`chatoe@gmail.com`).

Drive sync: **0 Drive sources, 0 stale**. Web/generated sources indexed in the notebook (~97). This folder is the local analytics extract for implementation.

## Layers

1. **Price-graph** — Structure first; OHLC / Heikin-Ashi context; ATR (≥1.5× for stops); EMA/SMA, ADX, Parabolic SAR with trend; RSI/MACD/Stoch/CCI as context only; VWAP/OBV/A-D; Bollinger/Keltner/Donchian for squeeze vs expansion.
2. **Order book** — spread, depth, walls vs prints, slippage. BTC thicker; ETH thinner with more spoof wicks. Venue: Binance TH spot. Jarvise’s Binance API usage is market data (public klines) plus planned read-only spot balances — not order execution.
3. **On-chain / derivatives** — CryptoQuant flows/reserves, Glassnode supply/realized cap, CoinGlass OI/funding/liquidations/ETF flows, CMC Fear & Greed + dominance (spot context even without futures).
4. **Performance** — EV after fees/slippage, Sharpe/Sortino, max DD, kill-switch. Output: regime, 2–3 scenarios + invalidation, confidence 0–1, fractional Kelly. Below threshold → FLAT.

## Files

| File | Use |
|---|---|
| `api-map.json` | Provider endpoints and doctrine rules |
| `mvas-schema.sql` | Minimum viable tables |
| `sources/jarvise-doctrine.txt` | Owner-protection rules |
| `sources/jarvise-analyzer-stack.txt` | Analyzer layer spec |
| `sources/binance-api-intro-jarvise.txt` | Binance API intro → Jarvise allowlist/denylist |
| `sources/binance-skills-hub-jarvise.txt` | Binance Skills Hub adopt/exclude/deny matrix → skill `jarvise-binance-intel` (read-only, no keys) |
| `sources/notebook/` | NotebookLM export via `jarvise rag sync-notebook` |

## First build (doctrine)

Paper analytics + analysis output. Not live execution, not oscillator bots, not full Kelly. Deployment ladder remains paper → manual approval → autonomy.
