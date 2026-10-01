# Market safety ingest — implementation checklist

**Date:** 2026-10-01  
**Spec:** [2026-10-01-market-safety-ingest-design.md](../specs/2026-10-01-market-safety-ingest-design.md)

## Tasks

- [x] Design spec
- [x] Providers: `binance_book`, `coingecko_global`; CoinGlass L/S when available
- [x] DB writers + `global_market_cap_usd` migration; ingest CLI `--skip-book` / `--skip-macro`
- [x] `jarvise_risk.market_safety` + analyze / enqueue / ingest wiring
- [x] Spread-aware paper slippage
- [x] Unit tests + PROJECT_CONTEXT / product-usage / `.env.example`
