# Assist report truth (PR-C1)

Date: 2026-11-09. Read-only report. This PR does not place, cancel, or edit an order.
It does not change sizing, risk caps, pauses, floors, the analyzer, the 1.5× ATR stop,
or profit-lock / trail rules.

Owner decisions the same day: planned R stays the main unit; actual R is a second
column; trades that closed before the R journal shipped are labelled, not backfilled.

## Linking

`create_entry_preview` already copies `seen_entry` and `seen_stop` from
`latest_seen_idea`. It now also stores that row's id as preview key
`seen_signal_id`.

`seen_signal_id` is report linking only. It does not mark the signal taken, does
not change `same_idea`, and does not affect `symbol_has_in_flight_exec`. A card
tap still writes preview `signal_id`, and only that key is what
`mark_signal_taken` reads.

`build_real_rows` stores the link on the existing `assist_real_trade.signal_id`
column: preview `signal_id` when it is set, otherwise `seen_signal_id`. The
first real trade (open or closed) for that id, ordered by `entry_time_ms` then
`entry_order_id`, is the pair. Every later real trade with the same id is
`re_entry`. The flag is computed when the report is built. It is not a column.

`re_entry` rows stay in the real R totals. They are left out of the
real-vs-virtual difference. The report prints the `re_entry` count on its own
line.

## R

Planned R is unchanged: `net_r_real = (realized + commission + funding) / loss_at_stop`.

Migration 24 adds two nullable columns, default NULL, and does not update
existing rows. Values appear only when the normal sync rebuilds a row.

- `risk_actual_usdt = |entry_fill_avg - planned_stop| * entry_qty`
- `net_r_actual = net / risk_actual_usdt` with the same net as planned R

`entry_fill_avg` is the quantity-weighted average of the entry fills.
`entry_qty` is the filled quantity. Both are what `build_real_rows` already
stores. If `planned_stop`, `entry_fill_avg`, or `entry_qty` is missing, or
`risk_actual_usdt <= 0`, `net_r_actual` is NULL. That is not an error.

`net_r_actual - net_r_real` is not a slippage measure: both use the same net
USDT and differ only in the denominator, so the sign follows win or loss
rather than fill quality.

Entry slip in R is computed in the report and is not stored:

`slip_entry_r = sgn * (entry_fill_avg - plan_entry) / |plan_entry - planned_stop|`

`sgn` is +1 for a long and -1 for a short. `plan_entry` is the preview entry
(`signal_entry`). Positive means the fill was worse than the plan. The value
is None when any input is missing or the plan distance is <= 0. The report
shows the mean and n.

## Pairs and virtual rows

The real-vs-virtual difference uses only a closed pair: not `re_entry`, with
`net_r_real` and a virtual `net_r`. A non-USDT commission row stays out of
every R average, including that difference, as it does today.

The block states n pairs, n real without a pair, n virtual without a real
trade, and n `re_entry`.

A virtual row with `entry == stop` or `loss_at_stop <= 0` has `net_r` NULL
and is dropped from virtual stats. The report says how many were dropped.
`_r_and_pnl` writes that NULL. The weekly virtual summary also drops a stored
row that still has a zero from before this rule, and counts it.

## Labels

Every line in the real-vs-virtual block states the window as Bangkok dates
and n. An average states its unit (R, USDT, or bps). The window is the
summary's `since`/`until`.
The weekly job still syncs from the PR #280 gate (`SPEC_START_MS`) and labels
that sample through the report clock. Example:

`ไม้จริง 01–07 พ.ย. (เวลาไทย): n=5 · เฉลี่ย +0.21R (planned) · +0.18R (actual) · ต้องการอย่างน้อย 20 ไม้ก่อนสรุป`

`slip เข้าเป็น R 01–07 พ.ย. (เวลาไทย): n=5 · เฉลี่ย +0.03R (บวก = เข้าแย่กว่าแผน)`

`คู่จริง-จำลอง 01–07 พ.ย. (เวลาไทย): n=3 · ต่างเฉลี่ย (จริง − จำลอง) +0.05R`

`ต้องการอย่างน้อย 20 ไม้ก่อนสรุป` is the closed-sample target on the first line.

## Journal

