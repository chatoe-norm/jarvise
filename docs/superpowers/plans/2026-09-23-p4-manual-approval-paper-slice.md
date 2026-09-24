# P4 Manual Approval (Paper Slice) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `jarvise paper run` enqueue candidates into SQLite `approval_queue` by default; let the owner Approve/Reject on `/analytics` (or CLI); Approve runs existing paper `apply_signal` only — still no exchange order APIs.

**Architecture:** Extend `jarvise_paper` with `approval.py` (enqueue / approve / reject / expire / list). Persist queue rows via helpers in `jarvise_ingest.db`. CLI defaults to enqueue (`--auto-fill` restores immediate fill). Web adds an Approval queue card + POST handlers on `/analytics`. Kill-switch blocks enqueue and Approve fills. Timeout marks `timed_out` only (no FLAT).

**Tech Stack:** Python 3.11+, sqlite3, argparse (`jarvise_paper.cli`), FastAPI (`jarvise_web`), pytest.

**Spec:** [docs/superpowers/specs/2026-09-23-p4-manual-approval-paper-slice-design.md](../specs/2026-09-23-p4-manual-approval-paper-slice-design.md)

## Global Constraints

- PAPER ONLY — no Binance/venue order, trade, cancel, or withdraw HTTP anywhere in this slice.
- Do not modify `jarvise_exchange` trade allowlist; leave exchange package read-only.
- `paper run` default = enqueue; `--auto-fill` = immediate `apply_signal` (escape hatch).
- Timeout → `timed_out` status only; never auto-close paper positions in this slice.
- Kill-switch engaged → no enqueue and no Approve fills (row stays `pending` on Approve refuse).
- Dedup: at most one `pending` row per `(symbol, timeframe)`; re-enqueue updates in place (same `id`).
- Approve mid = latest **closed** candle close at approve time (not enqueue-time mid).
- Default TTL: `JARVISE_APPROVAL_TIMEOUT_MIN` env, default `60`.
- Timestamps: INTEGER Unix milliseconds UTC.
- Commit only when the user asks; plan commit steps are for the implementing agent when commits are authorized.

---

## File map

| Path | Responsibility |
|------|----------------|
| `src/jarvise_ingest/db.py` | `approval_queue` in `SCHEMA_SQL` + CRUD helpers |
| `data/analytics/mvas-schema.sql` | Document `approval_queue` DDL |
| `src/jarvise_paper/approval.py` | enqueue / approve / reject / expire / list / timeout helpers |
| `src/jarvise_paper/cli.py` | `--auto-fill`, `queue` / `approve` / `reject` / `expire`; run default enqueue |
| `src/jarvise_paper/engine.py` | unchanged (`apply_signal` reuse only) |
| `src/jarvise_web/app.py` | Approval card on `/analytics`; POST `/approvals/approve` + `/approvals/reject` |
| `.env.example` | `JARVISE_APPROVAL_TIMEOUT_MIN=60` |
| `docs/product-usage.md` | Document enqueue default + approve path |
| `docs/superpowers/specs/2026-09-23-p4-manual-approval-paper-slice-design.md` | Point Plan link + status |
| `tests/test_approval_db.py` | Schema + helper behavior |
| `tests/test_paper_approval.py` | enqueue / approve / reject / expire / kill-switch / dedup |
| `tests/test_paper_cli.py` | Extend for `--auto-fill` + queue commands |
| `tests/test_web.py` | Approval card + POST stubs |

---

### Task 1: Schema + DB helpers

**Files:**
- Modify: `src/jarvise_ingest/db.py` (append to `SCHEMA_SQL` after `paper_positions`; add helpers)
- Modify: `data/analytics/mvas-schema.sql`
- Test: `tests/test_approval_db.py`

**Interfaces:**
- Produces:
  - `upsert_pending_approval(conn, row: dict) -> dict` — insert or update-in-place pending by `(symbol, timeframe)`; returns stored row
  - `get_approval(conn, approval_id: str) -> dict | None`
  - `list_approvals(conn, *, status: str | None = "pending", limit: int = 50) -> list[dict]`
  - `resolve_approval(conn, approval_id: str, *, status: str, resolve_reason: str | None = None, paper_order_ids_json: str | None = None, resolved_at_ms: int | None = None) -> dict | None` — only transitions from `pending`; returns updated row or `None` if not pending
  - `expire_pending_approvals(conn, *, now_ms: int) -> int` — count marked `timed_out`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_approval_db.py
