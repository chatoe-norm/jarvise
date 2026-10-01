# P4-C live submit — Implementation Plan

> **Gate:** Do **not** start Tasks until owner marks the design APPROVED.  
> **Spec:** [docs/superpowers/specs/2026-09-27-p4c-live-submit-design.md](../specs/2026-09-27-p4c-live-submit-design.md)

**Goal:** Optional live Binance spot MARKET on Approve when `JARVISE_LIVE_TRADING=true`; default remains paper-only.

**Architecture:** `jarvise_trade` (order POST only) + reuse `jarvise_risk` + `live_orders` audit. Never add trade POSTs to `jarvise_exchange`.

## File map

| Path | Responsibility |
|------|----------------|
| `src/jarvise_trade/` | Signed spot MARKET place; allowlist |
| `src/jarvise_ingest/db.py` | `live_orders` schema + helpers |
| `src/jarvise_paper/approval.py` | Branch Approve on live flag |
| `src/jarvise_web/app.py` | LIVE APPROVAL ENABLED banner |
| `.env.example` / compose | `JARVISE_LIVE_TRADING=false` + trade key names |
| `tests/test_trade_*.py` | Flag-off / caps / mock POST |

## Tasks (after APPROVED)

1. Schema `live_orders` + DB helpers (TDD)
2. `jarvise_trade` Binance spot MARKET client (mock HTTP; no live keys in CI)
3. Wire `approve_approval`: live flag → risk → place → audit; flag off unchanged
4. Web banner + status fields
5. Docs / ops: key permission checklist (spot trade only, no withdraw)
6. Owner-gated enablement on VPS (flag stays false in repo defaults)

## Out of scope

P5, live timeout flatten, multi-venue, withdraw, futures
