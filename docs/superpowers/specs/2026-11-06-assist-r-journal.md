# Assist R journal (option C)

Date: 2026-11-06. A draft card after a full close. The owner grades it with one tap.
The journal never places, cancels, or edits an order.

## Table

Migration 22 adds `assist_journal`. Primary key `entry_order_id` matches
`assist_real_trade.entry_order_id`. Columns: `auto_checks_json`, `grade`
(`pass` or `fail`, else null), `mistake_tags` (comma list), `note`,
`card_message_id`, `created_at_ms`, `graded_at_ms`.

No fill, R, or USDT numbers are copied. The R unit is that trade's
`loss_at_stop`. A later stop move does not change it.

Tags, only these: `chase`, `early_exit`, `moved_stop_away`, `no_stop`,
`over_2_positions`, `after_3_losses`, `other`.

## Trigger

When watch sets `closed_at_ms`, the live orphan `jv-` cancel runs first.
`journal_on_close` runs after that block, still in its own try/except. It syncs
real trades from one hour before the earliest entry among closes that still need
a card, then inserts one journal row for each `assist_real_trade` row in that
window with `status='closed'` and `side` of `long` or `short` that does not have
one yet, and sends one Telegram card per new row.

`unmatched` and `open` rows never get a journal row. Sending and the weekly
summary use the same filter: join `assist_real_trade` and keep only
`status='closed'` with `side` in `long` or `short`. A row that fails it is never
sent and never counted. A closed `long` or `short` the owner exited by hand
still gets a card. Its plan check fails. The closing order, stored as side
`buy` or `sell`, does not.

A failed sync or send is logged and swallowed. It does not stop the watch loop,
exit or move-stop advice, or pause logic. A stored message id is never sent
again.

A row with no `loss_at_stop` still gets a card. R is shown as `ไม่ทราบ` and the
row is left out of R sums.

## Close sync throttle

Each closed watch position, keyed by `(symbol, position_side, closed_at_ms)`,
gets at most one sync attempt per 30 minutes and at most 4 attempts. The close
must also still be less than 6 hours old. After the fourth attempt the next
poll gives up and logs once. No journal row is written for that close, and the
weekly summary does not count it as ungraded. Attempts live in
`assist_journal_sync` from migration 22. No new env var.

## Night hold

`_send_pending` sends nothing while `is_night_hold(now)` is true (23:30
inclusive through 07:30 exclusive, Asia/Bangkok). Rows may still be inserted.
Pending cards go out together on the first watch poll after the window. There
is no per-poll cap.

## Card

Thai. Symbol and side, entry to exit, `1R` in USDT, net R to 2 decimals, USDT
P&L on its own line labelled `ผลเป็นเงิน`, exit kind plus the last watch advice
before the exit, the stop timeline with `locked_R` at the planned stop and each
done move-stop. The first timeline line is labelled `stop เริ่ม`. Then the
analysis thesis and confidence from `preview_json.analysis_id`, and the six
auto-checks.

`locked_R` is price-locked USDT divided by the original `loss_at_stop`:

- long: `(stop - entry_fill) * qty / loss_at_stop`
- short: `(entry_fill - stop) * qty / loss_at_stop`

Buttons: `✅ ตามแผน` (`jp:`) and `❌ ผิดแผน` (`jf:`). After a fail, tag buttons
(`jt:`) toggle, then `เสร็จ` (`jd:`). A text reply to the card is appended to
`note` (500 characters) and is not a menu or explore command.

## Auto-checks

Stored once in `auto_checks_json` and shown on the card:

- watch rows open at entry time are at most 2
- `signal_present`, labelled `เข้าจากสัญญาณบอท`, passes when `signal_id` is set,
  or when `intent_id` is set and that intent's `preview_json` contains an
  `analysis_id`
- `stop_existed` fails when any `assist_watch_advice` row with `kind='no_stop'`
  for that symbol has `created_at_ms` from `entry_time_ms` through
  `exit_time_ms` inclusive. Otherwise it passes
- every done move-stop only tightened (long stop up, short stop down)
- exit followed plan: `stop` or `tp`, or `owner_exit` / `emergency` when an exit
  advice was sent before the exit. `manual` and `unknown` fail
- entry was not inside a still-recorded streak pause
  (`streak_anchor_ms` ≤ entry < `paused_until_ms`). A cleared pause, or one
  anchored at this close, does not fail

## Weekly

The Monday summary adds one block for closes in the same 7 days: n, sum R,
average R, pass rate, average R of pass versus fail, the top mistake tag, and
the ungraded count. Rows with no `loss_at_stop` are out of the R math. The
existing USDT and virtual lines stay.

## Untouched

Floors 0.55 and 0.70, the analyzer formula, the 1.5× ATR stop, risk caps,
sizing, shared leverage, `flat_exit`, `same_side_hold`, the universe,
`ASSIST_SYMBOLS`, and the one-tap execute path. `JARVISE_LIVE_TRADING` stays
off. No new env var. No writes to paper, live, approval, or shadow tables.
