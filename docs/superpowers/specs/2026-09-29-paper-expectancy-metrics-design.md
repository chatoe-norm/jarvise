# Jarvise — paper expectancy metrics — design

**Date:** 2026-09-29  
**Status:** Shipped (this PR)  
**Depends on:** P4 paper ledger + approval fills (`paper_orders` with `open_*` / `close_*` reasons)  
**Plan:** [`../plans/2026-09-29-paper-expectancy-metrics.md`](../plans/2026-09-29-paper-expectancy-metrics.md)  
**Related:** `PROJECT_CONTEXT.md` §5.3

## Locked decisions

1. Surfaces **equal peers:** `jarvise paper metrics` and `/analytics` + `/api/paper/metrics`
2. Closed trade = **round-trip** (`open_*` → matching `close_*`); not equity-only
3. Sharpe/Sortino **null** until ≥30 closed trades
4. Compute **on demand**; persist only with CLI `--persist` or web POST refresh
5. Approach: pure `jarvise_paper/metrics.py` (no new trades table)

## Formulas

- Trade PnL = gross (long: close−open; short: open−close) × qty − open fee − close fee  
- EV = mean closed PnL; win rate = wins / (wins+losses); scratches excluded from win rate  
- Max drawdown % from equity curve `start + Σ closed PnL`  
- Sharpe/Sortino from per-trade returns `pnl / STARTING_PAPER_EQUITY` when n≥30  

## Out of scope

Replay/backtest, risk caps, live, n8n metrics job
