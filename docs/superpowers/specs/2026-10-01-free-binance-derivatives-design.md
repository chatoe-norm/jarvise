# Free-first derivatives (Binance Futures fallback) — design

**Date:** 2026-10-01  
**Status:** APPROVED  
**Parent:** Market safety ingest / capital preservation

## Goal

Do not require `COINGLASS_API_KEY` for derivatives context. Default to Binance Futures public GET (funding + open interest). Use CoinGlass only when a key is present (richer liquidations / L/S).

## Locked decisions

1. Router: `jarvise_ingest.providers.derivatives.fetch_derivatives` → CoinGlass if key else Binance Futures.
2. Binance path leaves `liquidations_24h_usd` and `long_short_ratio` null; market safety accepts funding present.
3. `--skip-derivatives` remains the explicit off switch.
4. Scheduled jobs ingest no longer passes `--skip-derivatives`.

## Non-goals

CMC / CoinLore / DIA for derivatives; scraping CoinGlass without a key; live trading.
