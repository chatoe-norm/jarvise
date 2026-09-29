# Jarvise — historical analyze replay — design

**Date:** 2026-09-29  
**Status:** Shipped (this PR)  
**Depends on:** stored OHLCV + indicators; paper engine; expectancy metrics (§5.3)  
**Related:** `PROJECT_CONTEXT.md` §5.4

## Locked decisions

1. CLI surface: extend `jarvise analyze` with `--replay` (not a new top-level command)
2. Optional `--apply-paper` for fills; default analyze-only
3. `--apply-paper` requires isolated `--db` ≠ default live ledger (hard refuse)
4. Apply path uses `apply_signal` directly (no approval queue)
5. Analysis upserts are idempotent via existing content-hash `analysis_id`
6. Apply-paper resets paper tables in the isolated DB before the loop

## CLI

```
jarvise analyze --replay --since ISO [--until ISO] \
  [--symbol … | --universe paper_core] [--timeframe 4h] \
  [--db PATH] [--apply-paper] [--dry-run] [--json]
```

`--replay` requires `--since`. `--apply-paper` refuses default live DB path.

## Loop

For each symbol: load closed candles in `[since, until]` with indicators → `analyze_snapshot` → upsert (unless dry-run) → optional `apply_signal(mid=close, now_ms=ts)`. End report includes counts; with apply-paper, embed metrics summary.

## Out of scope

UI backtest, n8n, live orders, risk caps, approval-queue during replay
