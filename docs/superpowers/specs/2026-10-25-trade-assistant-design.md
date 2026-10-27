# Trade assistant mode (design, 2026-10-25)

## Intent

The owner trades Binance USDⓈ-M futures by hand and wants Jarvise to tell them which coin to enter now, where to enter, where to put the stop, how big the trade should be, and when to exit. The owner places every order. Jarvise only gives advice over Telegram and reads the futures account with a read-only key.

The reason is speed. Proving an autonomous edge takes months: the shadow-veto window and forward test H3 are both running. The owner wants to act on Jarvise's signals now, under their own judgment, while those tests continue untouched.

## Goals

1. **New signals.** When a 4h candle closes and the analyzer gives `long` or `short` for one of the seven symbols, send one short Telegram message:
   - entry, which is the current price;
   - stop, which is the analyzer's `invalidation_price` (1.5× ATR);
   - target at 2R;
   - quantity and notional;
   - the leverage needed;
   - the USDT lost if the stop is hit;
   - an expiry time.
2. **Position size.** Size every trade from a risk-per-trade percentage the owner sets: default 2%, allowed range 0.5–15%. Ask for an extra confirmation above 5%. The account balance comes from a read-only Binance futures API key.
3. **Live position tracking.** Detect the owner's real futures positions from the API, with entry price, size, side, and whether a protective stop order exists. There is no "I entered" or "I exited" button.
4. **Advice while a position is open.** This applies to positions opened from a signal and to positions the owner opened alone, as long as the symbol is one of the seven:
   - **Move stop.** Use the same breakeven and 1R trail rule as the paper `profit_lock` (`trail_stop_with_source`), computed from the real entry.
   - **Exit now.** Send when the analyzer for that symbol turns `flat` or the opposite side, or when price reaches 2R.
   - **No stop order.** Send immediately, at any hour, when an open position has no stop order on Binance.
