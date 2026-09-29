# Jarvise — paper risk caps — design

**Date:** 2026-09-29  
**Status:** Shipped  
**Related:** `PROJECT_CONTEXT.md` §5.5; roadmap P4 Reject/timeout → FLAT

## Decisions

1. Package `jarvise_risk` with env: `JARVISE_MAX_NOTIONAL_PER_ORDER` (default 2000), `JARVISE_MAX_DAILY_LOSS_USD` (100), `JARVISE_DRAWDOWN_LOCK_PCT` (5).
2. Enforce on **enqueue** and **approve** (paper).
3. Breach → `mark_approval_failed` (approve) or skip enqueue + **engage kill-switch**.
4. **Timeout → FLAT**: `expire_approvals` applies `action=flat` when an open paper position exists for the timed-out symbol, then marks `timed_out` / `timeout_flat` (roadmap-aligned; overrides slice-B “timed_out only”).
5. Surface caps on `/analytics` + `/api/status.risk_caps`.
6. No live order placement in this slice.