from pathlib import Path

from jarvise_ingest.db import (
    expire_pending_approvals,
    get_approval,
    list_approvals,
    open_db,
    resolve_approval,
    upsert_pending_approval,
)


def test_upsert_pending_dedupes_symbol_timeframe(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "a.db")
    first = upsert_pending_approval(
        conn,
        {
            "id": "appr1",
            "created_at_ms": 1_000,
            "expires_at_ms": 3_600_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "a1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "size_pct_equity": 5.0,
            "status": "pending",
        },
    )
    second = upsert_pending_approval(
        conn,
        {
            "id": "appr_new_ignored",
            "created_at_ms": 2_000,
            "expires_at_ms": 4_000_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "a2",
            "action": "flat",
            "regime_state": "range",
            "confidence_score": 0.4,
            "size_pct_equity": 0.0,
            "status": "pending",
        },
    )
    assert second["id"] == first["id"]
    assert second["analysis_id"] == "a2"
    assert second["action"] == "flat"
    assert second["created_at_ms"] == 2_000
    assert len(list_approvals(conn, status="pending")) == 1


def test_resolve_and_expire(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "b.db")
    row = upsert_pending_approval(
        conn,
        {
            "id": "x1",
            "created_at_ms": 1_000,
            "expires_at_ms": 1_500,
            "symbol": "ETHUSDT",
            "timeframe": "1h",
            "analysis_id": "e1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.8,
            "size_pct_equity": 2.0,
            "status": "pending",
        },
    )
    assert resolve_approval(conn, row["id"], status="rejected", resolve_reason="nope")["status"] == "rejected"
    assert resolve_approval(conn, row["id"], status="approved") is None  # not pending

    upsert_pending_approval(
        conn,
        {
            "id": "x2",
            "created_at_ms": 1_000,
            "expires_at_ms": 1_500,
            "symbol": "SOLUSDT",
            "timeframe": "1h",
            "analysis_id": "s1",
            "action": "short",
            "regime_state": "trend_down",
            "confidence_score": 0.6,
            "size_pct_equity": 1.0,
            "status": "pending",
        },
    )
    assert expire_pending_approvals(conn, now_ms=2_000) == 1
    assert get_approval(conn, "x2")["status"] == "timed_out"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_approval_db.py -v`  
Expected: FAIL (helpers / table missing)

- [ ] **Step 3: Add DDL to `SCHEMA_SQL` and helpers**

Append to `SCHEMA_SQL` in `src/jarvise_ingest/db.py` (after `paper_positions`):

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

Implement helpers (sketch):

```python
def upsert_pending_approval(conn: sqlite3.Connection, row: dict) -> dict:
    symbol = str(row["symbol"]).upper()
    timeframe = str(row["timeframe"])
    existing = conn.execute(
        """
        SELECT id FROM approval_queue
        WHERE symbol = ? AND timeframe = ? AND status = 'pending'
        LIMIT 1
        """,
        (symbol, timeframe),
    ).fetchone()
    approval_id = existing["id"] if existing else str(row["id"])
    conn.execute(
        """
        INSERT INTO approval_queue (
            id, created_at_ms, expires_at_ms, symbol, timeframe, analysis_id,
            action, regime_state, confidence_score, size_pct_equity, status,
            resolved_at_ms, resolve_reason, paper_order_ids_json
        ) VALUES (
            :id, :created_at_ms, :expires_at_ms, :symbol, :timeframe, :analysis_id,
            :action, :regime_state, :confidence_score, :size_pct_equity, 'pending',
            NULL, NULL, NULL
        )
        ON CONFLICT(id) DO UPDATE SET
            created_at_ms=excluded.created_at_ms,
            expires_at_ms=excluded.expires_at_ms,
            analysis_id=excluded.analysis_id,
            action=excluded.action,
            regime_state=excluded.regime_state,
            confidence_score=excluded.confidence_score,
            size_pct_equity=excluded.size_pct_equity,
            status='pending',
            resolved_at_ms=NULL,
            resolve_reason=NULL,
            paper_order_ids_json=NULL
        """,
        {
            "id": approval_id,
            "created_at_ms": int(row["created_at_ms"]),
            "expires_at_ms": int(row["expires_at_ms"]),
            "symbol": symbol,
            "timeframe": timeframe,
            "analysis_id": row.get("analysis_id"),
            "action": str(row["action"]),
            "regime_state": row.get("regime_state"),
            "confidence_score": row.get("confidence_score"),
            "size_pct_equity": row.get("size_pct_equity"),
        },
    )
    conn.commit()
    return dict(get_approval(conn, approval_id))
