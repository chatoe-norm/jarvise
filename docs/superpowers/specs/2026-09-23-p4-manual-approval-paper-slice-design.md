# Jarvise P4 — manual approval (paper slice A→B) — design

**Date:** 2026-09-23  
**Status:** Design approved; implementation plan ready  
**Depends on:** P2 paper ledger + `/analytics`; P3 exchange read-only remains orthogonal (no trade POSTs)  
**Roadmap:** [`2026-09-23-product-roadmap-design.md`](2026-09-23-product-roadmap-design.md) P4 (thin slice before live submit)  
**Plan:** [`../plans/2026-09-23-p4-manual-approval-paper-slice.md`](../plans/2026-09-23-p4-manual-approval-paper-slice.md)

## Goal

Climb one rung toward roadmap P4 without live exchange orders:

1. **Phase A:** This design + an implementation plan (no product code until plan exists).
2. **Phase B:** Candidates that `jarvise paper` would trade today enter SQLite `approval_queue` by default; owner Approves/Rejects on `/analytics`; Approve runs the existing paper `apply_signal` fill path.

Deployment ladder stays **paper → manual approval → autonomy**. Live trading and autonomy flags remain **off**. This slice is still **PAPER ONLY**.

## Non-goals (this slice)

- Any Binance / venue **order, trade, cancel, or withdraw** HTTP (signed POST) — that is **P4-C** (later)
- Timeout → FLAT / automatic close of open paper positions
- Autonomy / unattended live submit (P5)
- Multi-venue approval routing
- Changing ingest, RAG, or exchange read-only sync
- Requiring n8n for expire (CLI `paper expire` is enough for B; schedule optional later)

## Locked decisions

1. Extend **`jarvise_paper`** (Approach 1) — reuse fill math, kill-switch, ledger
2. Queue source = same candidates as today’s `paper run` path (candle + analysis)
3. `paper run` **defaults to enqueue**; `--auto-fill` restores immediate `apply_signal`
4. Timeout → status `timed_out` only (default **60 minutes**, env `JARVISE_APPROVAL_TIMEOUT_MIN`)
5. Approve / Reject UI = card on **`/analytics`** (Tailscale + existing basic auth)
6. Kill-switch engaged → no enqueue and no Approve fills
7. Dedup: at most one **pending** row per `(symbol, timeframe)`
8. Approve uses mid from the latest **closed** candle at approve time (not stale enqueue mid)
9. Still **PAPER ONLY** banner; no live-order UI copy or code path

## Architecture

```text
jarvise paper run
  │  kill-switch? → skip (exit 3)
  │  resolve symbols + closed candle + analysis (same as P2)
  │  --auto-fill? → apply_signal (legacy immediate fill)
  ▼
approval_queue (SQLite)   status=pending, expires_at_ms
  │
  ├── /analytics Approve|Reject  ──┐
  ├── jarvise paper approve <id>   ├── Approve → apply_signal → paper_orders / paper_positions
  ├── jarvise paper reject <id>    └── Reject / expire → status only (no FLAT)
  └── jarvise paper expire
```

**Boundary:** Slice B may only mutate the **paper** ledger via `apply_signal`. It must not call exchange trade endpoints. `jarvise_exchange` stays read-only.

**Package touchpoints:**

```text
src/jarvise_ingest/db.py      # approval_queue DDL + helpers
src/jarvise_paper/
  engine.py                   # unchanged apply_signal (reuse)
  approval.py                 # enqueue / approve / reject / expire / list (new)
  cli.py                      # run default enqueue; new subcommands
src/jarvise_web/app.py        # Approval queue card + POST handlers
tests/test_paper_approval*.py
tests/test_web.py             # approval card / POST stubs
```

## Data model

Default DB: `data/analytics/jarvise.db` (same as paper / analysis).

```sql
CREATE TABLE IF NOT EXISTS approval_queue (
  id TEXT NOT NULL PRIMARY KEY,
  created_at_ms INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  analysis_id TEXT,
  action TEXT NOT NULL,
  regime_state TEXT,
  confidence_score REAL,
  size_pct_equity REAL,
  status TEXT NOT NULL,
  resolved_at_ms INTEGER,
  resolve_reason TEXT,
  paper_order_ids_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_queue_status_expires
  ON approval_queue (status, expires_at_ms);
CREATE INDEX IF NOT EXISTS idx_approval_queue_symbol_tf_status
  ON approval_queue (symbol, timeframe, status);
```

**Status values:** `pending` | `approved` | `rejected` | `timed_out` | `failed`

**Dedup rule:** At most one `pending` row per `(symbol, timeframe)`. On re-enqueue, **update that row in place** (refresh analysis snapshot + reset `created_at_ms` / `expires_at_ms`; keep the same `id`). Do not stack duplicates.

