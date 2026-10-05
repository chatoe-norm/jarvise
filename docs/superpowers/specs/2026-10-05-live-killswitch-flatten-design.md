# Jarvise P4-C hardening — live kill-switch flatten — design

**Date:** 2026-10-05  
**Status:** APPROVED 2026-10-05 — dormant while `JARVISE_LIVE_TRADING=false`.  
**Depends on:** P4-C live submit ([`2026-09-27-p4c-live-submit-design.md`](2026-09-27-p4c-live-submit-design.md)), paper risk caps ([`2026-09-29-paper-risk-caps-design.md`](2026-09-29-paper-risk-caps-design.md))  
**Gap source:** [`docs/research/2026-10-05-autotrade-stack-gap.md`](../../research/2026-10-05-autotrade-stack-gap.md) Bucket C, "Live flatten / cancel on timeout or kill-switch"

## Goal

When the kill-switch is engaged and live trading is on, cancel the protective stops Jarvise placed and market-sell the live spot inventory Jarvise bought. Then the book is FLAT and stays locked until a written human review.

Doctrine: a drawdown halt cancels open orders, closes positions, and locks until a written review. Before this slice, the kill-switch only blocked new approvals. Live holdings kept their venue stop but were never closed.

This is a prerequisite for checklist step 3 (enable live). It is **not** P5.

## Bug fixed in the same slice

P4-C sized the protective `STOP_LOSS_LIMIT` and the `flat` SELL from `executed_qty` / ledger inventory at 8dp.

- Binance spot takes the taker fee out of the received base asset. Free balance after a BUY is about 0.1% below `executedQty`, so a stop or sell for the full amount is rejected for insufficient balance.
- Quantities were not floored to the symbol `LOT_SIZE` `stepSize`, so Binance rejects them with a filter error.

Fix: one sizing helper, `sellable_qty`. It uses `floor_to_step(min(cap_qty, venue free base balance))` and returns nothing when the result is under `minQty` or `minNotional`. The protective stop, the `flat` SELL, and flatten all use it. Stop and limit prices are also floored to the `PRICE_FILTER` `tickSize`.

A venue `STOP_LOSS_LIMIT` locks its base balance, so a `flat` SELL from Approve would also have been rejected while the protective stop was open. The approval `flat` SELL now goes through the same cancel-our-stops → size → sell path as flatten (`sell_inventory`).

## Locked rules

1. **Preconditions:** flatten runs only when `live_trading_enabled()` **and** `kill_switch_state(strict=True)` reports `known=True, engaged=True`. If Redis can't be read, new orders stay blocked as they are today, but nothing is sold.
2. **Any engaged reason** flattens: drawdown lock, daily loss, market safety, manual Ops toggle, the `auto_decide` live-path guard.
3. **Timing:** the same tick when the risk monitor itself trips the halt. Otherwise the next risk-monitor tick (n8n, 15 min). The venue protective stops cover the gap. The web toggle does not flatten inline. The owner can run `jarvise trade flatten` right after toggling.
4. **Own orders only:** cancel with `DELETE /api/v3/order` by `origClientOrderId`, using ids from `live_orders`. Never `DELETE /api/v3/openOrders`.
5. **Order of operations per symbol:** cancel our open stops → size → MARKET SELL.
   - If a cancel fails, skip the symbol. The stop stays in place and the owner gets an alert.
   - If the SELL fails and the venue has no such order, re-place the protective stop (client id `jrv-xr<approval id>`, original `invalidation_price`) and alert. If the re-place also fails, the alert says **UNPROTECTED**.
   - Dust (under `minQty` / `minNotional`) is recorded once as a `skipped` row under the `jrv-f` id, so later ticks see it in the ledger and do not re-alert.
6. **Idempotent:** the flatten client id is `jrv-f` plus the id of the most recent filled BUY row for that symbol. A rerun checks the ledger, then `GET /api/v3/order`, before any POST, so it never sells twice.
7. **Live flag off → zero trade HTTP.** To retire live, engage the kill-switch and let flatten finish before setting the flag false.
8. Sell rows: `side=SELL`, `order_type=MARKET`, `approval_id=NULL`, `realized_pnl_usd` from the existing average-entry PnL.
9. Telegram summary (soft-fail) whenever anything was cancelled, sold, flagged as dust, or errored.

## Allowlist (trade package only)

| Call | Why |
|------|-----|
| `POST /api/v3/order` | existing: MARKET, STOP_LOSS_LIMIT |
| `GET /api/v3/order` | existing: idempotency + reconcile |
| `DELETE /api/v3/order` | new: cancel our stop by `origClientOrderId` |
| `GET /api/v3/account` | new: free base balance (trade key, signed) |
| `GET /api/v3/exchangeInfo` | new: `LOT_SIZE`, `PRICE_FILTER`, `NOTIONAL`/`MIN_NOTIONAL` (public) |

Still forbidden: `/api/v3/openOrders` cancel-all, `/api/v3/order/cancelReplace`, withdraw, transfer, futures, margin.

## Surfaces

- `jarvise_trade.flatten.flatten_live_positions(conn, *, reason, now_ms, http_client=None, dry_run=False)`
- `jobs.run_risk_monitor`: after the paper monitor, when live is on, it runs reconcile, then the live day-loss check (engages the kill-switch on breach), then flatten if engaged (`jarvise_trade.flatten.live_risk_tick`). The live block runs even if the paper pass crashed. The output goes in `payload["live"]`.
- CLI: `jarvise trade flatten [--dry-run] [--json]`, with the same preconditions.
- `jarvise_notify.notify_live_flatten(result)`.

## Non-goals

- Live flatten on approval timeout (TTL)
- P5 autonomy, LIMIT/OCO, user-data WebSocket, multi-venue routing
- Inline flatten from the web kill-switch toggle
- Changing paper behavior, analyzer floors, or `same_side_hold`

## Testing

- Mocked HTTP only. Live flag off → zero trade HTTP. Unknown or clear kill-switch → no-op.
- Cancel then sell a fee-net, step-floored qty. A rerun does not POST twice.
- Cancel failure → no sell, stop row unchanged. Sell failure with the venue absent → stop re-placed.
- Dust below `minQty` / `minNotional` → skipped and reported.
- Risk-monitor job: live day-loss breach engages the kill-switch and flattens. Live off leaves the payload unchanged.

## Success criteria

1. Default deploy (`JARVISE_LIVE_TRADING=false`): no behavior change, no new HTTP.
2. With live on and kill-switch engaged: every Jarvise live position is either sold, reported as dust, or left with its stop in place and an alert sent.
3. The live-enable checklist smoke covers one kill-switch flatten on a tiny position.