5. **Night hold.** Hold messages created from 23:30 to 07:30 Asia/Bangkok and send one batch at 08:20, after the 08:00 paper-run has written fresh analysis, with only what is still valid. A signal is still valid when the latest analyzer row has the same side and price has not moved more than 0.3R past the entry toward the target. Exit and move-stop advice held overnight is re-checked against current price before sending. The "no stop order" warning is never held.
6. **Telegram buttons.**
   - Under each new signal: one inline button, "⏭ ข้าม" (skip). It marks the signal skipped for the weekly summary.
   - A persistent reply keyboard with four buttons:
     - "📋 ไม้ที่ถืออยู่" (open positions): lists them with suggested stop and status;
     - "🔎 ตอนนี้มีเหรียญไหนน่าเข้า" (anything to enter now?): the latest still-valid signals, or "none";
     - "📊 ผลสัปดาห์นี้" (this week's results);
     - "⚙️ ตั้งความเสี่ยงต่อไม้ (ตอนนี้ X%)" (set risk per trade): opens a small choice of 1%, 2%, 3%, 5%, and Other.
   - If the owner sends a bare number after "Other", it sets the risk. The bot accepts input only from `TELEGRAM_CHAT_ID`.
7. **Weekly summary.** Every Monday at 08:00 Bangkok, report two numbers, both in R and in USDT:
   - **All signals as if taken.** A virtual track of every sent signal: entry at signal price, resting stop fill, exit by the same rules as goal 4.
   - **The owner's real trades,** from realized PnL on closed positions.

   Report the counts of signals sent, skipped, and taken. The owner chose to see no statistics on each signal, so the weekly summary is where the honest track record lives.

## Non-goals

- No order placement, no trade-enabled key, and no change to the live spot path or `JARVISE_LIVE_TRADING`.
- No change to the analyzer, floors 0.55/0.70, the 1.5× ATR stop, `flat_exit`, `same_side_hold`, or `profit_lock` code. The assistant reads their outputs and calls their pure functions.
- No change to the paper ledger, approval queue, auto-decide, EV gate, shadow veto, forward test H3, or the universe.
- No coins beyond BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, ADAUSDT, DOGEUSDT, and XRPUSDT. For any other symbol, "open positions" lists the position and says "no data, no advice".
- No chart images, and no Claude or LLM calls in the signal path. Messages are deterministic from the analyzer and price.

## Anti-goals (a failure even if it works)

- A message that reads like a guarantee. Messages are numbers only. The weekly summary must show losses as plainly as wins.
- Any write to Binance. A key with trading or withdrawal permission must be refused at startup, the same way `jarvise_exchange/permissions.py` refuses withdraw-scoped keys today.
- Accepting a Telegram command from any chat other than the owner's.
- A held night message sent at 08:20 with stale numbers.
- Silent failure. If the futures API or the bot listener is down for more than 30 minutes, send one alert, and one more on recovery, using the edge-state pattern of PR #227.

## Constraints

- **Secrets.** The owner activated USDⓈ-M futures on the Binance master account on 2026-10-26, after passing the futures quiz. Futures reads reuse the existing master-account read-only key "Jarvise": Ed25519, Enable Reading only, with the owner restricting it to the VPS IP. It is read through `resolve_binance_auth()` in `jarvise_exchange/binance_spot.py` from `BINANCE_API_KEY` plus `BINANCE_API_PRIVATE_KEY_PATH`, which are already passed to the containers. No new env vars. The key was created before futures was activated. On the first futures read, if Binance answers with a permission error, report it clearly (Telegram once, plus the job payload) so the owner can create a fresh read-only key. Do not retry in a loop. The key must never appear in logs, payloads, or Telegram.
- **Binance endpoints.** Read-only, signed GETs:
  - futures balance and account;
  - position risk;
  - open orders, including conditional orders (stop and take-profit), which Binance may serve from a separate algo-orders endpoint.

  The builder must confirm the current endpoints against Binance docs before coding.
- **Telegram input.** Telegram is send-only today (`jarvise_notify/telegram.py`). Receiving button presses needs a long-polling listener (`getUpdates`). There is no public HTTPS on the VPS, so a webhook is not an option.
- **Timing.** The 4h analysis runs from the existing paper-run job. The assistant runs right after it, or as its own n8n job a few minutes later. The position watch runs every 5 minutes so that the "no stop order" warning and "exit" advice are timely. The night window and the Monday summary use Asia/Bangkok time.
- **Quiet mode.** The owner's global quiet mode (`jarvise:notify:quiet_mode`) does not suppress assistant messages. The assistant has its own on/off switch, so the owner can pause it without touching other alerts.
- **Storage.** New tables come through a numbered migration (next after 13):
  - sent signals, with status `sent`, `skipped`, `expired`, or `taken`;
  - the virtual track;
  - the last seen futures positions;
  - assistant settings: risk %, on/off, and edge state.

  Never write existing paper or live tables.

## Decided by the owner

- Binance USDⓈ-M futures, traded by hand.
- Jarvise picks the coins, from the seven current symbols.
- Message style A: numbers only, no reasons and no stats on each signal.
- Risk per trade is set by the owner, default 2%.
- The balance and the real positions come from a read-only API key. There are no "entered" or "exited" buttons.
- The buttons are "skip", plus the four-button persistent menu.
- Night messages are held for the morning.
- Positions the owner opens outside signals also get stop and exit advice.

## Claude's calls (the owner may change these)

- Target at 2R. The chase limit is 0.3R. The night window is 23:30–07:30 with the batch at 08:20 Bangkok.
- Signal expiry is the next 4h close.
- The stop for an off-signal position is 1.5× ATR (4h) from the real entry.
- The "no stop order" warning is never held overnight.
- Above 5% risk, the owner must confirm.
- The weekly summary is on Monday at 08:00 and splits virtual results from real ones.
- Leverage is shown as the minimum needed for the notional against the balance, rounded up to a whole number, never more than 5x. If more than 5x would be needed, the message says so and suggests a smaller size.
- The balance is the USDT wallet balance of the futures account. Other assets are ignored.
- Support both one-way and hedge position mode. In hedge mode, a long and a short on the same symbol are two positions.
- Signal levels come from the analyzer, which works on spot candles. Entry and current price use the futures mark price. The stop is the analyzer's invalidation level as is, because the spot–futures basis on these seven symbols is small against a 1.5× ATR stop.
- Quantities are rounded to each symbol's futures step size, read from exchange info.

## Left to the builder

- Where the bot listener runs: a small new container or a loop in `jobs`.
- Table and module names. A suggested package is `jarvise_assist`.
- How "taken" is matched: a real position on the same symbol and side, opened within the signal's validity window.
- The exact Telegram formatting, within style A as in the approved mockup. Mockup files are in `.superpowers/brainstorm/843-1792921537/content/`.
- Tests and fixtures for every rule above, using recorded Binance and Telegram payloads. No live calls in tests.