**Timestamps:** INTEGER Unix milliseconds UTC.

Document the same `CREATE TABLE` in `data/analytics/mvas-schema.sql` for discoverability; runtime writer lives with other paper helpers in `jarvise_ingest.db`.

## Components

### 1. Enqueue (`approval.enqueue_from_run` / inside `cmd_run`)

Same preamble as today’s `paper run`:

1. Kill-switch → skip, exit 3, write nothing
2. Resolve symbols (`--symbol` / `--universe`)
3. Load latest closed candle; skip symbol with error if missing
4. Analyze or `--skip-analyze` load `analysis_output`
5. Unless `--auto-fill`: upsert pending `approval_queue` row; **do not** call `apply_signal`
6. With `--auto-fill`: call `apply_signal` as today (no queue required)

Enqueue even for `action=flat` if today’s engine would act on it (keep parity with P2). If product later wants to skip flats, that is a follow-up filter — not this slice’s default.

### 2. Approve

1. Load row by `id`; must be `pending`
2. Kill-switch → refuse (row stays pending)
3. Load latest closed candle for symbol+timeframe; missing → mark `failed` + reason
4. Rebuild analysis dict from queue snapshot fields (+ symbol)
5. `apply_signal(...)`; on success mark `approved`, store fill order ids JSON if available; on engine error mark `failed`

### 3. Reject

Mark `rejected`, set `resolved_at_ms` and optional `resolve_reason`. No ledger change.

### 4. Expire

For each `pending` where `expires_at_ms <= now`: set `timed_out`. No FLAT, no ledger change. Idempotent.

Default TTL: `JARVISE_APPROVAL_TIMEOUT_MIN` (default `60`).

### 5. CLI

| Command | Behavior |
|---------|----------|
| `jarvise paper run` | Enqueue default |
| `jarvise paper run --auto-fill` | Immediate fill (dev escape) |
| `jarvise paper queue` | List pending (optional `--all` recent resolved) |
| `jarvise paper approve <id>` | Approve path |
| `jarvise paper reject <id> [--reason …]` | Reject |
| `jarvise paper expire` | Mark timed-out pendings |
| `jarvise paper status` | Existing ledger; include pending count when cheap |

`--json` / text emit stay consistent with existing paper CLI style.

### 6. Web

On `GET /analytics`:

- Card **Approval queue** listing pending rows (symbol, TF, action, confidence, size%, expires)
- Forms POST to `/approvals/approve` and `/approvals/reject` with `id` (same auth as `/kill-switch`)
- Empty queue → muted “No pending approvals”
- Never imply live exchange execution

## Error handling

| Case | Behavior |
|------|----------|
| Kill-switch on `paper run` | Exit 3; no enqueue |
| Kill-switch on Approve | Error response / CLI non-zero; row stays pending |
| Missing candle on Approve | Status `failed` + reason; no fill |
| Approve/Reject non-pending id | No-op error |
| Expire | Idempotent; only pending past expiry |
| `--auto-fill` + kill-switch | Same as today’s paper run skip |

No interactive prompts.

## Testing

- Unit: enqueue creates pending; second run same symbol+TF replaces pending (no stack)
- Unit: `--auto-fill` still fills paper ledger without requiring Approve
- Unit: Approve → paper fill + `approved`; Reject / expire → no position/order change
- Unit: kill-switch blocks Approve fill
- Unit: expire marks `timed_out` after `expires_at_ms`
- Web: pending row renders; POST approve (stubbed) updates path without live HTTP
- Regression: existing paper + web tests pass; no exchange trade endpoints introduced

## Success criteria

1. Spec + plan accepted (A) before product code for B
2. `paper run` without `--auto-fill` leaves a pending queue row and does **not** change paper positions
3. Approve on `/analytics` or CLI produces the same class of paper fill as today’s auto path
4. Reject / timeout never close positions in this slice
5. Kill-switch respected on enqueue and Approve
6. Code review gate: **no** signed trade/order POST allowlist additions in `jarvise_exchange` or new trade modules

## Follow-ups (explicitly out of B)

- **P4-C:** On Approve, size-capped **live** spot order + `live_orders` audit (only after B is stable)
- Roadmap timeout → FLAT behavior
- n8n / jobs schedule for `paper expire`
- Hard notional / daily-loss caps on live path (needed for P4-C; optional display-only caps later)

## Related

- [Product roadmap](2026-09-23-product-roadmap-design.md)
- [P2 paper auto-trade plan](../plans/2026-09-23-p2-paper-auto-trade.md)
- [P3 exchange read-only design](2026-09-23-p3-exchange-readonly-design.md)
- [Product usage](../../product-usage.md)
