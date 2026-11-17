# Exit-rule experiment (pre-registered, not implemented)

สถานะ: เจ้าของล็อกกติกานี้ไว้ก่อนรัน เอกสารนี้บันทึกแผนเท่านั้น โค้ดยังใช้กติกาปัจจุบัน และปุ่ม 🛡 ไม่ถูกเพิ่มในการ์ด 07:30

This note freezes the comparison before any replay or live sample. It does not change `profit_lock`, the trail, the triggers, or the price source the assistant uses today.

## Current rule (unchanged)

- Breakeven when the **wick** (high for a long, low for a short) of a **closed 1h spot** bar touches `+0.5R` (`DEFAULT_BREAKEVEN_R = 0.5` in `src/jarvise_paper/profit_lock.py`).
- Trail when that same wick touches `+1R` (`DEFAULT_TRAIL_AFTER_R`).
- Those bars are spot klines (`binance_klines`, `api.binance.com`). The exchange stop triggers on the futures mark.
- The entry stop stays `1.5 × ATR`. The stop only tightens. There is no 1:3 take-profit.
- Progress cards and the ⚠️ early-exit button use the futures mark. ⚠️ stays discretionary: the card does not show a score. Offers and taps are logged.

## Price source for any future rule

Future rules use **futures 1h closes**. The mark is only for progress cards and ⚠️. Do not score a future rule on spot wicks.

## Variants (do not implement here)

Primary, against the current rule:

- **A.** Breakeven only after a futures 1h **close** `>= +1R`. Trail after a futures 1h **close** `>= +1.5R`.

Secondary, not the decision cell:

- **B.** Step on futures 1h closes: `+0.5R` moves the stop to `-0.5R` risk, `+1R` moves it to breakeven, then the trail.
- **C.** Hold to the plan. No breakeven move.
- **D.** Descriptive only. A 23:25 card offering to reduce the stop when the trade is `>= +0.5R`. Not a pass/fail cell.

`ExitPolicy` refactor is deferred until a variant has passed.

## Decision

Parameters below are locked now. Do not retune them after seeing the sample.

1. Replay about **60 existing signals** first, current rule vs A, on the same fills.
2. **Stop** if A beats the current rule by **less than +0.10R per trade**. Do not collect the live sample.
3. Otherwise collect **25–30 live trades**, at least **3 coins** and at least **3 weeks**.
4. **Pass** when the live mean is `>= +0.10R` per trade and `P(A better than current) >= 80%`.
5. Guardrails, both must hold: worst trade `>= -1.15R`, and drawdown no worse than the current rule's drawdown **+ 1.5R**.

Report every figure **with and without** the 2026-11-17 BNB short.

## What this change stored for that replay

Migration 32 adds snapshots a later replay cannot rebuild:

- each advice card (kind, time, mark, wick, 1h spot close, R now, suggested stop, approximate R after the 5 bps/side taker fee, distance from mark)
- owner tap, ignore, or the 07:30 manual-move note, with latency
- exchange stop history
- exit source on `assist_real_trade.exit_source` (`initial_stop`, `be_stop`, `trail_stop`, `tp`, `manual`, `early_exit`, and the other labels)
- ATR at entry on the watch row and copied onto the real trade

Realised fees and funding stay the existing `commission_usdt` and `funding_usdt` columns. ⚠️ offers and taps are rows of their own. Nothing here places, moves, or cancels an order.
