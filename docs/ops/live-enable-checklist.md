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
# BINANCE_TRADE_* already set
```

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

Idempotency: every live order carries `newClientOrderId = jrv-<approval id>`. A retried approve (same id) first checks the ledger, then the venue, and never POSTs twice; a lost response after POST is recovered by the same query.

## Current VPS status (2026-10-01)

- Read-only key present (Ed25519 PEM); trade keys **not** set.
- `JARVISE_LIVE_TRADING` remains **false**.
- Permission probe CLI shipped: `jarvise exchange check-key`.
- Live submit refuses keys with withdraw / transfer enabled.
