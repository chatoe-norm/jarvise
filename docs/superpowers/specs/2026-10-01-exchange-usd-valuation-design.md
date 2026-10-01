# Jarvise §5.8a — exchange spot USD valuation — design

**Date:** 2026-10-01  
**Status:** APPROVED  
**Parent:** Ops hygiene before live (§5.8)  
**Depends on:** P3 read-only exchange balances

## Goal

On `/analytics` Exchange (spot) card, show **~USD per asset** and a **portfolio ~USD total**, using Binance public `USDT` last prices. Soft-fail behavior for missing keys unchanged.

## Non-goals

- Paper vs exchange reconciliation / PnL
- Multi-venue pricing
- Signed endpoints / trade keys
- Changing balance sync persistence schema
- Notifications, doctrine re-sync, live flag enable (other §5.8 slices)

## Locked decisions

1. **Price source:** Binance public `GET /api/v3/ticker/price?symbol={ASSET}USDT` (no auth).
2. **Display:** Per-asset ~USD column + portfolio Total ~USD.
3. **Unpriced assets:** Show `—` for ~USD; exclude from total; panel still renders.
4. **Stables at face:** `USDT`, `USDC`, `FDUSD`, `BUSD` → $1 (no ticker).
5. **Placement:** Valuation helper in `jarvise_exchange` (GET-only public client); web renders only.
6. **Label:** `~USD` (USDT proxy, not FX).

## Architecture

```text
sync_spot_balances → SpotBalance[]
        │
        ▼
value_spot_balances(balances, price_fn)
        │
        ▼
ValuedBalance[] + total_usd
        │
        ▼
/analytics Exchange card
```

## Testing

- Stablecoin face value
- Mocked BTCUSDT ticker → correct ~USD
- Missing ticker → `—` / excluded from total
- Regression: no order POST paths in exchange package

## Success

Owner sees meaningful ~USD totals on `/analytics` when balances sync succeeds; deploy stays paper-only / live flag untouched.
