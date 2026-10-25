# Forward test: daily Donchian long with trail (pre-registered 2026-10-25)

**Research overlay — not trading doctrine.** Do not index this page into Qdrant `jarvise_doctrine`. Nothing here changes `analyze_snapshot`, `auto_decide`, the paper ledger, the shadow-veto window, floors 0.55/0.70, the 1.5× ATR stop, `flat_exit`, `same_side_hold`, `profit_lock`, the EV gate, or `JARVISE_LIVE_TRADING`.

## Why

The daily long-only study (§3 of `2026-10-17-ledger-cause-and-replay.md`) failed, and it still fails after the stop-fill correction (`2026-10-25-stop-fill-audit.md`). One reference cell, `donchian_long` with `--stops trail`, came out ≥ 0 in all three historical windows after the correction:

| Window | Closed trades | EV (USD per trade) |
| --- | --- | --- |
| W0 | 88 | 0.508 |
| W1 | 109 | 1.371 |
| W2 | 86 | 0.504 |

That cell was picked after seeing results, so the history cannot confirm it. This page registers it as hypothesis **H3** and tests it only on candles that close after this page was written. It is long-only, so it fits spot live trading.

## Frozen rule

- **Strategy:** `jarvise analyze --replay --apply-paper --strategy donchian_long --stops trail --timeframe 1d --paper-policy rules`, with no `--mtf`, no `--no-breakeven`, and the default `--exit`. The code is `main` at `0775e9f` or later, with no change to `strategies.py`, `replay.py`, `stops.py`, `profit_lock.py`, or the paper engine during the window. Any change to those files restarts the count at zero.
- **Symbols:** `paper_liquid` as `jarvise universe show` reports it on the VPS at the first run. Expected: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, ADAUSDT, DOGEUSDT, XRPUSDT. The first run's list is frozen. A later universe change does not change the list.
- **Start:** `--since 2026-10-26`, the first daily candle opening after registration. Earlier candles are channel context only, as `replay.py` already does.
- **Measurement:** each daily run copies the live ledger to an isolated file and replays from the start to now. The result is a pure function of stored closed candles. Fees and slippage are the engine defaults.
- **Window end:** 20 closed trades, or 120 days after the start (2027-02-23), whichever comes first. One 60-day extension needs the owner and is not applied automatically.
- **Pass:** at least 20 closed trades, and EV ≥ 0 USD per closed trade.
- **Report only, never deciding:** mean R, win rate, max drawdown, open positions, and equal-weight buy-and-hold over the same days.
- **What a pass earns:** a proposal for size-capped live spot trading, which the owner must approve separately. Nothing is wired automatically. A fail closes H3, and no parameters, symbols, or timeframes are added.

## Expected pace

The historical cell made about 5–6 closed trades a month across the seven symbols, so 20 trades should take roughly 3–4 months. The historical edge is thin, about +0.1 to +0.2R per trade. With 20 trades this window is a check that the edge is not negative. It is not statistical proof.

## Results

None yet.
