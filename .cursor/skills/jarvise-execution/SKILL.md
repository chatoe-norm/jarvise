---
name: jarvise-execution
description: Check Jarvise paper order flow and the gated live door. Use when enqueueing, approving, rejecting, or expiring paper approvals, reading a paper fill, or asking whether live trading is on.
---

# Jarvise execution

Paper execution already lives in `jarvise_paper`. The live path in `jarvise_trade` stays closed while `live_trading_enabled()` is false.

## Where to look

- [`src/jarvise_paper/approval.py`](../../../src/jarvise_paper/approval.py)
  - `enqueue_approval(...)`
  - `approve_approval(...)`
  - `reject_approval(...)`
  - `expire_approvals(conn, *, now_ms=None)`
- [`src/jarvise_paper/engine.py`](../../../src/jarvise_paper/engine.py)
  - `fill_price(...)` for the simulated fill
  - `effective_slip_bps(...)` and `fee_usd(...)` for cost
- [`src/jarvise_trade/flags.py`](../../../src/jarvise_trade/flags.py)
  - `live_trading_enabled() -> bool`
- Live submit, reconcile, and flatten in `src/jarvise_trade` run only when that flag is true and the owner checklist is done. Do not call `place_spot_market_order` from this skill while the flag is false.

## UI to read, not rebuild

- `App` → `AppShell` → `HomePage` (`ApprovalQueue`, `RecommendationCard`, `ConfidenceLadder`, `KpiStrip`, Fear & Greed as context)
- `PaperPage` for the closed-ledger loss lesson
- `DecisionsPage`, `ExchangePage`
- `OpsPage` for the kill switch and the paper risk monitor

Do not add `TradingCommandDashboard`, a drawdown review modal, or a 1:3 calculator.

## Do not

- Turn on `JARVISE_LIVE_TRADING`.
- Add Coinbase, Alpaca, IBKR, or a TradingView webhook as an order source.
- Swap `paper_liquid` or add fiat and cross pairs.
- Treat research notes in `docs/research/` as a paper or live order.
