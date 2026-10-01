# Jarvise P4-C — live submit on Approve — design

**Date:** 2026-09-27; **refreshed:** 2026-10-01  
**Status:** APPROVED 2026-10-01 — implementing gated live submit (`JARVISE_LIVE_TRADING` default false).
**Depends on:** §5.1–5.5 shipped (paper approval, jobs 24/7, expectancy, replay, risk caps + timeout→FLAT paper)  
**Roadmap:** [`2026-09-23-product-roadmap-design.md`](2026-09-23-product-roadmap-design.md) P4 (live leg)  
**Prior slice:** [`2026-09-23-p4-manual-approval-paper-slice-design.md`](2026-09-23-p4-manual-approval-paper-slice-design.md)  
**Risk (paper, shared):** [`2026-09-29-paper-risk-caps-design.md`](2026-09-29-paper-risk-caps-design.md) → package `jarvise_risk`  
**Related:** [`2026-09-23-p3-exchange-readonly-design.md`](2026-09-23-p3-exchange-readonly-design.md) (read-only stays; trade module is separate)

## Goal

On **Approve** of a pending `approval_queue` row, optionally place a **size-capped live Binance spot order** and record it in `live_orders`, while keeping autonomy **off** and paper fills as the default path until explicitly gated.

Climb: paper → **manual approval with live submit** → (later) autonomy. Do not enable P5 in this slice.

## Non-goals (this slice)

- P5 autonomy / unattended live submit
- Live position timeout → FLAT / auto-close on venue (paper timeout→FLAT already shipped in §5.5)
- Futures / margin / OCO / cancel / withdraw APIs
- Multi-venue live routing (Binance spot only for C)
- Changing ingest, RAG, or read-only balance sync allowlists
- Replacing the paper ledger — paper Approve path remains when live flag is off

## Locked decisions

1. **Gate:** Live submit only when `JARVISE_LIVE_TRADING=true` (default **false**). When false, Approve = paper `apply_signal` only (P4-B + risk caps).
2. **Separate trade module:** New package `jarvise_trade` — **do not** add order POSTs into `jarvise_exchange.binance_spot`. Read package stays GET-only.
3. **Trade keys:** `BINANCE_TRADE_API_KEY` + `BINANCE_TRADE_API_SECRET` (HMAC) or PEM via `BINANCE_TRADE_PRIVATE_KEY_PATH`. Distinct from read-only balance keys. Never render in HTML/logs.
4. **Venue / order type:** Binance global **spot MARKET**; side from queue `action` (`long`→BUY; `flat` with open live inventory → SELL sized by plan — details in plan). No limit/IOC in C.
5. **Sizing / caps:** Reuse **`jarvise_risk`** (`JARVISE_MAX_NOTIONAL_PER_ORDER`, `JARVISE_MAX_DAILY_LOSS_USD`, `JARVISE_DRAWDOWN_LOCK_PCT`) plus live-day PnL from `live_orders`. Optional tighter live override env only if needed; prefer one shared cap surface.
6. **Kill-switch:** Engaged → no enqueue and no live or paper Approve fills (already).
7. **Audit:** Every attempt writes `live_orders` (requested, submitted, venue ack/error, timestamps, approval_id).
8. **UI:** When live flag on, `/analytics` shows **LIVE APPROVAL ENABLED** (not PAPER ONLY alone). No autonomy controls.
9. **Paper still first:** Live Approve does **not** also write paper fills (no double ledger).

## Architecture

```text
                 JARVISE_LIVE_TRADING?
Approve ──────────── false ──► jarvise_risk → apply_signal (paper)
   │
   └── true ──► jarvise_risk + kill-switch
                  │
                  ├── fail → approval failed; live_orders attempt=error
                  └── ok → jarvise_trade spot MARKET POST
                              │
                              ▼
                         live_orders (audit only; no paper mirror)
```

**Boundary:** Trade HTTP only in `jarvise_trade` allowlist (`POST /api/v3/order` for this slice).

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

## Caps (env)

| Env | Default | Role |
|-----|---------|------|
| `JARVISE_LIVE_TRADING` | `false` | Master live gate |
| `JARVISE_MAX_NOTIONAL_PER_ORDER` | `2000` | Shared with paper (`jarvise_risk`) — consider tighter live default in plan |
| `JARVISE_MAX_DAILY_LOSS_USD` | `100` | Paper equity + live day PnL checks |
| `JARVISE_DRAWDOWN_LOCK_PCT` | `5` | Shared drawdown lock |
| Trade API key/secret (or PEM) | unset | Required only when live flag true |

## Error handling

- Missing trade keys with live flag on → Approve fails closed (`failed`); no silent paper fill
- Venue reject / network → `live_orders` row + approval `failed`
- Flat / zero size → no live HTTP

## Testing (plan expands)

- Unit: live flag off → zero trade HTTP
- Unit: caps + kill-switch block
- Mock: signed POST allowlist only
- Regression: `jarvise_exchange` still GET-only; paper suite green

## Success criteria

1. Spec + implementation plan accepted before product code
2. Default deploy: Approve remains paper-only
3. With live flag + keys + caps: one Approve → at most one size-capped spot order + audit
4. No autonomy, no withdraw, no futures
5. Review: read-only exchange package unchanged for trade POSTs

## Follow-ups (out of C)

- Live timeout / venue flatten
- P5 autonomy
- Multi-venue live adapters
- Ops hygiene (§5.8): notifications, USD balance valuation, key permission audit

## Stabilization evidence (pre-req)

- VPS smoke 2026-09-27: paper approval loop
- 2026-09-29: paper jobs, expectancy, replay, risk caps + paper timeout→FLAT on `main` (`aa79a40`+)