Trades that exited before the #293 deploy are labelled `ก่อนมีบันทึก` in the
weekly summary. They are excluded from the pass rate and from the ungraded
count. This PR does not insert cards for them.

Constant `JOURNAL_SHIPPED_MS = 1_793_950_811_000` (`2026-11-06T07:40:11Z`).

Source: GitHub Actions Deploy run
[42181311569](https://github.com/chatoe-norm/jarvise/actions/runs/42181311569),
job `deploy`, field `completed_at`. Head `148ca1a55355fb82acff0b3302325175431a154d`,
the merge of PR #293. A trade is before the journal when `exit_time_ms` is
strictly less than that instant.

## PR-C2

Income status, exit-time windows, and the reconcile. This PR does not place,
cancel, or edit an order. It does not change sizing, risk caps, pause
thresholds, floors, the analyzer, stops, or trail rules. No new environment
variable.

### Why commission has no pending state

Each `userTrades` fill already carries `realizedPnl` at fill time. Commission
is taken from those fills (`commission` / `commissionAsset`), which the sync
already stores. There is no income row to wait for, so commission cannot be
`pending`. `FUNDING_FEE` that has not been published counts as 0 and does not
make a trade `pending`. Only `REALIZED_PNL` income can arrive after the fill.

### Income status

Migration 25 adds nullable `income_status` TEXT on `assist_real_trade`. It does
not rewrite existing rows. A closed trade is one of:

- `complete` — every exit fill whose `realizedPnl` is nonzero has a matching
  `REALIZED_PNL` income row (`tradeId`).
- `pending` — that match is missing and `now - exit_time_ms` is at most 24 h.
- `missing` — the match is still missing more than 24 h after the exit.

Open trades and unmatched orders stay NULL.

Until the status is `complete`, `realized_pnl_usdt` is the sum of the exit
fills' `realizedPnl`, scaled by `exit_trade_fractions` the same way income is
scaled. `net_r_real` uses that figure at once, so the journal card and the
daily 3R pause are right before income arrives. When every required income row
is present, the stored realized value is replaced by the income sum and the
status becomes `complete`.

`_complete_income_cache` serves only rows with `income_status='complete'`.
A `pending` row, a `missing` row, and a closed row whose status is still NULL
(written before this PR) are rebuilt on the next sync. A NULL row is not left
at realized 0 just because the column is already filled.

### Refresh

`run_assist_watch` re-syncs closed rows whose status is NULL, `pending`, or
`missing` and whose exit is within the last 7 days. The refresh runs at most
once per 30 minutes in total. It uses the journal sync gap (`30 * 60 * 1000`)
and one sentinel row in `assist_journal_sync`. It does not give up after a
fixed number of attempts: a row leaves the set when it becomes `complete` or
its exit is older than 7 days.

Missing rows older than 7 days are not refetched. They stay `missing` and are
listed in the reconcile. A refresh failure does not block watch advice, the
orphan cancel, the journal, or the pause check.

The daily 3R pause still reads `net_r_real`. The pause rules are unchanged.
If the refresh corrects a row that exited on today's Bangkok day,
`evaluate_day_loss_pause` in that same watch pass sees the new day sum and
pauses when it reaches the limit. A correction from an earlier day does not
pause.

### Report windows

Closed real rows and virtual rows are in the report window when their exit
time is in `[window start, window end]`. Open real trades are counted on
their own line and are not part of the window stats. `_label_pair_roles` runs
on every synced row before that filter. The first trade for a signal, ordered
by entry time, is the pair even when that entry is outside the window. A later
trade that exits inside the window is still `re_entry`.

The R stats — mean planned R, mean actual R, the pairs difference, and entry
slip in R — skip `pending` and `missing` trades. The block shows
`รอข้อมูล income n=…` and `income ขาด n=…`.

The reconcile is for the same window. It sums Binance `REALIZED_PNL`,
`COMMISSION`, and `FUNDING_FEE` for `jarvise_assist.ASSIST_SYMBOLS` (the tuple,
not a hard-coded list) and sets those totals next to the sums stored on the
trades. Income rows that match no trade are listed with symbol, Bangkok time,
category, and amount, at most 10 lines, plus a count of the rest. Missing
trades are listed in that block.
