# Jarvise analytics stack

Exported from NotebookLM notebook [Jarvise : Crypto Trader](https://notebooklm.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e) on 2026-09-21 (`chatoe@gmail.com`).

Drive sync: **0 Drive sources, 0 stale**. Web/generated sources (94) were already indexed. This folder is the local analytics extract for implementation.

## Layers

1. **Price-graph** — OHLC, ATR (≥1.5× for stops), EMA/SMA, ADX, RSI/MACD/Stoch/CCI as context, VWAP/OBV/A-D.
2. **Order book** — spread, depth, walls vs prints, slippage. Venue: Binance TH spot.
3. **On-chain / derivatives** — CryptoQuant flows/reserves, Glassnode supply/realized cap, CoinGlass OI/funding/liquidations/ETF flows, CMC Fear & Greed + dominance.
4. **Performance** — EV after fees/slippage, Sharpe/Sortino, max DD, kill-switch. Output: regime, 2–3 scenarios + invalidation, confidence 0–1, fractional Kelly.

## Files

| File | Use |
|---|---|
| `api-map.json` | Provider endpoints and doctrine rules |
| `mvas-schema.sql` | Minimum viable tables |
| `sources/jarvise-doctrine.txt` | Owner-protection rules |
| `sources/jarvise-analyzer-stack.txt` | Analyzer layer spec |

## First build (doctrine)

Paper analytics + analysis output. Not live execution, not oscillator bots, not full Kelly.
