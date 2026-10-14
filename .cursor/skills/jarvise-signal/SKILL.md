---
name: jarvise-signal
description: Check Jarvise entry and exit signals on closed candles. Use when reading analyzer output, confidence floors, FLAT decisions, auto-decide, or whether a setup should be queued.
---

# Jarvise signal

Entry and exit decisions already live in Python. Use those functions. Do not add a new signal formula, and do not adopt the shallow EMA20 pullback filter.

## Where to look

- `analyze_snapshot(candle, *, confidence_threshold=CONFIDENCE_THRESHOLD) -> dict` in [`src/jarvise_analyze/engine.py`](../../../src/jarvise_analyze/engine.py). `CONFIDENCE_THRESHOLD` is `0.55`. Below that, the action is `flat`.
- `analyze_mtf(ltf_candle, htf_candle, *, confidence_threshold=CONFIDENCE_THRESHOLD) -> dict` exists in the same file. Leave `JARVISE_ANALYZE_MTF` off. A short daily history reads as chaotic and forces `FLAT`.
- `run_auto_decide(conn, *, now_ms=None, kill_switch=False, kill_switch_check=None, config=None, chat=chat_json, doctrine_lookup=None) -> dict` in [`src/jarvise_paper/auto_decide.py`](../../../src/jarvise_paper/auto_decide.py). Doctrine-free approval stays at `0.70` (`NO_DOCTRINE_MIN_CONF`). One open position per symbol (`same_side_hold`) stays as written.

## Market safety before entry

Low volume, missing or stale books, and wide spreads are reasons for `FLAT` through [`src/jarvise_risk/market_safety.py`](../../../src/jarvise_risk/market_safety.py) `evaluate_market_safety` and `apply_safety_to_analysis`. Fear & Greed is context on Home. It does not change the signal.

## Do not

- Loosen `0.55` / `0.70` or change `same_side_hold` to raise fill count.
- Treat TradingView, Glassnode, CryptoQuant, or the research notes in `docs/research/` as an order source.
- Index those notes into Qdrant `jarvise_doctrine`.
