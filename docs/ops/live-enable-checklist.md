# §5.8d — Live enable checklist (owner ops)

**Two independent real-money paths.** §1–§4 and the kill-switch sections cover **spot live** only (`JARVISE_LIVE_TRADING`). The path that trades today is **assist futures** (`JARVISE_ASSIST_EXECUTE`), covered in [Assist one-tap execute key](#assist-one-tap-execute-key). `JARVISE_LIVE_TRADING=false` does **not** stop assist futures orders.

**Gate:** Keep `JARVISE_LIVE_TRADING=false` until every spot step below is done.

## 1. Dedicated Binance sub-account + spot trade API key

Capital isolation first — do **not** trade from the main account.

1. Binance → Sub-accounts → create a **Jarvise-only** sub-account.
2. Transfer in **only** what you are willing to lose on live (size-capped).
3. On that sub-account: API Management → Create API.
4. Permissions: **Enable Spot & Margin Trading** only.
5. Disable / do not enable: **Withdrawals**, Internal Transfer, Universal Transfer, Futures. Futures belongs on the separate assist key below, never on the spot key.
6. Prefer IP allowlist to VPS public/Tailscale egress if available.
7. Store as **separate** secrets from the read-only balance key:
   - `BINANCE_TRADE_API_KEY`
   - `BINANCE_TRADE_API_SECRET` **or** `BINANCE_TRADE_PRIVATE_KEY_PATH`

The VPS `BINANCE_TRADE_*` values today are the **assist futures** key and are composed into **jobs only**. Enabling live spot later needs a **separate spot** key wired explicitly into the **web** service. Do not point web at the assist futures key.

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

## Assist one-tap execute key

Separate from the spot live section above. Spec: [`2026-11-02-assist-execute-design.md`](../superpowers/specs/2026-11-02-assist-execute-design.md) §8. Does **not** enable `JARVISE_LIVE_TRADING`.

1. Create an Ed25519 PEM API key (for example "Jarvise-trade") on the account that will hold assist futures positions.
2. Permissions: **Enable Futures** on; **Withdrawals**, Spot, and Margin **off**; **IP restrict** to the VPS egress.
3. Store under `/opt/jarvise/secrets` (compose mounts `./secrets` → `/opt/jarvise/secrets`) with the same owner/mode as the read PEM. Point env at that path:
   - `BINANCE_TRADE_API_KEY`
   - `BINANCE_TRADE_PRIVATE_KEY_PATH`
   Never reuse `BINANCE_API_*` (the read key) for writes.
4. Verify (booleans only; never prints secrets):

```bash
docker compose exec jobs jarvise exchange check-key --assist-trade --json
```

Expect `ok: true`, `enableFutures: true`, `enableWithdrawals: false`, `ipRestrict: true`. Exit 3 = unsafe; exit 2 = missing/refused auth reason.
5. Futures account settings (Binance → Futures → Preferences). Jarvise probes both before every execute and refuses if either is wrong:
   - **Position mode: One-way** (not Hedge). Probe `GET /fapi/v1/positionSide/dual` → `dualSidePosition: false`; refusal text "บัญชีต้องเป็น one-way mode" (`src/jarvise_assist/execute.py` `MSG_ONE_WAY`).
   - **Asset mode: Single-Asset** (not Multi-Assets). Probe `GET /fapi/v1/multiAssetsMargin` → `multiAssetsMargin: false` (`MSG_MULTI_ASSETS`).
6. Margin type and leverage: Jarvise sets **Isolated** (`marginType=ISOLATED`) and the signal's leverage on the symbol before each entry (`set_isolated`, `set_leverage`). Do not switch an assist symbol to Cross by hand while a position is open; the signal message warns when the account disagrees (`src/jarvise_assist/messages.py` `account_leverage_warning_lines`).
7. Testnet: use a demo.binance.com futures key and set `JARVISE_ASSIST_EXECUTE=testnet` (owner only). Live assist execute uses `JARVISE_ASSIST_EXECUTE=live` after the same key audit.

## Current VPS status

Refreshed 2026-11-05 from the repo only; confirm on the VPS before acting (`docker compose exec jobs jarvise exchange check-key --assist-trade --json`). Do not paste `.env` values.

- `JARVISE_LIVE_TRADING` remains **false** (spot live path off).
- `BINANCE_TRADE_*` on the VPS is the **assist futures** key, composed into **jobs only** (see §1 note). The spot live path has no key wired into web.
- Assist futures execute is gated by `JARVISE_ASSIST_EXECUTE` (`off` / `testnet` / `live`). Its current value is shown on the dashboard Exchange page.

History:

- 2026-10-01: read-only key present (Ed25519 PEM); trade keys not yet set.
- Permission probe CLI shipped: `jarvise exchange check-key`.
- Live submit refuses keys with withdraw / transfer enabled.
- 2026-10-05: kill-switch flatten shipped, dormant while the flag is false (`jarvise trade flatten`; spec [`2026-10-05-live-killswitch-flatten-design.md`](../superpowers/specs/2026-10-05-live-killswitch-flatten-design.md)). Live stops and sells are now fee-net and `LOT_SIZE`-floored. Smoke step 8 is still open.
