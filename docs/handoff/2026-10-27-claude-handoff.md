# Jarvise handoff — 2026-10-27 (Claude session 2026-10-24 to 2026-10-26)

For the next Claude or Cursor session. `AGENTS.md` and `PROJECT_CONTEXT.md` are still the source of truth for rules. This page covers only what changed in this session and what is open.

## 1. What shipped (all on `main`, deployed, healthz `{"ok":true,"paper_only":true}`)

| PR | Change |
| --- | --- |
| #223 | `r_multiple` is true R: PnL ÷ (\|entry − initial stop\| × qty). Migration 13 adds `paper_orders.initial_stop_price` and `paper_decision_outcomes.return_on_notional`. Legacy rows have R = NULL. |
| #224 | Dependency bumps for new pip-audit advisories. |
| #225 | Resting-stop fill in paper and replay: fill at the stop, or at the gap open; a close already through a just-moved stop fills at the close. `shadow_track.py` keeps the old `evaluate_stop` on purpose, because it is pre-registered. |
| #226, #227 | Risk monitor skips MTM and stops when candles are stale (age measured from the candle close). One Telegram alert on the stale edge and one on recovery. Edge state lives in SQLite. |
| #228 | Disk-space alert (free < 15% or < 2 GB). Disk fields in `/healthz`. |
| #229, #230 | Forward test H3: a daily job replays `donchian_long` + trail on 1d over 7 symbols from 2026-10-26, on an isolated ledger copy. |
| #231 | Trade assistant PR A: Telegram entry signals for the owner's manual Binance USDⓈ-M futures trades. |
| #232 | Trade assistant PR B: concurrent risk cap, exchange minimums, and a position watch every 5 minutes. |
| #233 | `AGENTS.md` memory note. **Open, docs only.** This handoff is in the same PR. |

Research notes from this session:

- `docs/research/2026-10-25-stop-fill-audit.md`: root cause of the stops past −1R, the grid re-runs, and the outage.
- `docs/research/2026-10-25-donchian-trail-forward.md`: the frozen H3 rule.

Design: `docs/superpowers/specs/2026-10-25-trade-assistant-design.md`.

## 2. Key findings to remember

- **Stop fills.**
  - The old fill rule filled stops at the worst price since entry. That was 44% of the paper ledger's loss.
  - After the fix, the frozen 10-17 grid still fails: decision cell MTF on × trail, IS −0.247 / OOS −0.280.
  - The daily long-only study still fails.
  - `donchian_long` + trail was ≥ 0 in all three windows. It was picked after the fact, so it is only a forward hypothesis (H3).
- **Outage of 2026-10-06.**
  - The VPS disk filled up. The kill-switch alert reason was "database or disk is full", and a Deploy failed.
  - The risk monitor kept running on stale prices. Fixed by #227.
  - The disk is healthy now: 69% free.
- **Spot balance sync was dead from 2026-10-06 to 2026-10-26.**
  - Cause: the Ed25519 PEM disappeared from `/opt/jarvise/secrets/`. Why is unknown.
  - Fixed on 2026-10-26: a new key pair was generated on the VPS, and a new Binance key **"Jarvise-2"** was created. It has Enable Reading only and is IP-locked to `76.13.218.48`.
  - The old key is deleted. `.env` keeps the same path.
- **CoinGlass.** Do not put `COINGLASS_API_KEY` on the VPS. The key's plan is too low, the v4 paths are stale, and there is no Binance fallback, so it would trip the kill-switch.
- **EV gate and auto-decide.**
  - The EV gate stays latched and auto-decide stays off by design, until the shadow-veto window resolves.
  - The 66 short approvals that timed out were expected. Approvals have a 60 min TTL, and nobody approves them by hand.

## 3. Trade assistant: state

- **Owner setup.** The owner trades Binance USDⓈ-M futures by hand on the master account:
  - futures activated 2026-10-26;
  - one-way position mode;
  - futures wallet 100 USDT.

  Jarvise only advises. It places no orders, and it refuses any key with trade or withdraw permission.