```

Mirror the same `CREATE TABLE` into `data/analytics/mvas-schema.sql`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_approval_db.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when authorized)

```bash
git add src/jarvise_ingest/db.py data/analytics/mvas-schema.sql tests/test_approval_db.py
git commit -m "feat(paper): add approval_queue schema and helpers"
```

---

### Task 2: `jarvise_paper.approval` core

**Files:**
- Create: `src/jarvise_paper/approval.py`
- Test: `tests/test_paper_approval.py`

**Interfaces:**
- Consumes: DB helpers from Task 1; `apply_signal`; `load_latest_candle`; `kill_switch_engaged` (import from `jarvise_paper.cli` or move kill-switch to a tiny shared helper to avoid circular imports — prefer `jarvise_paper.guards.kill_switch_engaged` if needed, or pass `kill_switch: bool` into functions)
- Produces:
  - `approval_timeout_ms() -> int`
  - `enqueue_approval(conn, *, analysis: dict, timeframe: str, now_ms: int | None = None) -> dict`
  - `approve_approval(conn, approval_id: str, *, kill_switch: bool, now_ms: int | None = None) -> dict` — result shape `{"ok": bool, "approval": dict|None, "fills": list, "error": str|None, "paper_only": True}`
  - `reject_approval(conn, approval_id: str, *, reason: str | None = None, now_ms: int | None = None) -> dict`
  - `expire_approvals(conn, *, now_ms: int | None = None) -> dict` — `{"ok": True, "expired": N, "paper_only": True}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paper_approval.py
from pathlib import Path

from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_position,
    list_approvals,
    list_paper_orders,
    open_db,
    upsert_market_technicals,
)
from jarvise_paper.approval import (
    approve_approval,
    enqueue_approval,
    expire_approvals,
    reject_approval,
)


def _seed_candle(conn, symbol: str, timeframe: str, close: float, ts: int = 1_700_000_000_000) -> None:
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": symbol,
                "timestamp": ts,
                "timeframe": timeframe,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1.0,
            }
        ],
    )


def test_enqueue_approve_fills_paper(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    assert row["status"] == "pending"
    assert get_paper_position(conn, "BTCUSDT") is None

    result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)
    assert result["ok"] is True
    assert result["approval"]["status"] == "approved"
    assert get_paper_position(conn, "BTCUSDT") is not None
    assert list_paper_orders(conn)


