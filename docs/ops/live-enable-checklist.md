# §5.8d — Live enable checklist (owner ops)

**Gate:** Keep `JARVISE_LIVE_TRADING=false` until every step below is done.

## 1. Dedicated Binance sub-account + trade API key

Capital isolation first — do **not** trade from the main account.

1. Binance → Sub-accounts → create a **Jarvise-only** sub-account.
2. Transfer in **only** what you are willing to lose on live (size-capped).
3. On that sub-account: API Management → Create API.
4. Permissions: **Enable Spot & Margin Trading** only.
5. Disable / do not enable: **Withdrawals**, Internal Transfer, Universal Transfer, Futures (unless required later).
6. Prefer IP allowlist to VPS public/Tailscale egress if available.
7. Store as **separate** secrets from the read-only balance key:
   - `BINANCE_TRADE_API_KEY`
   - `BINANCE_TRADE_API_SECRET` **or** `BINANCE_TRADE_PRIVATE_KEY_PATH`

## 2. Audit on VPS

```bash
cd /opt/jarvise
# after keys are in .env and jobs recreated:
docker compose exec jobs jarvise exchange check-key --trade --json
```

Expect `ok_for_trade: true` and `enableWithdrawals: false`. Exit code 3 = unsafe.

Also audit the read key (should not withdraw):

```bash
docker compose exec jobs jarvise exchange check-key --json
```

## 3. Enable live (only after audit passes)

In `/opt/jarvise/.env`:

```bash
JARVISE_LIVE_TRADING=true
JARVISE_LIVE_EQUITY_USD=10000
WEB_BASIC_AUTH_USER=...
WEB_BASIC_AUTH_PASSWORD=...
# BINANCE_TRADE_* already set
```

Live submit **requires** `invalidation_price` on the approval and places a venue `STOP_LOSS_LIMIT` after a BUY fill. Do not enable live until you have verified the stop row in `live_orders`. Paper equity is **never** used to size live orders.

```bash
docker compose up -d --force-recreate jobs web
curl -sS http://127.0.0.1:8080/healthz   # via Tailscale IP in practice
# expect live_trading true
```

## 4. Smoke

1. Ensure one small pending approval (`long`, tiny size under caps).
2. Approve once on `/analytics` (or Command Dashboard Home).
3. Confirm Telegram alert path still works; check the `live_orders` row: `client_order_id` is `jrv-<approval id>`, `status` is `filled` (or `partially_filled`), `executed_qty` / `cummulative_quote_qty` populated.
4. Run `docker compose exec jobs jarvise trade reconcile --json` — expect `open: 0` (or the partial fill moving to `filled`).
5. Import + activate `infra/n8n/workflows/jarvise-live-reconcile.json` (hourly read-only `GET /api/v3/order` by client order id; places nothing).
6. Confirm the fill hit the **Jarvise sub-account** balance, not the main account.
7. Kill-switch still blocks approve; with Redis stopped, approve on Home returns "kill_switch unreadable" and nothing is submitted.
8. **Kill-switch flatten** (with the tiny position from step 2 still open):
   1. Engage the kill-switch on Ops (`:8080/ops`).
   2. `docker compose exec jobs jarvise trade flatten --dry-run --json`. Expect `ran: true`, the symbol as `would_sell`, and its `jrv-x<approval id>` stop under `stops_to_cancel`. No venue HTTP is made.
   3. `docker compose exec jobs jarvise trade flatten --json`, or wait for the next 15-minute risk-monitor tick.
   4. In `live_orders`: the stop row is `canceled`, and a `jrv-f<buy row id>` MARKET SELL row is `filled` with `approval_id` empty and `realized_pnl_usd` set. Its `executed_qty` sits slightly under the BUY qty because the fee came out of the base asset.
   5. Telegram shows a "Jarvise LIVE FLATTEN" summary. Binance shows no open orders and only dust in that asset.
   6. Run it again. Expect `already_sold` and no new order.
   7. Clear the kill-switch only after a written review.

Idempotency: every live order carries `newClientOrderId = jrv-<approval id>`. A retried approve (same id) first checks the ledger, then the venue, and never POSTs twice; a lost response after POST is recovered by the same query. Flatten uses `jrv-f<latest BUY row id>` with the same ledger-then-venue check.

## Kill-switch flatten (what happens while live is on)

- The risk-monitor job (n8n, every 15 min) reconciles live orders and checks the live daily-loss cap. On a breach it engages the kill-switch. Then, while the kill-switch is **known-engaged** for any reason, it cancels Jarvise's own protective stops (by client order id, never cancel-all) and MARKET-sells every live spot position Jarvise holds.
- The web kill-switch toggle does not sell inline. The sell happens on the next tick, or right away with `jarvise trade flatten`. Venue stops cover the gap.
- An unreadable Redis blocks new orders but never sells.
- A failed cancel leaves that stop in place and alerts. A failed sell re-places the stop (`jrv-xr<approval id>`). If that also fails, Telegram says **UNPROTECTED**: close the position on Binance by hand.

## Disable live

1. Engage the kill-switch on Ops and let flatten finish: `jarvise trade flatten --json` shows every symbol `sold`, `already_sold`, or dust.
2. Only then set `JARVISE_LIVE_TRADING=false` and recreate `jobs web`. With the flag off, Jarvise makes **no** trade HTTP at all, including flatten.

## Current VPS status (2026-10-01)

- Read-only key present (Ed25519 PEM); trade keys **not** set.
- `JARVISE_LIVE_TRADING` remains **false**.
- Permission probe CLI shipped: `jarvise exchange check-key`.
- Live submit refuses keys with withdraw / transfer enabled.
- 2026-10-05: kill-switch flatten shipped, dormant while the flag is false (`jarvise trade flatten`; spec [`2026-10-05-live-killswitch-flatten-design.md`](../superpowers/specs/2026-10-05-live-killswitch-flatten-design.md)). Live stops and sells are now fee-net and `LOT_SIZE`-floored. Smoke step 8 is still open.
