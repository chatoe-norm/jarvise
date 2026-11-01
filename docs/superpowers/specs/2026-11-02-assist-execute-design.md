# Trade assistant: one-tap execution with owner confirm

Date: 2026-11-02. Status: draft for owner approval.
Builds on `2026-10-25-trade-assistant-design.md` (signals, watch, sizing, menu).

## 1. Intent

The owner trades Binance USDⓈ-M futures by hand from Telegram advice. Typing the
numbers by hand caused real mistakes. On 2026-10-30 the owner entered DOGE twice
(676 instead of 338), the stop covered only 338, and the position was Cross
instead of isolated. Typing also takes time, and the price moves while the owner
types.

The owner wants Jarvise to fill the order so the owner only decides yes or no.
Every write to Binance happens only after the owner presses a confirm button in
Telegram. Binance has no "pending approval" state for API orders, so the confirm
button in Telegram is the approval.

Owner's words: "ระบบช่วยกรอกเพื่อลดความผิดพลาด และลดระยะเวลาการเข้าไม้ ที่สมบูรณ์".

## 2. Goals

1. Entry: pick a coin in Telegram, see a preview, press ✅, and Jarvise opens the
   position with isolated margin, the right leverage, the exact size, and a stop
   and TP covering the whole position.
2. Move stop: the watch's "เลื่อน stop" advice gets a ✅ button that moves the
   stop for the owner.
3. Exit: the watch's "ออกตอนนี้" advice gets a ✅ button that closes the whole
   position and removes its leftover stop and TP orders.
4. Fewer mistakes: one tap opens exactly one position, and every position has a
   full stop.

## 3. Non-goals

- Jarvise never sends an order without a confirm tap. Autotrade stays off.
- No withdrawals, transfers, or account-setting changes beyond margin type and
  leverage for the symbol being traded.
- No symbols outside the seven-symbol assist universe. Positions the owner opens
  on other symbols are never touched.
- No hedge mode, no Multi-Assets mode, no limit entries, no adding to an open
  position, no partial exits.
- No change to the analyzer, floors 0.55/0.70, 1.5×ATR stop, `flat_exit`,
  `same_side_hold`, the universe, or the paper engine.

## 4. Anti-goals (a failure even if it "works")

- A position open without a full stop for longer than the stop-placement retry.
- Two positions from one tap, or from two taps on the same preview.
- Executing numbers the owner did not see in the preview.
- An order based on stale analysis, after the price has run away, during a
  losing-streak pause, or while the kill switch is on.
- A trade-enabled key that can withdraw, or that works from any IP.

## 5. Owner flow

### 5.1 Entry

1. The owner presses `🔎 ตอนนี้มีเหรียญไหนน่าเข้า` (existing button).
2. The answer stays as today and adds one inline button per admitted idea, for
   example `DOGE SHORT`. Pushed signals (:25 and the 08:20 batch) get the same
   button next to `⏭ ข้าม`.
3. Pressing a coin button re-runs the scan for that symbol with live balance,
   mark, filters and caps, and replies with a preview. The preview uses the
   existing `format_signal_message` lines: entry ≈ mark, stop, target 2R,
   quantity, leverage, margin, and loss at stop. It shows two buttons:
   `✅ ยืนยันเข้าไม้` and `✖️ ยกเลิก`.
4. A preview is valid for 120 seconds. After that, ✅ answers
   "หมดเวลา — กดเหรียญใหม่" and does nothing.
5. On ✅, Jarvise runs the confirm-time checks in §6.2, then the entry sequence
   in §7.1, and edits the preview message to the result: fill price, quantity,
   stop, TP, and loss at stop recomputed from the fill.

### 5.2 Move stop

The watch's move-stop message gets `✅ เลื่อนให้`. On tap, Jarvise checks that the
position is still open with the same side, that the new level is still tighter
than the current stop, and that the mark has not passed it. If the mark has
passed it, Binance would trigger the stop at once, so Jarvise replies
"ราคาผ่านจุดนี้แล้ว" and offers the exit button instead. Otherwise it runs §7.2.

### 5.3 Exit

The watch's exit message gets `✅ ปิดไม้`. On tap, if the position is already
closed, it replies "ไม้นี้ปิดไปแล้ว". Otherwise it runs §7.3 and replies with the
exit price and realized PnL from the fill.

### 5.4 Held advice

