# §5.8d — Live enable checklist (owner ops)

**Gate:** Keep `JARVISE_LIVE_TRADING=false` until every step below is done.

## 1. Create a dedicated trade API key (Binance)

1. Binance → API Management → Create API.
2. Permissions: **Enable Spot & Margin Trading** only.
3. Disable / do not enable: **Withdrawals**, Internal Transfer, Universal Transfer, Futures (unless required later).
4. Prefer IP allowlist to VPS public/Tailscale egress if available.
5. Store as **separate** secrets from the read-only balance key:
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
2. Approve once on `/analytics`.
3. Confirm Telegram alert path still works; check `live_orders` row status `submitted`.
4. Kill-switch still blocks approve.

## Current VPS status (2026-10-01)

- Read-only key present (Ed25519 PEM); trade keys **not** set.
- `JARVISE_LIVE_TRADING` remains **false**.
- Permission probe CLI shipped: `jarvise exchange check-key`.
- Live submit refuses keys with withdraw / transfer enabled.
