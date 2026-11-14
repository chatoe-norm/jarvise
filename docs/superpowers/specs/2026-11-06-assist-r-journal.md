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

## Daily 3R loss pause

This sits beside the streak pause (3 consecutive losses until 08:20) and the
week-loss pause (trading net worse than 4R × week-start risk, 12% at 3% risk, until 08:20; 6% before 2026-11-14). Those two rules are
unchanged. The day rule does not replace them. If streak or week-loss engages
on the same call, that pause wins and the day rule waits until the assistant
is not paused.

The unit is planned R from `assist_real_trade`:

`net_r_real = (realized_pnl_usdt + commission_usdt + funding_usdt) / loss_at_stop`

`loss_at_stop` is the planned base stored at entry. A later stop move does not
change the denominator.

The day boundary is 00:00 Asia/Bangkok. A trade belongs to the day of its
`exit_time_ms`, not the time it was synced or journaled. The window is
`[today 00:00, now)`. The sum uses only bot-entered trades:

- `status = 'closed'`
- `side` is `long` or `short`
- `loss_at_stop > 0`
- `intent_id` is set (the one-tap entry `assist_real_trade` matched)

Wins offset losses. The pause engages when that sum is `<= -3`. Only closed
bot-entered trades are in the sum. Open positions are not.

Not counted: unmatched rows, trades opened by hand in the Binance app (no
intent), and rows with no `loss_at_stop`. A bot entry the owner later closes
by hand still has an intent, so that result counts. The Thai pause message
says the sum counts bot-entered trades only (`นับเฉพาะไม้ที่เข้าผ่านบอท`) and
shows the real end clock, for example `ถึง 08:20`. It does not say midnight,
and it does not say "tomorrow", because a pause at 02:00 ends at 08:20 the
same morning.

The pause lasts until the next 08:20 Asia/Bangkok, the same clock as the streak
and week-loss pauses. The day sum still resets at 00:00. A pause that started
the day before keeps running until its 08:20, while the new day's sum is
already zero. A fill with `exit_time_ms` 23:59 that is synced at 00:02 still
belongs to the previous day.

While the pause is on, new assist signals and one-tap entries (`ec:` / `ey:`,
already gated by `is_paused`) are blocked. Open positions are not closed.
Watch advice, move-stop (`ms:`), and exit (`xx:`) are not blocked.

The -3R pause fires at most once per Bangkok day. Migration 23 adds nullable
columns on the single `assist_edge` row (`day_pause_id`, `day_resume_net_r`,
`day_resume_id`, `day_deeper_pause_id`) with null defaults. Existing row values
are not rewritten. A new Bangkok day ignores those stored values because they
still carry yesterday's day id.

Manual resume is the owner turning the assistant back on while a pause is
active (`paused_until_ms > now`). That clears `paused_until_ms` and does not
re-pause on the same -3R. If today already hit the cap, the resume reply warns
with the day's current net R of closed bot-entered trades only, for example
`วันนี้ติดลบแล้ว -3.2R จากไม้ที่ปิดแล้ว`. Every such resume stores that net R
and clears the deeper marker, which arms a new threshold 1R lower
(`<= snapshot - 1`). The next further -1R pauses again until the next 08:20.
A resume when no pause is active, including an already-expired
`paused_until_ms`, does not move the stored level. An automatic resume at
08:20 on the same Bangkok day as the day-loss pause also arms a new -1R
threshold.

Watch runs the day-loss check again immediately after `journal_on_close`, from
SQLite only, so a close that reaches -3R pauses in that same poll.

The jobs skip key stays `streak_pause`. That string is the existing any-pause
gate, including blocked virtual tracks. The pause payload `reason` is
`day_loss` or `day_loss_deeper`. No new env var.