def test_reject_and_expire_do_not_fill(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p2.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "ETHUSDT", "4h", 50.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "e1",
            "symbol": "ETHUSDT",
            "action": "long",
            "size_pct_equity": 5.0,
            "regime_state": "trend_up",
            "confidence_score": 0.6,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    reject_approval(conn, row["id"], reason="nope", now_ms=1_100)
    assert get_paper_position(conn, "ETHUSDT") is None

    row2 = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "e2",
            "symbol": "ETHUSDT",
            "action": "short",
            "size_pct_equity": 5.0,
            "regime_state": "trend_down",
            "confidence_score": 0.6,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    conn.execute("UPDATE approval_queue SET expires_at_ms = 500 WHERE id = ?", (row2["id"],))
    conn.commit()
    out = expire_approvals(conn, now_ms=2_000)
    assert out["expired"] == 1
    assert get_paper_position(conn, "ETHUSDT") is None
    assert list_approvals(conn, status="timed_out")


def test_kill_switch_blocks_approve(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "p3.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 10.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    result = approve_approval(conn, row["id"], kill_switch=True, now_ms=2_000)
    assert result["ok"] is False
    assert "kill" in (result["error"] or "").lower()
    assert row["id"] == list_approvals(conn, status="pending")[0]["id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_paper_approval.py -v`  
Expected: FAIL (`approval` module missing)

- [ ] **Step 3: Implement `src/jarvise_paper/approval.py`**

```python
"""Paper approval queue — simulated fills only. No exchange order APIs."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from jarvise_ingest.db import (
    expire_pending_approvals,
    get_approval,
    load_latest_candle,
    resolve_approval,
    upsert_pending_approval,
)
from jarvise_paper.engine import apply_signal

DEFAULT_TIMEOUT_MIN = 60


def approval_timeout_ms() -> int:
    raw = os.environ.get("JARVISE_APPROVAL_TIMEOUT_MIN", str(DEFAULT_TIMEOUT_MIN))
    try:
        minutes = max(1, int(raw))
    except ValueError:
        minutes = DEFAULT_TIMEOUT_MIN
    return minutes * 60_000


def _new_id(symbol: str, timeframe: str, analysis_id: str | None, now_ms: int) -> str:
    material = f"{symbol}|{timeframe}|{analysis_id}|{now_ms}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def enqueue_approval(
    conn: Any,
    *,
    analysis: dict[str, Any],
    timeframe: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    symbol = str(analysis["symbol"]).upper()
    return upsert_pending_approval(
        conn,
        {
            "id": _new_id(symbol, timeframe, analysis.get("analysis_id"), ts),
            "created_at_ms": ts,
            "expires_at_ms": ts + approval_timeout_ms(),
            "symbol": symbol,
            "timeframe": timeframe,
            "analysis_id": analysis.get("analysis_id"),
            "action": str(analysis.get("action") or "flat"),
            "regime_state": analysis.get("regime_state"),
            "confidence_score": analysis.get("confidence_score"),
            "size_pct_equity": analysis.get("size_pct_equity"),
            "status": "pending",
        },
    )


def approve_approval(
    conn: Any,
    approval_id: str,
    *,
    kill_switch: bool,
    now_ms: int | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    row = get_approval(conn, approval_id)
    if row is None or row["status"] != "pending":
        return {
            "ok": False,
            "approval": row,
            "fills": [],
            "error": "approval not pending",
            "paper_only": True,
        }
    if kill_switch:
        return {
            "ok": False,
            "approval": row,
            "fills": [],
            "error": "kill_switch engaged",
            "paper_only": True,
        }
    candle = load_latest_candle(conn, row["symbol"], row["timeframe"])
    if candle is None:
        updated = resolve_approval(
            conn,
            approval_id,
            status="failed",
            resolve_reason="no stored candles",
            resolved_at_ms=ts,
        )
        return {
            "ok": False,
            "approval": updated,
            "fills": [],
            "error": "no stored candles",
            "paper_only": True,
        }
    analysis = {
        "analysis_id": row.get("analysis_id"),
        "symbol": row["symbol"],
        "action": row["action"],
        "regime_state": row.get("regime_state"),
        "confidence_score": row.get("confidence_score"),
        "size_pct_equity": row.get("size_pct_equity"),
    }
    applied = apply_signal(
        conn,
        analysis=analysis,
        mid_price=float(candle["close"]),
        timeframe=str(row["timeframe"]),
        now_ms=ts,
    )
    order_ids = [f.get("order_id") for f in applied.get("fills") or [] if f.get("order_id")]
    updated = resolve_approval(
        conn,
        approval_id,
        status="approved",
        resolved_at_ms=ts,
        paper_order_ids_json=json.dumps(order_ids) if order_ids else None,
    )
    return {
        "ok": True,
        "approval": updated,
        "fills": applied.get("fills") or [],
        "error": None,
        "paper_only": True,
        "equity": applied.get("equity"),
    }


def reject_approval(
    conn: Any,
    approval_id: str,
    *,
    reason: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    updated = resolve_approval(
        conn,
        approval_id,
        status="rejected",
        resolve_reason=reason,
        resolved_at_ms=ts,
    )
    return {
        "ok": updated is not None,
        "approval": updated,
        "error": None if updated else "approval not pending",
        "paper_only": True,
    }


def expire_approvals(conn: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    n = expire_pending_approvals(conn, now_ms=ts)
    return {"ok": True, "expired": n, "paper_only": True}
```

Fix `upsert_pending_approval` so that when an existing pending row is found for `(symbol, timeframe)`, the `ON CONFLICT` path updates **that** `id` (as in Task 1). New ids in the incoming dict must not create a second pending row.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_paper_approval.py tests/test_approval_db.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when authorized)

```bash
git add src/jarvise_paper/approval.py tests/test_paper_approval.py
git commit -m "feat(paper): enqueue and approve paper fills via approval queue"
```

---

### Task 3: CLI — enqueue default + approve commands

**Files:**
- Modify: `src/jarvise_paper/cli.py`
- Modify: `tests/test_paper_cli.py`

**Interfaces:**
- Consumes: `enqueue_approval`, `approve_approval`, `reject_approval`, `expire_approvals`, `list_approvals`
- Produces: CLI flags/commands per spec table

- [ ] **Step 1: Write / extend failing CLI tests**

Read `tests/test_paper_cli.py` first and reuse its existing DB seed helpers. Add:

```python
from jarvise_ingest.db import list_approvals, list_paper_orders, open_db
from jarvise_paper.cli import main


def test_paper_run_enqueues_without_fill(tmp_path, monkeypatch):
    db = tmp_path / "cli.db"
    # Use the same candle + analysis_output seed pattern already in this file
    # (copy the helper/setup from the nearest existing run test).
    code = main(
        [
            "run",
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "4h",
            "--db",
            str(db),
            "--json",
            "--skip-analyze",
        ]
    )
    assert code == 0
    conn = open_db(db)
    assert list_approvals(conn, status="pending")
    assert list_paper_orders(conn) == []


def test_paper_run_auto_fill_writes_orders(tmp_path, monkeypatch):
    db = tmp_path / "cli2.db"
    # Same seed as above
    code = main(
        [
            "run",
            "--symbol",
            "BTCUSDT",
            "--timeframe",
            "4h",
            "--db",
            str(db),
            "--json",
            "--skip-analyze",
            "--auto-fill",
        ]
    )
    assert code == 0
    conn = open_db(db)
    assert list_paper_orders(conn)
```

- [ ] **Step 2: Run to verify fail / assert current auto-fill behavior breaks default**

Run: `pytest tests/test_paper_cli.py -v`  
Expected: new enqueue-default tests FAIL until CLI changes

- [ ] **Step 3: Wire CLI**

In `build_parser()`:

```python
run_p.add_argument(
    "--auto-fill",
    action="store_true",
    help="Apply paper fills immediately (skip approval queue)",
)
# update run help text: default enqueues for Approve

q = sub.add_parser("queue", help="List approval queue")
q.add_argument("--db", type=Path, default=DEFAULT_DB)
q.add_argument("--all", action="store_true", help="Include recent non-pending")
q.add_argument("--json", action="store_true", dest="as_json")
q.add_argument("--limit", type=int, default=50)

ap = sub.add_parser("approve", help="Approve pending → paper fill")
ap.add_argument("approval_id")
ap.add_argument("--db", type=Path, default=DEFAULT_DB)
ap.add_argument("--json", action="store_true", dest="as_json")

rj = sub.add_parser("reject", help="Reject pending (no fill)")
rj.add_argument("approval_id")
rj.add_argument("--reason", default=None)
rj.add_argument("--db", type=Path, default=DEFAULT_DB)
rj.add_argument("--json", action="store_true", dest="as_json")

ex = sub.add_parser("expire", help="Mark timed-out pendings (no FLAT)")
ex.add_argument("--db", type=Path, default=DEFAULT_DB)
ex.add_argument("--json", action="store_true", dest="as_json")
```

In `cmd_run`, replace the unconditional `apply_signal` block:

```python
if args.auto_fill:
    applied = apply_signal(...)
    ...
else:
    if args.dry_run:
        queued = {"dry_run": True, "would_enqueue": True, "analysis": {...}}
    else:
        queued = enqueue_approval(conn, analysis=analysis, timeframe=args.timeframe)
    results.append({"queued": queued, "analysis": {...}})
```

Update payload `note` to distinguish enqueue vs auto-fill.

Wire `cmd_queue` / `cmd_approve` / `cmd_reject` / `cmd_expire` in `run()`:

- `approve`: if `kill_switch_engaged()` pass `kill_switch=True`; exit 3 when kill-switch blocks; exit 2 when not pending; exit 1 on other failure; exit 0 on ok
- `status`: include `pending_count` from `list_approvals(conn, status="pending")` length

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_paper_cli.py tests/test_paper_approval.py tests/test_paper_engine.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when authorized)

```bash
git add src/jarvise_paper/cli.py tests/test_paper_cli.py
git commit -m "feat(paper): default paper run to approval queue; add approve CLI"
```

---

### Task 4: Web Approval card + POST routes

**Files:**
- Modify: `src/jarvise_web/app.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: `list_approvals`, `approve_approval`, `reject_approval`, `kill_switch` via Redis same as `/kill-switch`
- Produces: HTML card on `/analytics`; `POST /approvals/approve`; `POST /approvals/reject` → redirect `/analytics`

- [ ] **Step 1: Write failing web tests**

```python
def test_analytics_shows_pending_approval(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    # open_db, ensure account, upsert pending via upsert_pending_approval, seed nothing else required for list
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/analytics")
    assert resp.status_code == 200
    assert b"Approval queue" in resp.content
    assert b"BTCUSDT" in resp.content  # from pending row


def test_post_approve_redirects(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    # seed pending + candle so approve can fill
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.post("/approvals/approve", data={"id": approval_id}, follow_redirects=False)
    assert resp.status_code == 303
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_web.py::test_analytics_shows_pending_approval -v`  
Expected: FAIL (no Approval queue card)

- [ ] **Step 3: Implement helpers + HTML + routes in `app.py`**

```python
def _approval_queue_html() -> str:
    path = db_path()
    if not path.exists():
        return (
            '<div class="card"><strong>Approval queue</strong>'
            '<p class="muted">No database.</p></div>'
        )
    try:
        conn = open_db(path)
        try:
            rows = list_approvals(conn, status="pending", limit=20)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return (
            f'<div class="card"><strong>Approval queue</strong>'
            f'<p class="muted">{html.escape(str(exc))}</p></div>'
        )
    if not rows:
        return (
            '<div class="card"><strong>Approval queue</strong>'
            '<p class="muted">No pending approvals</p></div>'
        )
    # table + per-row forms POST /approvals/approve and /approvals/reject
    ...


@app.post("/approvals/approve")
def approvals_approve(id: str = Form(...), _: None = Depends(require_auth)) -> RedirectResponse:
    # kill_switch from redis; open_db; approve_approval(...); redirect /analytics
    ...


@app.post("/approvals/reject")
def approvals_reject(id: str = Form(...), _: None = Depends(require_auth)) -> RedirectResponse:
    ...
```

Insert `{_approval_queue_html()}` on `/analytics` after the paper ledger card and before Pipeline status.

Example row actions (escape all dynamic strings):

```html
<tr>
  <td>BTCUSDT</td><td>4h</td><td>long</td><td>0.7</td><td>5.0</td>
  <td>
    <form method="post" action="/approvals/approve" style="display:inline">
      <input type="hidden" name="id" value="APPROVAL_ID"/>
      <button type="submit">Approve</button>
    </form>
    <form method="post" action="/approvals/reject" style="display:inline">
      <input type="hidden" name="id" value="APPROVAL_ID"/>
      <button type="submit">Reject</button>
    </form>
  </td>
</tr>
```

```python
@app.post("/approvals/approve")
def approvals_approve(
    id: str = Form(...),
    _: None = Depends(require_auth),
) -> RedirectResponse:
    engaged = False
    try:
        engaged = (_redis().get(KILL_SWITCH_KEY) or "0") in {"1", "true", "on", "yes"}
    except Exception:
        engaged = False
    conn = open_db(db_path())
    try:
        approve_approval(conn, id, kill_switch=engaged)
    finally:
        conn.close()
    return RedirectResponse("/analytics", status_code=303)


@app.post("/approvals/reject")
def approvals_reject(
    id: str = Form(...),
    _: None = Depends(require_auth),
) -> RedirectResponse:
    conn = open_db(db_path())
    try:
        reject_approval(conn, id, reason="ui")
    finally:
        conn.close()
    return RedirectResponse("/analytics", status_code=303)
```

Copy must say paper / simulated only — never “live order”.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_web.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when authorized)

```bash
git add src/jarvise_web/app.py tests/test_web.py
git commit -m "feat(web): approval queue card and approve/reject on analytics"
```

---

### Task 5: Docs + env example + regression sweep

**Files:**
- Modify: `.env.example`
- Modify: `docs/product-usage.md`
- Modify: `docs/superpowers/specs/2026-09-23-p4-manual-approval-paper-slice-design.md` (Plan link + Status = plan ready / implementing)
- Modify: `docs/superpowers/specs/2026-09-23-product-roadmap-design.md` (optional one-line pointer under P4)

- [ ] **Step 1: Add env example**

```bash
# Paper approval queue TTL (minutes); timeout marks timed_out only (no FLAT)
JARVISE_APPROVAL_TIMEOUT_MIN=60
```

- [ ] **Step 2: Update product-usage**

Replace / extend paper section:

```markdown
### Paper + approval (P2/P4 paper slice)

`.venv/bin/jarvise paper run --symbol BTCUSDT --timeframe 4h --json`
→ enqueues `approval_queue` (default). Approve on `/analytics` or:

`.venv/bin/jarvise paper approve <id> --json`

Immediate fill escape hatch:

`.venv/bin/jarvise paper run --symbol BTCUSDT --timeframe 4h --auto-fill --json`

Expire timed-out pendings (no position change):

`.venv/bin/jarvise paper expire --json`
```

- [ ] **Step 3: Point design Status/Plan at this file**

Set design header:

```markdown
**Status:** Implementation plan ready — [../plans/2026-09-23-p4-manual-approval-paper-slice.md](../plans/2026-09-23-p4-manual-approval-paper-slice.md)
**Plan:** [../plans/2026-09-23-p4-manual-approval-paper-slice.md](../plans/2026-09-23-p4-manual-approval-paper-slice.md)
```

- [ ] **Step 4: Full regression**

Run: `pytest tests/test_approval_db.py tests/test_paper_approval.py tests/test_paper_cli.py tests/test_paper_engine.py tests/test_web.py tests/test_exchange_web.py -v`  
Expected: PASS  
Sanity: `rg -n "order|trade|withdraw" src/jarvise_exchange -i` still shows account read-only only (no new trade endpoints).

- [ ] **Step 5: Commit** (when authorized)

```bash
git add .env.example docs/product-usage.md docs/superpowers/specs/2026-09-23-p4-manual-approval-paper-slice-design.md
git commit -m "docs: document paper approval queue usage"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| `approval_queue` DDL + helpers | Task 1 |
| enqueue / approve / reject / expire core | Task 2 |
| `paper run` default enqueue + `--auto-fill` | Task 3 |
| CLI `queue` / `approve` / `reject` / `expire` | Task 3 |
| Dedup pending per symbol+TF | Task 1–2 |
| Kill-switch blocks enqueue + Approve | Task 3 (enqueue) + Task 2/4 (approve) |
| Timeout → `timed_out` only | Task 2 |
| Approve uses closed-candle mid | Task 2 |
| `/analytics` Approval card + POST | Task 4 |
| Docs / env TTL | Task 5 |
| No exchange trade POSTs | Global + Task 5 sanity |

## Out of scope (do not implement in this plan)

- Live Binance order on Approve (P4-C)
- Timeout → FLAT
- n8n job for `paper expire`
- Autonomy flag / P5
