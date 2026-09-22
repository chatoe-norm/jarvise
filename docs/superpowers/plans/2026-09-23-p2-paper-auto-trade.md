# P2 Paper auto-trade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simulate fills from paper `analyze` signals + closed candle prices into a local ledger; expose `jarvise paper run|status` and show equity/positions on `/analytics` — still no exchange order APIs.

**Architecture:** SQLite `paper_account` / `paper_orders` / `paper_positions`; fill engine with 5 bps fee + 5 bps slippage; CLI re-analyzes then fills (or `--skip-analyze`); kill-switch via Redis when `REDIS_URL` is set; web `/api/paper` + ledger card on `/analytics`.

**Tech Stack:** Python 3.11+, sqlite3, Typer/argparse, FastAPI, pytest.

**Spec:** [docs/superpowers/specs/2026-09-23-product-roadmap-design.md](../specs/2026-09-23-product-roadmap-design.md) (P2 only)

**Status:** Implementing on `feature/p2-paper-auto-trade`.

## Global Constraints

- PAPER ONLY simulated ledger — no signed exchange / live order endpoints.
- Starting equity 10_000 USD; fee 5 bps/side; slippage 5 bps adverse.
- Kill-switch engaged → exit 3, write nothing.
- Numeric truth remains `data/analytics/jarvise.db`.

---

### Task 1: Schema + DB helpers

- [x] DDL for `paper_orders`, `paper_positions`, `paper_account`
- [x] Helpers: ensure/list/upsert paper + performance metrics
- [x] Tests

### Task 2: Engine + CLI

- [x] `src/jarvise_paper/` engine + cli
- [x] `jarvise paper` Typer delegate + `jarvise-paper` script
- [x] Tests for fill math, dry-run, kill-switch

### Task 3: Web

- [x] Paper ledger on `/analytics` + `GET /api/paper`

### Task 4: Docs

- [x] Link this plan; product-usage mentions `jarvise paper run`

## Verify

```powershell
.venv\Scripts\python.exe -m pytest tests/test_paper_engine.py tests/test_paper_cli.py tests/test_web.py tests/test_db_upsert.py -v
```

## Out of scope

P3 exchange APIs, P4 approval, P5 autonomy, n8n paper schedule.
