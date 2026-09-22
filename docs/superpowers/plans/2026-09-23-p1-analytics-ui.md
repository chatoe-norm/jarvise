# P1 Analytics UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show latest paper `analysis_output` on the VPS control web (`/analytics`) with symbol/timeframe filters, JSON API, and SQLite mounted into the web container — still PAPER ONLY, no trade buttons.

**Architecture:** Persist `timeframe` on `analysis_output` (engine already emits it). Add `list_analysis_output` in `jarvise_ingest.db`. Extend `jarvise_web` with `/analytics` + `/api/analysis`; keep `/` as kill-switch/control. Mount `./data/analytics` on the web service with `JARVISE_DB`.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, Docker Compose, pytest / TestClient.

**Spec:** [docs/superpowers/specs/2026-09-23-product-roadmap-design.md](../specs/2026-09-23-product-roadmap-design.md) (P1 only)

**Status:** Implemented on this branch (2026-09-23).

## Global Constraints

- PAPER ONLY — no order placement, approve, or live trading UI.
- Kill-switch / ladder: paper → manual approval → autonomy (unchanged).
- Numeric truth remains SQLite `data/analytics/jarvise.db`.
- Tailscale is private ingress only; analytics UI binds like existing web.
- Do not commit secrets; do not edit Cursor plan files under `.cursor/plans/`.

---

### Task 1: Persist `timeframe` on `analysis_output`

**Files:**
- Modify: `src/jarvise_ingest/db.py`
- Modify: `tests/test_analyze_cli.py`

- [x] Extend `analysis_output` DDL with `timeframe TEXT NOT NULL DEFAULT ''`
- [x] Add `_migrate_analysis_timeframe` ALTER if column missing; call from `migrate`
- [x] Update `upsert_analysis_output` to insert/update `timeframe`
- [x] Assert analyze CLI persists `timeframe == "4h"`
- [ ] Commit: `feat: persist analysis_output.timeframe for analytics filters` (when user asks)

### Task 2: `list_analysis_output`

**Files:**
- Modify: `src/jarvise_ingest/db.py`
- Modify: `tests/test_db_upsert.py`

- [x] Add `list_analysis_output(conn, *, symbol=None, timeframe=None, limit=50) -> list[dict]`
- [x] Order by `timestamp DESC`; optional filters
- [x] Unit tests with temp DB
- [ ] Commit (when user asks)

### Task 3: Web `/analytics` + `/api/analysis`

**Files:**
- Modify: `src/jarvise_web/app.py`
- Modify: `tests/test_web.py`

- [x] Resolve DB via `JARVISE_DB` env (default `data/analytics/jarvise.db`)
- [x] HTML table + GET form filters; empty state message
- [x] JSON API `{paper_only, rows}`; nav Control ↔ Analytics
- [x] Tests with temp SQLite + redis/qdrant stubs
- [ ] Commit (when user asks)

### Task 4: Compose mount

**Files:**
- Modify: `docker-compose.yml`

- [x] `web.volumes`: `./data/analytics:/data/analytics`
- [x] `JARVISE_DB=/data/analytics/jarvise.db`
- [ ] Commit (when user asks)

### Task 5: Docs links

**Files:**
- Modify: `docs/product-usage.md`, `README.md`, roadmap Related / P1

- [x] Link this plan; note `http://$TAILSCALE_IP:8080/analytics`
- [ ] Commit (when user asks)

## Verify

```powershell
.venv\Scripts\python.exe -m pytest tests/test_web.py tests/test_analyze_cli.py tests/test_db_upsert.py -v
```

Expected: all passed (11 tests as of implementation).

## Out of scope

P2 paper ledger, P3 exchange balances, approve buttons, chart libraries, public HTTPS.
