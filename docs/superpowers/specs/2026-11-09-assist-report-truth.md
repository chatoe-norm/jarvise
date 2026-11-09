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

## PR-C2 (later)

Income timing and reconcile are not this PR. The agreed rule, for that PR:

A closed trade with no `REALIZED_PNL` or no `COMMISSION` income yet is
`pending`. Absent `FUNDING_FEE` counts as 0. Income still absent 24 h after
exit becomes `missing` and is listed in the reconcile, not left pending.
