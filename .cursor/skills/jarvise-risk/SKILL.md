---
name: jarvise-risk
description: Check Jarvise position size, stop distance, daily loss, correlation bucket, market safety, and the kill switch. Use when sizing a paper trade, validating a stop, or reading a risk-cap breach.
---

# Jarvise risk

Size, stops, and halts already live in Python. Use those functions. Do not raise the 1% risk ceiling and do not add a 1:3 take-profit requirement.

## Where to look

- [`src/jarvise_risk/caps.py`](../../../src/jarvise_risk/caps.py)
  - `load_risk_caps() -> RiskCaps`
  - `check_caps(...)` and `check_portfolio_caps(...)`
  - `risk_at_stop_pct(*, size_pct_equity, entry, stop, round_trip_cost_bps=0.0) -> float`
  - `max_risk_per_trade_pct() -> float` capped by `HARD_MAX_RISK_PER_TRADE_PCT` (`1.0`, meaning 1% of equity)
  - `symbol_bucket(symbol)` with `max_correlated_bucket_pct` for correlated notional. BTC and ETH share the `crypto_majors` bucket. That is a notional cap, not a ban on holding both.
- [`src/jarvise_risk/market_safety.py`](../../../src/jarvise_risk/market_safety.py)
  - `load_market_safety_config()`
  - `evaluate_market_safety(...) -> MarketSafetyResult`
  - `apply_safety_to_analysis(...)`
  - `maybe_engage_kill_switch(safety)` only on a critical safety failure when market safety is enabled
- [`src/jarvise_paper/stops.py`](../../../src/jarvise_paper/stops.py)
  - `evaluate_stop`, `run_stop_monitor`, `remaining_stop`
  - Stop distance uses `ATR_STOP_MULT` (`1.5`) from `jarvise_analyze.engine`.
- Expectancy gate: `auto_ev_gate_status` in [`src/jarvise_paper/feedback.py`](../../../src/jarvise_paper/feedback.py), driven by `JARVISE_AUTO_DECIDE_MIN_EV` and `JARVISE_AUTO_DECIDE_MIN_EV_N`. The first block latches in `paper_ev_gate_latch` until `jarvise paper ev-gate reset --confirm`. A thin sample does not release it.

## Do not

- Raise risk at the stop above 1% of equity. The 1%–2% wording in research notes does not lift this ceiling.
- Block simultaneous BTC and ETH positions beyond the existing bucket cap.
- Treat Fear & Greed as a reason to size up.
- Auto-resume a drawdown halt or a kill switch without the owner.