- **Signals** (`/jobs/assist-signal`, n8n `jrvsAssistSignal4h`, cron `25 0,4,8,12,16,20` Asia/Bangkok):
  - symbols: BTC, ETH, SOL, BNB, ADA, DOGE, XRP;
  - floor 0.55;
  - stop = analysis invalidation (1.5× ATR), target 2R, risk 2% of the futures balance;
  - isolated, leverage ≤ 5x;
  - skips stale analysis, and repeats an idea only after 24 h or a side flip;
  - at most 2 open ideas and 4% total risk;
  - respects `MIN_NOTIONAL` and `minQty`.
- **Night hold.** 23:30–07:30 Bangkok, with the batch at 08:00 (`jrvsAssistMorning1`). The no-stop warning is never held.
- **Position watch** (`/jobs/assist-watch`, `jrvsAssistWatch5m`):
  - no-stop warning;
  - links a position to the signal it was taken from;
  - for off-signal positions, initial stop = 1.5× ATR;
  - move-stop advice uses only 1h candles opened after entry (`profit_lock.trail_stop_with_source`, import only);
  - exit advice on analyzer flat or opposite, or at 2R.
- **First live runs.**
  - The signal run before the wallet was funded gave `size_failed`.
  - With 100 USDT, ADA, DOGE, and XRP shorts size to about 73–97 USDT notional, 1x, about 2.00 USDT at stop.
  - The watch run saw no positions.

## 4. Open items, in order

1. **Check the first real assist payloads** on the VPS:
   - signals: created, skipped reasons, and whether the risk cap admitted at most 2;
   - watch: actions.

   Use `ssh Jarvise-VPS` with read-only commands.
2. **Trade assistant PR C** (not started):
   - a Telegram long-poll listener that accepts input only from `TELEGRAM_CHAT_ID`;
   - the "⏭ ข้าม" (skip) button under each signal;
   - a persistent 4-button menu: open positions, anything to enter now, this week's results, risk % (1/2/3/5/Other, confirm above 5%);
   - an assistant on/off switch;
   - the Monday 08:00 weekly summary: virtual R of all sent signals vs the owner's real realized PnL, including funding fees, with counts sent / skipped / taken.
3. **Owner decision still open:** a losing-streak pause. Pause signals until the next day after 3 losses in a row or a 6% weekly loss. Ask before writing the PR C prompt.
4. Merge #233.
5. Optional: a detailed review of `src/jarvise_paper/forward_h3.py` (#229). I checked only that the frozen files are untouched.

## 5. Checkpoints (do not change frozen rules in between)

| Date (about) | What | How |
| --- | --- | --- |
| 2026-11-17 | Shadow-veto window, day 30. At 2026-10-25: 2 scored, both `no_objection`, mean −0.95R, 0 vetoes. | `jarvise paper shadow-veto score --json` in the jobs container |
| 2026-11-25 | Forward H3, first look | `jarvise paper forward-h3 score --json` |
| 2026-12-17 | Shadow-veto window, day 60. Extension needs the owner. | same score command |

Frozen for H3 (any change restarts its count): `strategies.py`, `replay.py`, `stops.py`, `profit_lock.py`, `jarvise_paper/engine.py`. The shadow-veto model id, `PROMPT_VERSION`, and universe stay unchanged.

## 6. Working agreements from this session

- Claude reviews, audits, and does research. Cursor implements and ships with `fdpy`.
- Every feature PR comes back to Claude for review before merge. Claude may SSH to the VPS (`Jarvise-VPS`) for read-only checks.
- Claude never handles API key values. The owner pastes keys with `read -s` commands.
- Claude does not place or execute trades or transfers. It may open Binance pages and fill non-secret fields, such as a public key. The owner does 2FA and confirms.
- Replies to the owner are in Thai and short.