Advice held overnight is sent in the morning with the same buttons. Every tap
re-checks live state, so an old message never acts on old numbers.

## 6. Guards

### 6.1 Always (every button)

- The callback comes from the owner chat id. The listener already fails closed
  on any other chat. Keep it that way.
- The execution flag is on (§9), and the kill switch is off. If Redis cannot be
  read, treat the kill switch as on, the same fail-closed rule as the jobs.
- The trade key passes its safety check (§8).
- The account is in one-way mode, and Multi-Assets mode is off. If either is
  wrong, reply with what to change and do nothing.
- Each action has an intent id. A second tap on a used intent replies with the
  first result and sends nothing (§7.4).

### 6.2 Entry, at confirm time

These are the same rules as the signal scan, evaluated again at confirm:

- The analysis is fresh (`analysis_is_fresh`) and still on the same side and
  above the floor.
- The symbol has no open position.
- At most 2 open ideas and 4% total risk (`admit_signals` against open
  positions).
- Margin fits `availableBalance` (`assign_shared_leverage` for this one idea,
  with the open positions' margin already used).
- Mark has not moved more than 0.3R from the preview entry (`chase_exceeded`).
- Not in a losing-streak pause.
- Size ≥ min qty and min notional.

Jarvise executes the preview's quantity, stop and TP, never a quietly resized
version. If any check fails, it replies with the reason in Thai, reusing
`ON_DEMAND_REASON_TH`, and sends nothing.

## 7. Execution sequences

All writes go through one new module (suggested `jarvise_assist/execute.py`)
with its own signed client built from the trade key. The current read-only
`BinanceFuturesClient` stays read-only.

### 7.1 Entry

1. `POST /fapi/v1/marginType` ISOLATED. Treat "no need to change" as success.
2. `POST /fapi/v1/leverage` with the preview leverage.
3. `MARKET` order, quantity = preview qty, `newClientOrderId` derived from the
   intent id.
4. Read the fill (avg price, executed qty). If executed qty is 0, stop and
   report.
5. Place the protective stop: `STOP_MARKET`, `workingType=MARK_PRICE`,
   reduce-only, quantity = executed qty, trigger = preview stop rounded to the
   tick size. Then place the TP: `TAKE_PROFIT_MARKET`, reduce-only, quantity =
   executed qty, trigger = preview target.
6. If the stop is rejected, retry once. If it still fails, or the stop would
   trigger at once, close the position with a reduce-only `MARKET` order and
   alert. A position must never be left without a stop.
7. If the TP fails, keep the position (the stop protects it) and say so in the
   reply.
8. Link the watch row: mark the signal taken, and set `initial_stop` to the
   preview stop with source `signal`.

Reduce-only orders with an explicit quantity, rather than `closePosition=true`:
Binance allows only one `closePosition` stop per direction, which blocks the
place-new-then-cancel-old order in §7.2. The existing coverage check already
reads quantities.

### 7.2 Move stop

1. Place the new reduce-only `STOP_MARKET` for the full position qty.
2. Then cancel the old stop or stops for that position.
3. If step 1 fails, keep the old stop and report. If step 2 fails, report; two
   stops are safe, and the tighter one wins.

### 7.3 Exit

1. Reduce-only `MARKET` for the full position qty on the closing side.
2. Cancel the remaining reduce-only conditional orders for that symbol.
3. Reply with the fill and realized PnL. The watch marks the row closed on its
   next poll as today.

### 7.4 Idempotency and records

- New table `assist_exec_intent` with: id, kind (entry/move_stop/exit), symbol,
  side, preview payload (qty, stop, target, leverage, entry), created_at,
  expires_at, status (preview/confirmed/done/failed/expired/cancelled),
  result payload, and the Telegram message id.
- Moving from preview to confirmed is one atomic `UPDATE ... WHERE
  status='preview'`. Only the caller that changed the row runs the sequence.
- Every Binance write is logged in `assist_exec_log` (intent id, step, endpoint,
  client order id, HTTP status, Binance code, order or algo id). Never log keys,
  signatures, or full query strings.
- Client order ids carry a `jv-` prefix so Jarvise can tell its own orders apart.

### 7.5 Orphan clean-up

When the watch sees a position close (stop or TP hit), it cancels the remaining
`jv-` reduce-only orders for that symbol. Jarvise does not cancel orders it did
not place, except in §7.3 after the owner pressed exit.

## 8. Keys

- The current key "Jarvise-2" stays read-only and keeps its check that blocks
  trading permission. Signals, watch, and menu keep using it.
- A new key (for example "Jarvise-trade") has Enable Futures on, withdrawals off,
  Spot and Margin off, and an IP restriction to the VPS. The owner creates it.
  It goes into new variables, for example `BINANCE_TRADE_API_KEY` and
  `BINANCE_TRADE_PRIVATE_KEY_PATH`, the same way as the current key (pasted with
  `read -s`, never shown to Claude or written to the repo).
- Trade-key safety check, run before every write and cached briefly: futures
  trading on, withdrawals off, IP restricted. If any of these fails, alert and
  refuse.

## 9. Switches and rollout

- `JARVISE_ASSIST_EXECUTE` = `off` (default), `testnet`, or `live`, set in the VPS
  `.env` by the owner. When off, the coin and ✅ buttons are not shown; old
  buttons reply "ปิดอยู่". It is separate from `JARVISE_LIVE_TRADING`, which
  stays off for the paper autotrade path.
- The Telegram toggle (`⏸ หยุด / ▶️ เปิด ผู้ช่วย`) also disables execution.
- Phase 1, testnet: build and run on the Binance Futures testnet with a testnet
  key. Pass the scenarios in §10 on testnet before phase 2. Testnet prices differ
  from mainnet, and the watch and read key only see the live account. So in
  `testnet` mode, the preview keeps the analysis stop and target as percentage
  distances from the mainnet mark and applies them to the testnet mark, and the
  executor checks coverage and clean-up by reading the testnet account with the
  trade client. The live watch, signals, and pause logic do not read testnet.
- Phase 2, live: the owner sets `live`. Risk per trade stays at the current
  setting (1%).
- `AGENTS.md` changes from "Jarvise never places those orders" to "Jarvise places
  assist orders only after an owner confirm tap in Telegram". That edit and the
  new key are a separate owner-approved prompt.

## 10. Acceptance scenarios (tests, then testnet)

1. Entry happy path: one tap gives isolated margin, the right leverage, the
   exact qty, a full stop and a TP. `protective_stop_coverage` on the account
   that holds the position (testnet: read with the trade client) returns `full`.
2. Double tap and two taps on the same preview give exactly one order.
3. Preview older than 120 s: nothing is sent.
4. Price ran more than 0.3R, stale analysis, a third idea, more than 4% risk, a
   pause, or the kill switch on: nothing is sent, and the reason is shown.
5. Stop rejected twice: the position is closed and an alert is sent.
6. Move stop: the new stop exists before the old one is cancelled, and coverage
   stays full the whole time.
7. Move stop after the mark passed the level: nothing is sent, and the exit
   button is offered.
8. Exit: the position is closed, and its stop and TP orders are gone.
9. Exit when already closed: "ไม้นี้ปิดไปแล้ว", and nothing is sent.
10. The read-only key never sends a write. The trade key with withdrawals on is
    refused.
11. A callback from another chat does nothing.
12. Stop or TP hit: leftover `jv-` orders are cancelled.

## 11. Decided by the owner

- Execution goes through Telegram buttons, with the owner confirming every
  write.
- Scope covers entry, move stop, and exit.
- The coin choice starts from the existing "anything now" button.
- The purpose is fewer input mistakes and faster, complete entries.

## 12. Claude's calls (owner may change)

- Preview valid for 120 s.
- Market entry only, with stop and TP as reduce-only quantity orders.
- If the stop cannot be placed, close the position.
- Testnet first, then live at the current risk setting. On testnet, stop and
  target keep their percentage distance from the mark.
- Pushed signals also get the entry button.
- A separate trade key, and a separate switch from `JARVISE_LIVE_TRADING`.
- Clean-up only cancels Jarvise's own orders, except on owner exit.

## 13. Left to the builder

- Exact Binance endpoints and fields for conditional (algo) orders. Verify them
  against the current Binance docs and on testnet; the read path already uses
  `openAlgoOrders`.
- Tick-size rounding (add `PRICE_FILTER` tickSize to `SymbolFilters`).
- Telegram `callback_data` format within 64 bytes.
- Where the executor runs (the listener thread in the jobs container is the
  natural place) and how it shares the DB connection.
