# Jarvise P4-C — live submit on Approve — design

**Date:** 2026-09-27  
**Status:** Draft for owner review — **no implementation until this spec is approved and an implementation plan exists**  
**Depends on:** P4 paper approval slice B stable (enqueue → Approve → paper fill path proven on VPS 2026-09-27)  
**Roadmap:** [`2026-09-23-product-roadmap-design.md`](2026-09-23-product-roadmap-design.md) P4 (live leg)  
**Prior slice:** [`2026-09-23-p4-manual-approval-paper-slice-design.md`](2026-09-23-p4-manual-approval-paper-slice-design.md)  
**Related:** [`2026-09-23-p3-exchange-readonly-design.md`](2026-09-23-p3-exchange-readonly-design.md) (read-only stays; trade module is separate)

## Goal

On **Approve** of a pending `approval_queue` row, optionally place a **size-capped live Binance spot order** and record it in `live_orders`, while keeping autonomy **off** and paper fills available as the default/safe path until explicitly gated.

Climb: paper → **manual approval with live submit** → (later) autonomy. Do not enable P5 in this slice.

## Non-goals (this slice)

- P5 autonomy / unattended live submit
- Timeout → FLAT (auto-close open live or paper positions)
- Futures / margin / OCO / cancel / withdraw APIs
- Multi-venue live routing (Binance spot only)
- Changing ingest, RAG, or read-only balance sync allowlists
- Replacing the paper ledger — paper Approve path remains for `--paper-only` / when live flag is off

## Locked decisions (from roadmap + P4-B follow-ups + owner prefs)

1. **Gate:** Live submit only when `JARVISE_LIVE_TRADING=true` (default **false**). When false, Approve behaves exactly as P4-B (paper `apply_signal` only).
2. **Separate trade module:** New package `jarvise_trade` — **do not** add order POSTs into `jarvise_exchange.binance_spot`. Review gate: read package stays GET-only account/balance.
3. **Trade keys:** `BINANCE_TRADE_API_KEY` + `BINANCE_TRADE_API_SECRET` (HMAC) or PEM pair via `BINANCE_TRADE_PRIVATE_KEY_PATH` (same signing patterns as P3 read path). Distinct from read-only balance keys. Never render in HTML/logs.
4. **Venue / order type:** Binance global **spot MARKET** order for the approved `symbol`; side from queue `action` (`long`→BUY, `flat`/`reduce`→SELL as sized by plan). No limit/IOC in C.
5. **Sizing:** Cap notional by `min(size_pct_equity × configured_live_equity, JARVISE_MAX_ORDER_NOTIONAL_USD)`; reject Approve if caps fail (queue → `failed`, no partial live leave).
6. **Daily loss:** Before submit, if realized day PnL from `live_orders` that UTC day ≤ `-JARVISE_MAX_DAILY_LOSS_USD`, block live Approve.
7. **Kill-switch:** Engaged → no enqueue (already) and no live or paper Approve fills.
8. **Audit:** Every attempt writes `live_orders` (requested, submitted, venue ack / error, timestamps, approval_id).
9. **UI:** `/analytics` keeps Approve/Reject; when live flag on, banner must **not** say PAPER ONLY alone — show explicit **LIVE APPROVAL ENABLED** warning. No autonomy controls.
10. **Paper still first:** `--auto-fill` and live-off Approve remain paper-only simulated fills. Live Approve does **not** also write paper fills (no double ledger); paper and live stay separate.

## Architecture

```text
                 JARVISE_LIVE_TRADING?
Approve ──────────── false ──► apply_signal (paper) ──► paper_* tables
   │
   └── true ──► risk caps + kill-switch
                  │
                  ├── fail → approval status=failed; live_orders attempt=error
                  └── ok → signed spot MARKET POST
                              │
                              ▼
                         live_orders (audit only; no paper mirror)
```

**Boundary:** Read-only sync path unchanged. Trade HTTP lives only in `jarvise_trade` with an explicit allowlist (`POST /api/v3/order` only for this slice).

## Data model (contract)

```sql
CREATE TABLE IF NOT EXISTS live_orders (
  id TEXT NOT NULL PRIMARY KEY,
  created_at_ms INTEGER NOT NULL,
  approval_id TEXT,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  requested_qty REAL,
  requested_notional_usd REAL,
  status TEXT NOT NULL,
  venue_order_id TEXT,
  venue_response_json TEXT,
  error TEXT,
  kill_switch_clear INTEGER NOT NULL,
  caps_ok INTEGER NOT NULL
);
```

Exact columns land in the implementation plan; names above are the roadmap contract.

## Caps (env)

| Env | Default (proposal) | Role |
|-----|--------------------|------|
| `JARVISE_LIVE_TRADING` | `false` | Master live gate |
| `JARVISE_MAX_ORDER_NOTIONAL_USD` | `50` | Per-order notional cap |
| `JARVISE_MAX_DAILY_LOSS_USD` | `50` | Block further live Approves that day |
| Trade API key/secret (or PEM) | unset | Required only when live flag true |

## Error handling

- Missing trade keys with live flag on → Approve fails closed (`failed`), no paper fill unless owner uses explicit paper path
- Venue reject / network → `live_orders` row + approval `failed`
- Flat / zero size actions → no live HTTP (same as paper: no-op fill)

## Testing (plan will expand)

- Unit: caps block oversize and daily-loss
- Unit: live flag off → zero trade HTTP (mock)
- Unit: kill-switch blocks
- Integration mock: signed POST allowlist only
- Regression: P3 read-only package still has no order functions; P4-B paper tests pass

## Success criteria

1. Spec + implementation plan accepted before product code
2. Default deploy: Approve remains paper-only
3. With live flag + keys + caps: one Approve places at most one size-capped spot order and audits it
4. No autonomy flag, no withdraw, no futures
5. Code review: read-only exchange package unchanged for trade POSTs

## Follow-ups (out of C)

- Timeout → FLAT
- P5 autonomy
- n8n schedules for enqueue/expire
- Multi-venue live adapters

## Stabilization evidence (pre-req)

VPS smoke 2026-09-27 (`def1f8f`): ingest BTCUSDT 4h → analyze → `paper run` enqueue → `paper approve` → status `approved` (action was `flat`, fills empty, equity 10000). Paper approval loop works; live not exercised.
