# Paper auto-decide + recommendation card — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** [`docs/superpowers/specs/2026-10-02-paper-auto-decide-design.md`](../specs/2026-10-02-paper-auto-decide-design.md)

**Goal:** Every pending paper candidate gets a Thai recommendation card; a flag-gated job lets Claude (via OpenRouter, inside `jobs`) approve / reject / defer candidates right after `paper-run`; a thin ingest-health job alerts when `ema_200` warm-up is missing.

**Architecture:** Three independent slices, shipped in order. (A) `recommendation.py` builds a deterministic card from SQLite rows; web serves it at `GET /api/approvals/{id}/recommendation` and the SPA renders it per row. (B) `health.py` + `POST /jobs/ingest-health` publish `jarvise:ingest:health` and Telegram-alert on missing warm-up / stale / gaps. (C) `auto_decide.py` filters pending rows, briefs Claude through `llm_openrouter.chat_json`, and applies decisions only through the existing `approve_approval` / `reject_approval`, so every existing guard (kill-switch, market safety, expiry, risk caps) is inherited.

**Tech Stack:** Python 3.12, sqlite3, httpx, FastAPI (web), `http.server` jobs API, pytest; Vite + React 19 + TypeScript + shadcn-style components in `web/`; n8n workflow JSON; docker compose.

## Global Constraints

- Paper-only. The auto path never submits exchange orders; the job refuses when `JARVISE_LIVE_TRADING=true` (`reason: "live_trading_enabled"`).
- `JARVISE_PAPER_AUTO_DECIDE` default `false`; off → job returns `{"ok": true, "skipped": true, "reason": "auto_decide_disabled"}` and nothing changes.
- Env names, exactly: `JARVISE_PAPER_AUTO_DECIDE`, `JARVISE_AUTO_DECIDE_MODEL` (default `anthropic/claude-sonnet-4.5`), `JARVISE_AUTO_DECIDE_MIN_CONF` (`0.55`), `JARVISE_AUTO_DECIDE_MAX_PER_RUN` (`4`), `JARVISE_AUTO_DECIDE_TIMEOUT_S` (`30`), `OPENROUTER_API_KEY` (reused), `JARVISE_INGEST_HEALTH_MAX_AGE_MIN` (`60`).
- Redis keys: `jarvise:paper_auto:last`, `jarvise:ingest:health`.
- `resolve_reason` prefixes: `auto:claude:approve`, `auto:claude:reject:<reason>`, `auto:apply_failed:<inner error>`. Manual decisions stay unprefixed.
- Template rule: `conf >= 0.70` → `approve`; `0.55 <= conf < 0.70` → `approve_with_caution`; `conf < 0.55` or `action == flat` → `reject`. `confidence_label`: `>= 0.70` "ปานกลาง-สูง", `0.55–0.69` "ปานกลาง", `< 0.55` "ต่ำ".
- `risk.est_loss_usd = notional_usd * |close - invalidation_price| / close`; omitted when no invalidation price or close.
- Card copy is Thai; JSON keys are English.
- Claude output contract: exactly `{"decision": "approve" | "reject" | "defer", "reason": "<= 280 chars"}`; anything else → `defer` with reason `auto:claude:unparseable`.
- Fail-closed: OpenRouter timeout / non-2xx / bad JSON / missing key → `defer`; `approve_approval` `ok=false` → `apply_failed`, no retry.
- Do not change the n8n 4h paper cadence or scheduled ingest `--limit 200`.
- Imports at the top of the module. The only inline imports allowed are the existing lazy-optional-dependency pattern (`redis`, `qdrant_client`, `sentence_transformers`) with a comment saying why.
- Every new Python module starts with a docstring that says paper-only / no order placement, matching the codebase.
- Commit after each task; run `python -m pytest -q` before every commit. Frontend tasks also run `cd web && npm run build`.

## Spec amendments (clarifications made while planning)

1. **Doctrine lookup path.** The web image has `qdrant-client` but not `sentence-transformers`, so web cannot embed queries. The `jobs` service (worker image, has both) exposes a GET-only `GET /doctrine?q=&limit=` route; web calls it over the docker network (`JARVISE_JOBS_URL`, default `http://jobs:8090`, 5 s timeout) and falls back to `doctrine: []` on any error. `auto_decide` calls the same helper in-process. Spec §4/§5 said web needs no new env; it now needs `JARVISE_JOBS_URL` (+ `JARVISE_JOBS_TOKEN` when set).
2. **Ingest-health age.** Health runs on the paper timeframe (`JARVISE_PAPER_TIMEFRAME`, default `4h`) because that is where `ema_200` readiness decides the queue. `newest_age_min` is minutes since the newest stored candle **closed**; the alert threshold is one candle interval plus `JARVISE_INGEST_HEALTH_MAX_AGE_MIN`, because that age legitimately cycles up to one interval on a healthy series.

## File map

| Path | Responsibility | Task |
|------|----------------|------|
| `src/jarvise_ingest/db.py` | `approval_llm_reviews` table, `insert_llm_review`, `get_latest_llm_review`, `set_approval_resolve_reason`, `get_analysis_output`, `count_indicator_ready` | 1, 7 |
| `src/jarvise_paper/recommendation.py` | Deterministic Thai card builder | 2 |
| `src/jarvise/rag.py` | `doctrine_snippets` (lazy model, fail-soft) | 3 |
| `src/jarvise/jobs.py` | `GET /doctrine`, `POST /jobs/ingest-health`, `POST /jobs/paper-auto-decide` | 3, 8, 13 |
| `src/jarvise_web/app.py` | recommendation endpoint, `fetch_doctrine`, status keys | 4 |
| `web/src/lib/api.ts` | types + `api.recommendation` | 5, 9, 14 |
| `web/src/components/RecommendationCard.tsx` | card UI | 5 |
| `web/src/components/ApprovalQueue.tsx` | expandable row, labelled buttons | 5 |
| `web/src/pages/OpsPage.tsx` | ingest-health + paper-auto cards | 9, 14 |
| `src/jarvise_ingest/health.py` | `ingest_health` | 7 |
| `src/jarvise_notify/telegram.py`, `__init__.py` | health + auto-decide messages | 8, 13 |
| `src/jarvise_paper/llm_openrouter.py` | `chat_json` | 10 |
| `src/jarvise_paper/auto_decide.py` | config, filter, brief, prompt, parse, run | 11, 12 |
| `infra/n8n/workflows/jarvise-ingest-health.json` | hourly cron | 8 |
| `infra/n8n/workflows/jarvise-paper-run.json` | chain auto-decide after paper-run | 13 |
| `.env.example`, `docker-compose.yml` | env wiring | 4, 8, 13 |
| `docs/product-usage.md` | owner flow | 14 |

---

# Phase A — Recommendation card

### Task 1: SQLite — LLM review table + approval helpers

**Files:**
- Modify: `src/jarvise_ingest/db.py` (append to `SCHEMA_SQL` before the closing `"""`; add functions after `list_expired_pending_approvals`)
- Test: `tests/test_llm_reviews_db.py`

**Interfaces:**
- Produces: `insert_llm_review(conn, row: dict) -> dict`, `get_latest_llm_review(conn, approval_id: str) -> dict | None`, `set_approval_resolve_reason(conn, approval_id: str, reason: str) -> dict | None`, `get_analysis_output(conn, analysis_id: str) -> dict | None`. Review row keys: `approval_id, model, decision, reason, brief_hash, created_at_ms`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_reviews_db.py
from pathlib import Path

from jarvise_ingest.db import (
    get_analysis_output,
    get_approval,
    get_latest_llm_review,
    insert_llm_review,
    open_db,
    set_approval_resolve_reason,
    upsert_analysis_output,
    upsert_pending_approval,
)


def _approval(conn, approval_id: str = "a1") -> dict:
    return upsert_pending_approval(
        conn,
        {
            "id": approval_id,
            "created_at_ms": 1_000,
            "expires_at_ms": 9_999_999_999_999,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "an-1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "size_pct_equity": 1.125,
            "status": "pending",
        },
    )


def test_llm_review_roundtrip_latest_wins(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r.db")
    _approval(conn)
    assert get_latest_llm_review(conn, "a1") is None
    insert_llm_review(
        conn,
        {
            "approval_id": "a1",
            "model": "anthropic/claude-sonnet-4.5",
            "decision": "defer",
            "reason": "first",
            "brief_hash": "h1",
            "created_at_ms": 2_000,
        },
    )
    latest = insert_llm_review(
        conn,
        {
            "approval_id": "a1",
            "model": "anthropic/claude-sonnet-4.5",
            "decision": "approve",
            "reason": "second",
            "brief_hash": "h2",
            "created_at_ms": 3_000,
        },
    )
    assert latest["decision"] == "approve"
    got = get_latest_llm_review(conn, "a1")
    assert got is not None
    assert got["reason"] == "second"
    assert got["created_at_ms"] == 3_000


def test_set_resolve_reason_only_touches_reason(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r2.db")
    _approval(conn)
    row = set_approval_resolve_reason(conn, "a1", "auto:claude:approve")
    assert row is not None
    assert row["resolve_reason"] == "auto:claude:approve"
    assert row["status"] == "pending"
    assert get_approval(conn, "a1")["resolve_reason"] == "auto:claude:approve"
    assert set_approval_resolve_reason(conn, "missing", "x") is None


def test_get_analysis_output_by_id(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r3.db")
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "an-1",
            "timestamp": 1_700_000_000_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "action": "long",
            "invalidation_price": 84159.85,
            "size_pct_equity": 1.125,
            "thesis": "trend_up: price above EMA20 and EMA200",
        },
    )
    row = get_analysis_output(conn, "an-1")
    assert row is not None
    assert row["invalidation_price"] == 84159.85
    assert get_analysis_output(conn, "nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_llm_reviews_db.py -q`
Expected: FAIL with `ImportError: cannot import name 'get_analysis_output'`

- [ ] **Step 3: Add the table to `SCHEMA_SQL`**

In `src/jarvise_ingest/db.py`, immediately after the `idx_live_orders_created` index and before the closing `"""` of `SCHEMA_SQL`, add:

```sql
-- Second-layer LLM reviews for paper approvals (auto-decide audit). Paper only.
CREATE TABLE IF NOT EXISTS approval_llm_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id TEXT NOT NULL,
    model TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    brief_hash TEXT,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_llm_reviews_approval
    ON approval_llm_reviews (approval_id, created_at_ms);
```

`migrate()` runs `SCHEMA_SQL` with `CREATE TABLE IF NOT EXISTS`, so no migration function is needed.

- [ ] **Step 4: Add the helpers**

Append after `list_expired_pending_approvals` (before `_LIVE_ORDER_COLUMNS`):

```python
def set_approval_resolve_reason(
    conn: sqlite3.Connection, approval_id: str, reason: str
) -> dict | None:
    """Overwrite resolve_reason only (status untouched). Used for auto: audit prefixes."""
    conn.execute(
        "UPDATE approval_queue SET resolve_reason = ? WHERE id = ?",
        (reason, approval_id),
    )
    conn.commit()
    return get_approval(conn, approval_id)


def get_analysis_output(conn: sqlite3.Connection, analysis_id: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT analysis_id, timestamp, symbol, timeframe, regime_state,
               confidence_score, action, invalidation_price, size_pct_equity, thesis
        FROM analysis_output
        WHERE analysis_id = ?
        """,
        (analysis_id,),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


_LLM_REVIEW_COLUMNS = "approval_id, model, decision, reason, brief_hash, created_at_ms"


def insert_llm_review(conn: sqlite3.Connection, row: dict) -> dict:
    """Append one LLM decision for an approval; returns the stored row."""
    conn.execute(
        f"""
        INSERT INTO approval_llm_reviews ({_LLM_REVIEW_COLUMNS})
        VALUES (:approval_id, :model, :decision, :reason, :brief_hash, :created_at_ms)
        """,
        {
            "approval_id": str(row["approval_id"]),
            "model": str(row["model"]),
            "decision": str(row["decision"]),
            "reason": row.get("reason"),
            "brief_hash": row.get("brief_hash"),
            "created_at_ms": int(row["created_at_ms"]),
        },
    )
    conn.commit()
    latest = get_latest_llm_review(conn, str(row["approval_id"]))
    if latest is None:
        raise RuntimeError("approval_llm_reviews insert did not persist")
    return latest


def get_latest_llm_review(conn: sqlite3.Connection, approval_id: str) -> dict | None:
    cur = conn.execute(
        f"""
        SELECT {_LLM_REVIEW_COLUMNS}
        FROM approval_llm_reviews
        WHERE approval_id = ?
        ORDER BY created_at_ms DESC, id DESC
        LIMIT 1
        """,
        (approval_id,),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_llm_reviews_db.py tests/test_approval_db.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/jarvise_ingest/db.py tests/test_llm_reviews_db.py
git commit -m "feat(db): approval_llm_reviews table + resolve_reason/analysis helpers for auto-decide"
```

---

### Task 2: `recommendation.py` — deterministic Thai card

**Files:**
- Create: `src/jarvise_paper/recommendation.py`
- Test: `tests/test_recommendation.py`

**Interfaces:**
- Consumes: `ATR_STOP_MULT` from `jarvise_analyze.engine`.
- Produces: `APPROVE_CONF = 0.70`, `CAUTION_CONF = 0.55`, `confidence_label(conf: float | None) -> str`, `template_recommendation(action: str | None, conf: float | None) -> str`, `doctrine_query(row: Mapping[str, Any]) -> str`, `build_recommendation(*, approval, candle, analysis, account, position, safety, doctrine: list[str], claude: Mapping | None) -> dict[str, Any]` returning the §6 card.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_recommendation.py
from jarvise_paper.recommendation import (
    build_recommendation,
    confidence_label,
    doctrine_query,
    template_recommendation,
)

APPROVAL = {
    "id": "261b5544d1bfe916",
    "symbol": "BTCUSDT",
    "timeframe": "4h",
    "action": "long",
    "regime_state": "trend_up",
    "confidence_score": 0.75,
    "size_pct_equity": 1.125,
    "expires_at_ms": 9_999_999_999_999,
    "status": "pending",
}
CANDLE = {
    "close": 85000.0,
    "ema_20": 84000.0,
    "ema_200": 80000.0,
    "rsi_14": 65.0,
    "atr_14": 1000.0,
}
ANALYSIS = {"invalidation_price": 83500.0, "thesis": "trend_up"}
ACCOUNT = {"equity": 10000.0, "cash": 10000.0, "starting_equity": 10000.0}
SAFETY_OK = {"ok": True, "force_flat": False, "critical": False, "reasons": []}


def test_template_thresholds() -> None:
    assert template_recommendation("long", 0.75) == "approve"
    assert template_recommendation("long", 0.60) == "approve_with_caution"
    assert template_recommendation("long", 0.40) == "reject"
    assert template_recommendation("flat", 0.90) == "reject"
    assert template_recommendation(None, None) == "reject"


def test_confidence_labels() -> None:
    assert confidence_label(0.75) == "ปานกลาง-สูง"
    assert confidence_label(0.60) == "ปานกลาง"
    assert confidence_label(0.40) == "ต่ำ"
    assert confidence_label(None) == "ต่ำ"


def test_doctrine_query_uses_regime_and_action() -> None:
    assert doctrine_query(APPROVAL) == "trend_up long entry risk stop"
    assert doctrine_query({}) == "range flat entry risk stop"


def test_card_approve_with_risk_math() -> None:
    card = build_recommendation(
        approval=APPROVAL,
        candle=CANDLE,
        analysis=ANALYSIS,
        account=ACCOUNT,
        position=None,
        safety=SAFETY_OK,
        doctrine=["structure first, oscillators as context"],
        claude=None,
    )
    assert card["ok"] is True
    assert card["approval_id"] == "261b5544d1bfe916"
    assert card["recommendation"] == "approve"
    assert card["recommendation_source"] == "template"
    assert card["confidence_label"] == "ปานกลาง-สูง"
    assert card["headline"] == "แนะนำ: APPROVE (ความมั่นใจ 0.75)"
    assert card["risk"]["notional_usd"] == 112.5
    assert card["risk"]["invalidation_price"] == 83500.0
    # 112.5 * |85000-83500| / 85000 = 1.985...
    assert round(card["risk"]["est_loss_usd"], 2) == 1.99
    assert card["risk"]["stop_atr_multiple"] == 1.5
    assert any("ขาขึ้น" in line for line in card["what_happened"])
    assert any("RSI 65" in line for line in card["what_happened"])
    assert any("ไม่พบสัญญาณอันตราย" in line for line in card["what_happened"])
    assert card["doctrine"] == ["structure first, oscillators as context"]
    assert any("ยังไม่ถือตำแหน่งเดียวกันซ้ำ" in line for line in card["checklist"])
    assert card["claude"] is None


def test_card_omits_est_loss_without_invalidation() -> None:
    card = build_recommendation(
        approval=APPROVAL,
        candle=CANDLE,
        analysis=None,
        account=ACCOUNT,
        position=None,
        safety=SAFETY_OK,
        doctrine=[],
        claude=None,
    )
    assert "est_loss_usd" not in card["risk"]
    assert card["risk"]["invalidation_price"] is None
    assert card["doctrine"] == []


def test_card_flat_explains_reject_and_missing_ema200() -> None:
    flat = {**APPROVAL, "action": "flat", "confidence_score": 0.1, "size_pct_equity": 0.0}
    card = build_recommendation(
        approval=flat,
        candle={**CANDLE, "ema_200": None},
        analysis=None,
        account=ACCOUNT,
        position=None,
        safety=SAFETY_OK,
        doctrine=[],
        claude=None,
    )
    assert card["recommendation"] == "reject"
    assert "flat" in card["headline"]
    assert any("EMA200" in line for line in card["what_happened"])


def test_card_claude_source_and_open_position_and_safety_reasons() -> None:
    card = build_recommendation(
        approval=APPROVAL,
        candle=CANDLE,
        analysis=ANALYSIS,
        account=ACCOUNT,
        position={"symbol": "BTCUSDT", "side": "long", "qty": 0.001},
        safety={"ok": False, "force_flat": False, "critical": False, "reasons": ["book_stale_ms=1"]},
        doctrine=[],
        claude={
            "decision": "defer",
            "reason": "มีตำแหน่งเดิมอยู่",
            "model": "anthropic/claude-sonnet-4.5",
            "at_ms": 5,
        },
    )
    assert card["recommendation_source"] == "claude"
    assert card["claude"]["decision"] == "defer"
    assert any("ถือ BTCUSDT อยู่แล้ว" in line for line in card["checklist"])
    assert any("book_stale_ms=1" in line for line in card["what_happened"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_recommendation.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvise_paper.recommendation'`

- [ ] **Step 3: Write the module**

```python
# src/jarvise_paper/recommendation.py
"""Owner-facing recommendation card for a paper approval.

Deterministic Thai copy built from stored rows only — no LLM, no network. Paper only;
nothing here places orders. JSON keys stay English for the SPA.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jarvise_analyze.engine import ATR_STOP_MULT

APPROVE_CONF = 0.70
CAUTION_CONF = 0.55
HIGH_VOL_PCT = 3.0


def confidence_label(conf: float | None) -> str:
    c = float(conf or 0.0)
    if c >= APPROVE_CONF:
        return "ปานกลาง-สูง"
    if c >= CAUTION_CONF:
        return "ปานกลาง"
    return "ต่ำ"


def template_recommendation(action: str | None, conf: float | None) -> str:
    act = (action or "flat").lower()
    c = float(conf or 0.0)
    if act == "flat" or c < CAUTION_CONF:
        return "reject"
    if c >= APPROVE_CONF:
        return "approve"
    return "approve_with_caution"


def doctrine_query(row: Mapping[str, Any]) -> str:
    regime = row.get("regime_state") or "range"
    action = row.get("action") or "flat"
    return f"{regime} {action} entry risk stop"


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _headline(rec: str, action: str, conf: float) -> str:
    if action == "flat":
        return (
            "แนะนำ: REJECT — สัญญาณ flat "
            "(approve ไม่เปิดตำแหน่งใหม่; ถ้ามีตำแหน่งเปิดอยู่จะถูกปิด)"
        )
    if rec == "approve":
        return f"แนะนำ: APPROVE (ความมั่นใจ {conf:.2f})"
    if rec == "approve_with_caution":
        return f"แนะนำ: APPROVE แบบระวัง (ความมั่นใจ {conf:.2f} — ขนาดเล็ก, ดู stop ให้ดี)"
    return f"แนะนำ: REJECT (ความมั่นใจ {conf:.2f} ต่ำกว่าเกณฑ์ {CAUTION_CONF:.2f})"


def _what_happened(candle: Mapping[str, Any] | None, safety: Mapping[str, Any] | None) -> list[str]:
    lines: list[str] = []
    c = candle or {}
    close, ema20, ema200 = _f(c.get("close")), _f(c.get("ema_20")), _f(c.get("ema_200"))
    if close is None or ema20 is None or ema200 is None:
        lines.append(
            "ตัวชี้วัดระยะยาว (EMA200) ยังไม่พร้อม — ระบบจะไม่เสนอเทรดจนกว่าข้อมูลครบ"
        )
    elif close > ema20 > ema200:
        lines.append("ราคาอยู่เหนือเส้นค่าเฉลี่ยทั้งระยะสั้นและระยะยาว = แนวโน้มขาขึ้น")
    elif close < ema20 < ema200:
        lines.append("ราคาอยู่ต่ำกว่าเส้นค่าเฉลี่ยทั้งระยะสั้นและระยะยาว = แนวโน้มขาลง")
    else:
        lines.append("ราคาอยู่ระหว่างเส้นค่าเฉลี่ย = ยังไม่มีแนวโน้มชัด (ออกข้าง)")

    rsi = _f(c.get("rsi_14"))
    if rsi is not None:
        if rsi >= 70:
            lines.append(f"โมเมนตัม RSI {rsi:.0f} ร้อนแรง (ซื้อมากเกิน) — ระวังการย่อตัว")
        elif rsi <= 30:
            lines.append(f"โมเมนตัม RSI {rsi:.0f} เย็นจัด (ขายมากเกิน) — อาจเด้งกลับ")
        else:
            lines.append(f"โมเมนตัม RSI {rsi:.0f} ยังไม่ร้อนเกินไป")

    atr = _f(c.get("atr_14"))
    if atr is not None and close:
        pct = atr / close * 100.0
        if pct > HIGH_VOL_PCT:
            lines.append(f"ความผันผวนสูง ({pct:.1f}% ต่อแท่ง) — ขนาดตำแหน่งถูกลดลงอัตโนมัติ")
        else:
            lines.append(f"ความผันผวนปกติ ({pct:.1f}% ต่อแท่ง)")

    reasons = list((safety or {}).get("reasons") or [])
    if reasons:
        lines.append("market safety พบ: " + "; ".join(str(r) for r in reasons))
    else:
        lines.append("market safety ไม่พบสัญญาณอันตราย")
    return lines


def _risk(
    approval: Mapping[str, Any],
    candle: Mapping[str, Any] | None,
    analysis: Mapping[str, Any] | None,
    account: Mapping[str, Any],
) -> dict[str, Any]:
    equity = float(account.get("equity") or 0.0)
    size = _f(approval.get("size_pct_equity"))
    notional = round(equity * (size or 0.0) / 100.0, 2)
    invalidation = _f((analysis or {}).get("invalidation_price"))
    close = _f((candle or {}).get("close"))
    risk: dict[str, Any] = {
        "size_pct_equity": size,
        "notional_usd": notional,
        "equity_usd": equity,
        "invalidation_price": invalidation,
        "stop_atr_multiple": ATR_STOP_MULT,
    }
    if invalidation is not None and close:
        risk["est_loss_usd"] = round(notional * abs(close - invalidation) / close, 2)
    return risk


def _checklist(
    approval: Mapping[str, Any], position: Mapping[str, Any] | None
) -> list[str]:
    symbol = str(approval.get("symbol") or "")
    timeframe = str(approval.get("timeframe") or "")
    lines = [f"มีข่าวใหญ่ใน {timeframe} ข้างหน้าหรือไม่ (ระบบไม่เห็นข่าว)"]
    if position:
        lines.append(
            f"พอร์ตถือ {symbol} อยู่แล้ว ({position.get('side')} {position.get('qty')}) "
            "— approve จะเพิ่มหรือกลับทิศตำแหน่งเดิม"
        )
    else:
        lines.append("พอร์ตยังไม่ถือตำแหน่งเดียวกันซ้ำ")
    lines.append("คำสั่งนี้เป็น paper เท่านั้น — ไม่มีเงินจริงถูกส่งไปตลาด")
    return lines


def build_recommendation(
    *,
    approval: Mapping[str, Any],
    candle: Mapping[str, Any] | None,
    analysis: Mapping[str, Any] | None,
    account: Mapping[str, Any],
    position: Mapping[str, Any] | None,
    safety: Mapping[str, Any] | None,
    doctrine: list[str],
    claude: Mapping[str, Any] | None,
) -> dict[str, Any]:
    action = str(approval.get("action") or "flat").lower()
    conf = float(approval.get("confidence_score") or 0.0)
    rec = template_recommendation(action, conf)
    return {
        "ok": True,
        "approval_id": approval.get("id"),
        "symbol": approval.get("symbol"),
        "timeframe": approval.get("timeframe"),
        "action": action,
        "status": approval.get("status"),
        "recommendation": rec,
        "recommendation_source": "claude" if claude else "template",
        "confidence_label": confidence_label(conf),
        "headline": _headline(rec, action, conf),
        "what_happened": _what_happened(candle, safety),
        "risk": _risk(approval, candle, analysis, account),
        "doctrine": list(doctrine),
        "checklist": _checklist(approval, position),
        "thesis": (analysis or {}).get("thesis"),
        "claude": dict(claude) if claude else None,
        "paper_only": True,
    }
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_recommendation.py -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/jarvise_paper/recommendation.py tests/test_recommendation.py
git commit -m "feat(paper): deterministic Thai recommendation card builder"
```

---

### Task 3: Doctrine snippets helper + jobs `GET /doctrine`

**Files:**
- Modify: `src/jarvise/rag.py` (after `index_sources`, before `publish_redis_status`)
- Modify: `src/jarvise/jobs.py` (imports, new `run_doctrine_search`, `ROUTES`, `_dispatch`)
- Test: `tests/test_doctrine_snippets.py`, `tests/test_jobs.py` (append)

**Interfaces:**
- Produces: `doctrine_snippets(query: str, *, limit: int = 3, client: Any | None = None, encoder: Callable[[str], list[float]] | None = None) -> list[dict[str, Any]]` — each hit `{"text": str, "source": str | None, "score": float}`; `[]` on empty query, missing deps, or any Qdrant error. `run_doctrine_search(query: str, limit: int) -> dict[str, Any]` and route `GET /doctrine?q=&limit=` → `{"ok": true, "query", "hits", "paper_only": true}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_doctrine_snippets.py
from types import SimpleNamespace

from jarvise.rag import doctrine_snippets


class _FakeClient:
    def __init__(self, points):
        self.points = points
        self.calls = []

    def query_points(self, *, collection_name, query, limit):
        self.calls.append((collection_name, limit))
        return SimpleNamespace(points=self.points)


def test_empty_query_returns_empty() -> None:
    assert doctrine_snippets("   ") == []


def test_missing_encoder_deps_fail_soft(monkeypatch) -> None:
    def boom(_text):
        raise ImportError("no sentence_transformers")

    monkeypatch.setattr("jarvise.rag._encode_query", boom)
    assert doctrine_snippets("trend_up long") == []


def test_hits_mapped_and_truncated() -> None:
    points = [
        SimpleNamespace(score=0.9, payload={"text": "x" * 500, "source": "a.md"}),
        SimpleNamespace(score=0.5, payload={"text": "stops at least 1.5x ATR", "source": None}),
    ]
    client = _FakeClient(points)
    hits = doctrine_snippets(
        "trend_up long", limit=2, client=client, encoder=lambda _t: [0.1, 0.2]
    )
    assert client.calls == [("jarvise_doctrine", 2)]
    assert len(hits) == 2
    assert len(hits[0]["text"]) == 240
    assert hits[1] == {"text": "stops at least 1.5x ATR", "source": None, "score": 0.5}


def test_client_error_fail_soft() -> None:
    class Broken:
        def query_points(self, **kwargs):
            raise RuntimeError("qdrant down")

    assert doctrine_snippets("q", client=Broken(), encoder=lambda _t: [0.0]) == []
```

Append to `tests/test_jobs.py`:

```python
def test_doctrine_route_passes_query(monkeypatch) -> None:
    seen: list[tuple[str, int]] = []

    def fake_search(query: str, limit: int):
        seen.append((query, limit))
        return {"ok": True, "query": query, "hits": [], "paper_only": True}

    monkeypatch.setattr("jarvise.jobs.run_doctrine_search", fake_search)
    handler = _Handler()
    handler.command = "GET"
    handler.path = "/doctrine?q=trend_up%20long&limit=2"
    handler._dispatch()
    assert handler._status == 200
    assert seen == [("trend_up long", 2)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_doctrine_snippets.py tests/test_jobs.py -q`
Expected: FAIL with `ImportError: cannot import name 'doctrine_snippets'`

- [ ] **Step 3: Add `doctrine_snippets` to `rag.py`**

Add `from collections.abc import Callable` to the imports at the top of `src/jarvise/rag.py`, then insert after `index_sources`:

```python
_QUERY_MODEL: Any = None


def _encode_query(text: str) -> list[float]:
    """Embed one query with the same model used by index_sources. Cached per process."""
    global _QUERY_MODEL
    if _QUERY_MODEL is None:
        # Heavy optional extra (.[rag]); imported lazily so web/CLI without it stay importable.
        from sentence_transformers import SentenceTransformer

        _QUERY_MODEL = SentenceTransformer(MODEL_NAME)
    return _QUERY_MODEL.encode(text).tolist()


def doctrine_snippets(
    query: str,
    *,
    limit: int = 3,
    client: Any | None = None,
    encoder: Callable[[str], list[float]] | None = None,
) -> list[dict[str, Any]]:
    """Top-k doctrine chunks for a query. [] on empty query, missing deps, or Qdrant error."""
    text = (query or "").strip()
    if not text:
        return []
    k = max(1, min(int(limit), 10))
    try:
        vector = (encoder or _encode_query)(text)
        if client is None:
            # Optional extra; lazy import keeps the module importable without qdrant_client.
            from qdrant_client import QdrantClient

            client = QdrantClient(
                url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
                timeout=10,
                check_compatibility=False,
            )
        hits = client.query_points(collection_name=COLLECTION, query=vector, limit=k)
    except Exception:  # noqa: BLE001 — fail-soft by design; card and brief render without doctrine
        return []
    out: list[dict[str, Any]] = []
    for hit in getattr(hits, "points", []) or []:
        payload = getattr(hit, "payload", None) or {}
        out.append(
            {
                "text": str(payload.get("text") or "")[:240],
                "source": payload.get("source"),
                "score": float(getattr(hit, "score", 0.0) or 0.0),
            }
        )
    return out
```

- [ ] **Step 4: Add the jobs route**

In `src/jarvise/jobs.py`:

1. Change the import line `from jarvise.rag import publish_redis_status` to `from jarvise.rag import doctrine_snippets, publish_redis_status` and add `from urllib.parse import parse_qs` to the stdlib imports.
2. Add after `run_paper_pending_digest`:

```python
def run_doctrine_search(query: str, limit: int) -> dict[str, Any]:
    """GET-only doctrine lookup for web cards and the auto-decide brief."""
    hits = doctrine_snippets(query, limit=limit)
    return {"ok": True, "query": query, "hits": hits, "paper_only": True}
```

3. Add to `ROUTES`: `("GET", "/doctrine"): "doctrine",`
4. In `_dispatch`, after the `health` branch:

```python
        if action == "doctrine":
            params = parse_qs(self.path.partition("?")[2])
            query = (params.get("q") or [""])[0]
            try:
                limit = int((params.get("limit") or ["3"])[0])
            except ValueError:
                limit = 3
            self._send(200, run_doctrine_search(query, limit))
            return
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_doctrine_snippets.py tests/test_jobs.py tests/test_rag.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/jarvise/rag.py src/jarvise/jobs.py tests/test_doctrine_snippets.py tests/test_jobs.py
git commit -m "feat(rag): fail-soft doctrine_snippets + GET /doctrine on jobs"
```

---

### Task 4: Web — `GET /api/approvals/{id}/recommendation` + status keys + compose env

**Files:**
- Modify: `src/jarvise_web/app.py`
- Modify: `docker-compose.yml` (web service environment), `.env.example`
- Test: `tests/test_web_recommendation.py`

**Interfaces:**
- Consumes: `build_recommendation`, `doctrine_query` (Task 2); `get_approval`, `get_analysis_output`, `get_latest_llm_review`, `get_paper_position`, `load_latest_candle`, `ensure_paper_account` (db); `evaluate_from_db` (`jarvise_risk`).
- Produces: `JOBS_URL`, `PAPER_AUTO_KEY = "jarvise:paper_auto:last"`, `INGEST_HEALTH_KEY = "jarvise:ingest:health"`, `fetch_doctrine(query: str, *, limit: int = 3) -> list[str]`, `load_recommendation(approval_id: str) -> dict | None`, route `GET /api/approvals/{approval_id}/recommendation` (200 card / 404), `status_payload()` gains `paper_auto` and `ingest_health`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_recommendation.py
from pathlib import Path

from fastapi.testclient import TestClient

from jarvise_ingest.db import (
    ensure_paper_account,
    insert_llm_review,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
    upsert_pending_approval,
    write_indicators,
)
from jarvise_web.app import app


def _stub(monkeypatch, *, doctrine=None) -> None:
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)
    monkeypatch.setattr("jarvise_web.app.redis_get", lambda key: "0")
    monkeypatch.setattr("jarvise_web.app.redis_get_json", lambda key: {"ok": True, "key": key})
    monkeypatch.setattr("jarvise_web.app.qdrant_info", lambda: {"exists": True, "points": 0})
    monkeypatch.setattr(
        "jarvise_web.app.fetch_doctrine", lambda query, limit=3: list(doctrine or [])
    )


def _seed(db: Path) -> None:
    conn = open_db(db)
    ensure_paper_account(conn)
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_700_000_000_000,
                "timeframe": "4h",
                "open": 85000.0,
                "high": 85500.0,
                "low": 84500.0,
                "close": 85000.0,
                "volume": 1.0,
            }
        ],
    )
    write_indicators(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": 1_700_000_000_000,
                "atr_14": 1000.0,
                "rsi_14": 65.0,
                "ema_20": 84000.0,
                "ema_200": 80000.0,
            }
        ],
    )
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "an-1",
            "timestamp": 1_700_000_000_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "action": "long",
            "invalidation_price": 83500.0,
            "size_pct_equity": 1.125,
            "thesis": "trend_up",
        },
    )
    upsert_pending_approval(
        conn,
        {
            "id": "ap-1",
            "created_at_ms": 1_000,
            "expires_at_ms": 9_999_999_999_999,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "an-1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "size_pct_equity": 1.125,
            "status": "pending",
        },
    )
    conn.close()


def test_recommendation_200_template(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch, doctrine=["structure first"])
    db = tmp_path / "w.db"
    _seed(db)
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/approvals/ap-1/recommendation")
    assert resp.status_code == 200
    card = resp.json()
    assert card["ok"] is True
    assert card["recommendation"] == "approve"
    assert card["recommendation_source"] == "template"
    assert card["doctrine"] == ["structure first"]
    assert card["risk"]["notional_usd"] == 112.5
    assert card["claude"] is None


def test_recommendation_claude_source(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch)
    db = tmp_path / "w2.db"
    _seed(db)
    conn = open_db(db)
    insert_llm_review(
        conn,
        {
            "approval_id": "ap-1",
            "model": "anthropic/claude-sonnet-4.5",
            "decision": "approve",
            "reason": "แนวโน้มชัด",
            "brief_hash": "h",
            "created_at_ms": 2_000,
        },
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    card = client.get("/api/approvals/ap-1/recommendation").json()
    assert card["recommendation_source"] == "claude"
    assert card["claude"] == {
        "decision": "approve",
        "reason": "แนวโน้มชัด",
        "model": "anthropic/claude-sonnet-4.5",
        "at_ms": 2_000,
    }


def test_recommendation_404(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch)
    db = tmp_path / "w3.db"
    _seed(db)
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    assert client.get("/api/approvals/nope/recommendation").status_code == 404


def test_status_exposes_paper_auto_and_ingest_health(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch)
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "missing.db"))
    client = TestClient(app)
    payload = client.get("/api/status").json()
    assert payload["paper_auto"] == {"ok": True, "key": "jarvise:paper_auto:last"}
    assert payload["ingest_health"] == {"ok": True, "key": "jarvise:ingest:health"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web_recommendation.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'fetch_doctrine'`

- [ ] **Step 3: Implement in `app.py`**

1. Extend the `jarvise_ingest.db` import block to also import `get_analysis_output, get_approval, get_latest_llm_review, get_paper_position, load_latest_candle` (keep alphabetical order inside the parentheses).
2. Add `from jarvise_paper.recommendation import build_recommendation, doctrine_query` after the `jarvise_paper.metrics` import, and change `from jarvise_risk import load_risk_caps` to `from jarvise_risk import evaluate_from_db, load_risk_caps`.
3. After `PAPER_EXPIRE_KEY = ...` add:

```python
PAPER_AUTO_KEY = "jarvise:paper_auto:last"
INGEST_HEALTH_KEY = "jarvise:ingest:health"
JOBS_URL = os.environ.get("JARVISE_JOBS_URL", "http://jobs:8090")
```

4. In `status_payload()` add two entries after `"paper_expire": ...`:

```python
        "paper_auto": redis_get_json(PAPER_AUTO_KEY),
        "ingest_health": redis_get_json(INGEST_HEALTH_KEY),
```

5. After `load_metrics()` add:

```python
def fetch_doctrine(query: str, *, limit: int = 3) -> list[str]:
    """Doctrine snippets via the jobs service (it owns the embedding model). [] on any error."""
    headers: dict[str, str] = {}
    token = os.environ.get("JARVISE_JOBS_TOKEN") or ""
    if token:
        headers["X-Jarvise-Token"] = token
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{JOBS_URL}/doctrine",
                params={"q": query, "limit": limit},
                headers=headers,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("doctrine fetch soft-fail: %s", type(exc).__name__)
        return []
    return [str(h.get("text")) for h in hits if isinstance(h, dict) and h.get("text")]


def load_recommendation(approval_id: str) -> dict[str, Any] | None:
    path = db_path()
    if not path.exists():
        return None
    conn = open_db(path)
    try:
        row = get_approval(conn, approval_id)
        if row is None:
            return None
        symbol = str(row["symbol"])
        candle = load_latest_candle(conn, symbol, str(row["timeframe"]))
        analysis = (
            get_analysis_output(conn, str(row["analysis_id"])) if row.get("analysis_id") else None
        )
        account = ensure_paper_account(conn)
        position = get_paper_position(conn, symbol)
        safety = evaluate_from_db(conn, symbol).as_dict()
        review = get_latest_llm_review(conn, approval_id)
    finally:
        conn.close()
    claude = (
        {
            "decision": review["decision"],
            "reason": review["reason"],
            "model": review["model"],
            "at_ms": review["created_at_ms"],
        }
        if review
        else None
    )
    return build_recommendation(
        approval=row,
        candle=candle,
        analysis=analysis,
        account=account,
        position=position,
        safety=safety,
        doctrine=fetch_doctrine(doctrine_query(row)),
        claude=claude,
    )
```

6. After the `api_approvals` route add:

```python
@app.get("/api/approvals/{approval_id}/recommendation")
def api_approval_recommendation(
    approval_id: str, _: None = Depends(require_auth)
) -> dict[str, Any]:
    card = load_recommendation(approval_id)
    if card is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return card
```

- [ ] **Step 4: Wire compose + `.env.example`**

In `docker-compose.yml`, web service `environment`, after `- QDRANT_COLLECTION=...` add:

```yaml
      - JARVISE_JOBS_URL=http://jobs:8090
      - JARVISE_JOBS_TOKEN=${JARVISE_JOBS_TOKEN:-}
```

In `.env.example`, after the `JOBS_PORT=8090` line add:

```bash
# Web → jobs for doctrine snippets on the recommendation card (docker network only).
JARVISE_JOBS_URL=http://jobs:8090
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_web_recommendation.py tests/test_web.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/jarvise_web/app.py docker-compose.yml .env.example tests/test_web_recommendation.py
git commit -m "feat(web): recommendation card endpoint + paper_auto/ingest_health status keys"
```

---

### Task 5: SPA — recommendation card in the approval queue

**Files:**
- Modify: `web/src/lib/api.ts`
- Create: `web/src/components/RecommendationCard.tsx`
- Modify: `web/src/components/ApprovalQueue.tsx`

**Interfaces:**
- Consumes: `GET /api/approvals/{id}/recommendation` (Task 4).
- Produces: `RecommendationPayload` type, `api.recommendation(id)`, `<RecommendationCard data />`.

- [ ] **Step 1: Add the type and client call to `api.ts`**

After `ApprovalRow` add:

```ts
export type RecommendationPayload = {
  ok: boolean;
  approval_id: string;
  symbol: string;
  timeframe: string;
  action: string;
  status?: string;
  recommendation: "approve" | "approve_with_caution" | "reject";
  recommendation_source: "template" | "claude";
  confidence_label: string;
  headline: string;
  what_happened: string[];
  risk: {
    size_pct_equity: number | null;
    notional_usd: number | null;
    equity_usd: number;
    invalidation_price: number | null;
    est_loss_usd?: number;
    stop_atr_multiple: number;
  };
  doctrine: string[];
  checklist: string[];
  thesis?: string | null;
  claude: {
    decision: string;
    reason: string | null;
    model: string;
    at_ms: number;
  } | null;
};
```

In the `api` object, after `approvals: ...` add:

```ts
  recommendation: (id: string) =>
    request<RecommendationPayload>(
      `/api/approvals/${encodeURIComponent(id)}/recommendation`,
    ),
```

- [ ] **Step 2: Create `RecommendationCard.tsx`**

```tsx
// web/src/components/RecommendationCard.tsx
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import type { RecommendationPayload } from "@/lib/api";
import { formatNum } from "@/lib/utils";

const REC_LABEL: Record<
  RecommendationPayload["recommendation"],
  { text: string; variant: "ok" | "default" | "danger" }
> = {
  approve: { text: "แนะนำ APPROVE", variant: "ok" },
  approve_with_caution: { text: "APPROVE ได้ แต่ระวัง", variant: "default" },
  reject: { text: "แนะนำ REJECT", variant: "danger" },
};

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-muted)]">
        {title}
      </div>
      {children}
    </div>
  );
}

function List({ items }: { items: string[] }) {
  return (
    <ul className="list-disc space-y-1 pl-5">
      {items.map((line, i) => (
        <li key={i}>{line}</li>
      ))}
    </ul>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-[var(--color-muted)]">{label}</dt>
      <dd className="font-medium tabular-nums">{value}</dd>
    </div>
  );
}

export function RecommendationCard({ data }: { data: RecommendationPayload }) {
  const rec = REC_LABEL[data.recommendation];
  return (
    <div className="space-y-4 rounded-md border border-[var(--color-border)] bg-[#121922] p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={rec.variant}>{rec.text}</Badge>
        <span className="font-medium">{data.headline}</span>
        <span className="text-xs text-[var(--color-muted)]">
          ความมั่นใจ: {data.confidence_label}
        </span>
      </div>

      {data.claude ? (
        <Section title={`Claude (${data.claude.model}) ตัดสิน: ${data.claude.decision}`}>
          <p>{data.claude.reason || "—"}</p>
        </Section>
      ) : null}

      <Section title="เกิดอะไรขึ้น">
        <List items={data.what_happened} />
      </Section>

      <Section title="ความเสี่ยง (paper)">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
          <Stat label="ขนาด (% พอร์ต)" value={formatNum(data.risk.size_pct_equity, 3)} />
          <Stat label="มูลค่า (USD)" value={formatNum(data.risk.notional_usd, 2)} />
          <Stat label="ราคาตัดขาดทุน" value={formatNum(data.risk.invalidation_price, 2)} />
          <Stat
            label="ขาดทุนโดยประมาณ (USD)"
            value={data.risk.est_loss_usd == null ? "—" : formatNum(data.risk.est_loss_usd, 2)}
          />
          <Stat label="พอร์ต paper (USD)" value={formatNum(data.risk.equity_usd, 2)} />
          <Stat label="ระยะ stop (x ATR)" value={formatNum(data.risk.stop_atr_multiple, 1)} />
        </dl>
      </Section>

      <Section title="หลักการ (doctrine)">
        {data.doctrine.length ? (
          <List items={data.doctrine} />
        ) : (
          <p className="text-[var(--color-muted)]">
            ยังไม่มีข้อความ doctrine ที่ตรงกับสัญญาณนี้
          </p>
        )}
      </Section>

      <Section title="เช็คก่อนกด">
        <List items={data.checklist} />
      </Section>
    </div>
  );
}
```

- [ ] **Step 3: Make rows expandable in `ApprovalQueue.tsx`**

Replace the file contents with:

```tsx
import { useState } from "react";
import { api, type ApprovalRow, type RecommendationPayload } from "@/lib/api";
import { RecommendationCard } from "@/components/RecommendationCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { expiresIn, formatNum } from "@/lib/utils";

function approveLabel(card: RecommendationPayload | undefined): string {
  if (!card) return "Approve";
  switch (card.recommendation) {
    case "approve":
      return "Approve (แนะนำ)";
    case "approve_with_caution":
      return "Approve (ระวัง)";
    case "reject":
      return "Approve";
    default: {
      const exhaustive: never = card.recommendation;
      return exhaustive;
    }
  }
}

function rejectLabel(card: RecommendationPayload | undefined): string {
  return card?.recommendation === "reject" ? "Reject (แนะนำ)" : "Reject";
}

export function ApprovalQueue({
  rows,
  killSwitch,
  onChanged,
}: {
  rows: ApprovalRow[];
  killSwitch: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [cards, setCards] = useState<Record<string, RecommendationPayload>>({});
  const [cardError, setCardError] = useState<Record<string, string>>({});

  async function act(id: string, kind: "approve" | "reject") {
    setBusy(id + kind);
    setError(null);
    try {
      if (kind === "approve") await api.approve(id);
      else await api.reject(id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function toggle(id: string) {
    const next = !open[id];
    setOpen((prev) => ({ ...prev, [id]: next }));
    if (!next || cards[id]) return;
    try {
      const card = await api.recommendation(id);
      setCards((prev) => ({ ...prev, [id]: card }));
    } catch (err) {
      setCardError((prev) => ({
        ...prev,
        [id]: err instanceof Error ? err.message : String(err),
      }));
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Approval queue</CardTitle>
        <CardDescription>
          Paper candidates awaiting your decision. Approve runs a simulated fill
          (live only when the LIVE banner is on). กด "ดูคำแนะนำ" เพื่ออ่านสรุปภาษาไทยก่อนตัดสินใจ
        </CardDescription>
      </CardHeader>
      <CardContent>
        {error ? (
          <p className="mb-3 text-sm text-[var(--color-danger)]">{error}</p>
        ) : null}
        {rows.length === 0 ? (
          <p className="text-sm text-[var(--color-muted)]">
            No pending approvals — pipeline idle. Paper jobs enqueue here on schedule.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-[var(--color-muted)]">
                <tr className="border-b border-[var(--color-border)]">
                  <th className="py-2 pr-3 font-medium">Symbol</th>
                  <th className="py-2 pr-3 font-medium">TF</th>
                  <th className="py-2 pr-3 font-medium">Action</th>
                  <th className="py-2 pr-3 font-medium">Conf</th>
                  <th className="py-2 pr-3 font-medium">Size%</th>
                  <th className="py-2 pr-3 font-medium">Expires</th>
                  <th className="py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const card = cards[r.id];
                  return [
                    <tr key={r.id} className="border-b border-[var(--color-border)]/60">
                      <td className="py-3 pr-3 font-medium">{r.symbol}</td>
                      <td className="py-3 pr-3">{r.timeframe}</td>
                      <td className="py-3 pr-3">
                        <Badge variant="outline">{String(r.action || "—")}</Badge>
                      </td>
                      <td className="py-3 pr-3 tabular-nums">
                        {formatNum(r.confidence_score, 2)}
                      </td>
                      <td className="py-3 pr-3 tabular-nums">
                        {formatNum(r.size_pct_equity, 2)}
                      </td>
                      <td className="py-3 pr-3 text-[var(--color-muted)]">
                        {expiresIn(r.expires_at_ms)}
                      </td>
                      <td className="py-3">
                        <div className="flex flex-wrap gap-2">
                          <Button size="sm" variant="ghost" onClick={() => void toggle(r.id)}>
                            {open[r.id] ? "ซ่อนคำแนะนำ" : "ดูคำแนะนำ"}
                          </Button>
                          <Button
                            size="sm"
                            disabled={killSwitch || busy !== null}
                            onClick={() => act(r.id, "approve")}
                          >
                            {busy === r.id + "approve" ? "…" : approveLabel(card)}
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy !== null}
                            onClick={() => act(r.id, "reject")}
                          >
                            {busy === r.id + "reject" ? "…" : rejectLabel(card)}
                          </Button>
                        </div>
                      </td>
                    </tr>,
                    open[r.id] ? (
                      <tr key={r.id + ":card"} className="border-b border-[var(--color-border)]/60">
                        <td colSpan={7} className="pb-4 pt-1">
                          {card ? (
                            <RecommendationCard data={card} />
                          ) : cardError[r.id] ? (
                            <p className="text-sm text-[var(--color-danger)]">
                              โหลดคำแนะนำไม่สำเร็จ: {cardError[r.id]}
                            </p>
                          ) : (
                            <p className="text-sm text-[var(--color-muted)]">กำลังโหลดคำแนะนำ…</p>
                          )}
                        </td>
                      </tr>
                    ) : null,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 4: Build**

Run: `cd web && npm install && npm run build`
Expected: `tsc -b` clean, `vite build` writes `web/dist/`. If `tsc` complains about the `never` default branch, keep it — it is required by the exhaustive-switch rule; fix the union instead.

- [ ] **Step 5: Manual check (local)**

Run from repo root with a seeded DB (any pending approval): `JARVISE_WEB_DIST=web/dist python -m uvicorn jarvise_web.app:app --port 8080`, open `http://127.0.0.1:8080/`, click "ดูคำแนะนำ" on a row. Expected: card renders Thai sections; Approve label changes to "Approve (แนะนำ)" / "Approve (ระวัง)"; buttons are never disabled by the card.

- [ ] **Step 6: Commit**

```bash
git add web/src/lib/api.ts web/src/components/RecommendationCard.tsx web/src/components/ApprovalQueue.tsx
git commit -m "feat(web): Thai recommendation card per pending approval row"
```

---

# Phase B — Ingest-health alert

### Task 6: `count_indicator_ready` + `health.py`

**Files:**
- Modify: `src/jarvise_ingest/db.py` (after `count_derivatives`)
- Create: `src/jarvise_ingest/health.py`
- Test: `tests/test_ingest_health.py`

**Interfaces:**
- Consumes: `count_market`, `load_latest_candle` (db); `find_gaps` (`jarvise_ingest.series`); `INTERVAL_MS`.
- Produces: `count_indicator_ready(conn, symbol: str, timeframe: str, *, column: str = "ema_200") -> int`; `ingest_health(conn, symbols: list[str], timeframe: str, *, now_ms: int | None = None, max_age_min: float = 60.0) -> dict[str, Any]` with keys `ok, timeframe, symbols{SYM: {rows, ema200_ready, newest_age_min, gaps}}, alerts[], backfill_hint, at_ms, paper_only`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_health.py
from pathlib import Path

from jarvise_ingest.db import (
    count_indicator_ready,
    open_db,
    upsert_market_technicals,
    write_indicators,
)
from jarvise_ingest.health import ingest_health

H4 = 14_400_000
T0 = 1_700_000_000_000


def _candles(conn, symbol: str, n: int, *, skip_index: int | None = None) -> None:
    rows = []
    for i in range(n):
        if i == skip_index:
            continue
        rows.append(
            {
                "symbol": symbol,
                "timestamp": T0 + i * H4,
                "timeframe": "4h",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
            }
        )
    upsert_market_technicals(conn, rows)


def test_count_indicator_ready_counts_non_null(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "h.db")
    _candles(conn, "BTCUSDT", 3)
    assert count_indicator_ready(conn, "BTCUSDT", "4h") == 0
    write_indicators(
        conn,
        [
            {"symbol": "BTCUSDT", "timeframe": "4h", "timestamp": T0 + 2 * H4,
             "atr_14": 1.0, "rsi_14": 50.0, "ema_20": 100.0, "ema_200": 100.0},
        ],
    )
    assert count_indicator_ready(conn, "BTCUSDT", "4h") == 1


def test_health_ready_and_fresh_no_alerts(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "h2.db")
    _candles(conn, "BTCUSDT", 3)
    write_indicators(
        conn,
        [
            {"symbol": "BTCUSDT", "timeframe": "4h", "timestamp": T0 + 2 * H4,
             "atr_14": 1.0, "rsi_14": 50.0, "ema_20": 100.0, "ema_200": 100.0},
        ],
    )
    newest_close = T0 + 2 * H4 + H4
    report = ingest_health(conn, ["BTCUSDT"], "4h", now_ms=newest_close + 10 * 60_000)
    assert report["ok"] is True
    assert report["alerts"] == []
    sym = report["symbols"]["BTCUSDT"]
    assert sym == {"rows": 3, "ema200_ready": 1, "newest_age_min": 10.0, "gaps": 0}
    assert "--since 2021-01-01" in report["backfill_hint"]
    assert report["paper_only"] is True


def test_health_alerts_not_ready_stale_and_gap(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "h3.db")
    _candles(conn, "BTCUSDT", 4, skip_index=1)
    newest_close = T0 + 3 * H4 + H4
    report = ingest_health(conn, ["BTCUSDT", "ETHUSDT"], "4h", now_ms=newest_close + 90 * 60_000)
    assert report["ok"] is False
    btc = report["symbols"]["BTCUSDT"]
    assert btc["ema200_ready"] == 0
    assert btc["gaps"] == 1
    assert btc["newest_age_min"] == 90.0
    assert report["symbols"]["ETHUSDT"]["rows"] == 0
    joined = "\n".join(report["alerts"])
    assert "BTCUSDT: ema_200 not ready" in joined
    assert "BTCUSDT: newest 4h candle closed 90 min ago" in joined
    assert "BTCUSDT: 1 gap(s)" in joined
    assert "ETHUSDT: no 4h candles stored" in joined
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ingest_health.py -q`
Expected: FAIL with `ImportError: cannot import name 'count_indicator_ready'`

- [ ] **Step 3: Add `count_indicator_ready` to `db.py`**

After `count_derivatives`:

```python
_INDICATOR_COLUMNS = frozenset({"atr_14", "rsi_14", "ema_20", "ema_200"})


def count_indicator_ready(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    *,
    column: str = "ema_200",
) -> int:
    """Rows where an indicator column is populated (warm-up complete)."""
    if column not in _INDICATOR_COLUMNS:
        raise ValueError(f"unknown indicator column: {column}")
    cur = conn.execute(
        f"SELECT COUNT(*) FROM market_technicals "
        f"WHERE symbol=? AND timeframe=? AND {column} IS NOT NULL",
        (symbol.upper(), timeframe),
    )
    return int(cur.fetchone()[0])
```

- [ ] **Step 4: Create `health.py`**

```python
# src/jarvise_ingest/health.py
"""Ingest health: is indicator warm-up complete and is the stored series fresh?

Read-only. Alert-only — this module never triggers ingest and never places orders.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from jarvise_ingest.db import count_indicator_ready, count_market, load_latest_candle
from jarvise_ingest.series import find_gaps
from jarvise_ingest.timeframes import INTERVAL_MS

BACKFILL_HINT = (
    "jarvise ingest --symbol {symbols} --timeframe {timeframe} "
    "--since 2021-01-01 --skip-derivatives --json"
)


def ingest_health(
    conn: sqlite3.Connection,
    symbols: list[str],
    timeframe: str,
    *,
    now_ms: int | None = None,
    max_age_min: float = 60.0,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    step = INTERVAL_MS[timeframe]
    out: dict[str, dict[str, Any]] = {}
    alerts: list[str] = []
    for raw in symbols:
        sym = raw.strip().upper()
        if not sym:
            continue
        rows = count_market(conn, sym, timeframe)
        ready = count_indicator_ready(conn, sym, timeframe, column="ema_200")
        latest = load_latest_candle(conn, sym, timeframe)
        newest_age_min: float | None = None
        if latest is not None:
            closed_at = int(latest["timestamp"]) + step
            newest_age_min = round(max(0.0, (ts - closed_at) / 60_000.0), 1)
        gaps = len(find_gaps(conn, sym, timeframe))
        out[sym] = {
            "rows": rows,
            "ema200_ready": ready,
            "newest_age_min": newest_age_min,
            "gaps": gaps,
        }
        if rows == 0:
            alerts.append(f"{sym}: no {timeframe} candles stored")
        elif ready == 0:
            alerts.append(f"{sym}: ema_200 not ready ({rows} rows) — analyze stays flat")
        if newest_age_min is not None and newest_age_min > max_age_min:
            alerts.append(
                f"{sym}: newest {timeframe} candle closed {newest_age_min:.0f} min ago "
                f"(> {max_age_min:.0f})"
            )
        if gaps > 0:
            alerts.append(f"{sym}: {gaps} gap(s) in stored {timeframe} series")
    return {
        "ok": not alerts,
        "timeframe": timeframe,
        "symbols": out,
        "alerts": alerts,
        "backfill_hint": BACKFILL_HINT.format(symbols=",".join(out), timeframe=timeframe),
        "at_ms": ts,
        "paper_only": True,
    }
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_ingest_health.py tests/test_market_series.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/jarvise_ingest/db.py src/jarvise_ingest/health.py tests/test_ingest_health.py
git commit -m "feat(ingest): read-only ingest_health (ema200 ready, freshness, gaps)"
```

---

### Task 7: Jobs `POST /jobs/ingest-health` + Telegram alert + n8n + env

**Files:**
- Modify: `src/jarvise_notify/telegram.py`, `src/jarvise_notify/__init__.py`
- Modify: `src/jarvise/jobs.py`
- Create: `infra/n8n/workflows/jarvise-ingest-health.json`
- Modify: `.env.example`, `docker-compose.yml` (jobs env)
- Test: `tests/test_notify_telegram.py` (append), `tests/test_jobs.py` (append)

**Interfaces:**
- Consumes: `ingest_health` (Task 6), `publish_redis_status`, `send_telegram_message`, `notify_configured`.
- Produces: `format_ingest_health_message(payload: dict) -> str`, `notify_ingest_health(payload: dict, *, client: httpx.Client | None = None) -> bool`, `run_ingest_health() -> tuple[int, dict]`, route `POST /jobs/ingest-health`, Redis key `jarvise:ingest:health`, env `JARVISE_INGEST_HEALTH_MAX_AGE_MIN`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_notify_telegram.py`, add `format_ingest_health_message` and `notify_ingest_health` to the existing `from jarvise_notify.telegram import (...)` block at the top (alphabetical), then append:

```python
def test_ingest_health_message_and_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "ok": False,
        "timeframe": "4h",
        "symbols": {"BTCUSDT": {"rows": 217, "ema200_ready": 0, "newest_age_min": 12.0, "gaps": 0}},
        "alerts": ["BTCUSDT: ema_200 not ready (217 rows) — analyze stays flat"],
        "backfill_hint": "jarvise ingest --symbol BTCUSDT --timeframe 4h --since 2021-01-01 --skip-derivatives --json",
    }
    text = format_ingest_health_message(payload)
    assert "Jarvise ingest health (4h)" in text
    assert "ema_200 not ready" in text
    assert "--since 2021-01-01" in text

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify_ingest_health(payload) is False

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp
    assert notify_ingest_health(payload, client=client) is True
    assert notify_ingest_health({**payload, "ok": True, "alerts": []}, client=client) is False
    assert client.post.call_count == 1
```

In `tests/test_jobs.py`, add `run_ingest_health` to the existing `from jarvise.jobs import (...)` block at the top (alphabetical), then append:

```python
def test_ingest_health_missing_db_publishes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "missing.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_ingest_health()
    assert code == 0
    assert body["ok"] is False
    assert body["alerts"] and "database missing" in body["alerts"][0]
    assert body["telegram_sent"] is False
    assert published[0][0] == "jarvise:ingest:health"


def test_ingest_health_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "jarvise.jobs.run_ingest_health",
        lambda: (0, {"ok": True, "alerts": [], "paper_only": True}),
    )
    handler = _Handler()
    handler.path = "/jobs/ingest-health"
    handler._dispatch()
    assert handler._status == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notify_telegram.py tests/test_jobs.py -q`
Expected: FAIL with `ImportError: cannot import name 'format_ingest_health_message'`

- [ ] **Step 3: Telegram formatter + notifier**

Append to `src/jarvise_notify/telegram.py`:

```python
def format_ingest_health_message(payload: dict[str, Any]) -> str:
    lines = [f"Jarvise ingest health ({payload.get('timeframe')})"]
    for sym, info in (payload.get("symbols") or {}).items():
        lines.append(
            f"- {sym}: rows={info.get('rows')} ema200_ready={info.get('ema200_ready')} "
            f"age_min={info.get('newest_age_min')} gaps={info.get('gaps')}"
        )
    for alert in (payload.get("alerts") or [])[:DIGEST_CAP]:
        lines.append(f"! {alert}")
    hint = payload.get("backfill_hint")
    if hint:
        lines.append("One-shot backfill (VPS worker):")
        lines.append(str(hint))
    return "\n".join(lines)


def notify_ingest_health(
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Alert only when there are alerts. Soft-fail."""
    if not payload.get("alerts") or not notify_configured():
        return False
    return send_telegram_message(format_ingest_health_message(payload), client=client)
```

Update `src/jarvise_notify/__init__.py` import list and `__all__` to include `format_ingest_health_message` and `notify_ingest_health` (alphabetical).

- [ ] **Step 4: Jobs route**

In `src/jarvise/jobs.py`:

1. Imports: `from jarvise_ingest.db import list_approvals, open_db` → keep; add `from jarvise_ingest.health import ingest_health` and change `from jarvise_notify import notify_pending_digest` to `from jarvise_notify import notify_ingest_health, notify_pending_digest`. Add `import time` to stdlib imports.
2. Add helper after `_skipped`:

```python
def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default
```

3. Add after `run_paper_pending_digest`:

```python
def run_ingest_health() -> tuple[int, dict[str, Any]]:
    """Read-only warm-up/freshness check; alert only. Never triggers ingest, ignores kill-switch."""
    raw = os.environ.get("JARVISE_DB") or "data/analytics/jarvise.db"
    path = Path(raw)
    timeframe = os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
    symbols = [
        s for s in (os.environ.get("JARVISE_INGEST_SYMBOLS", "BTCUSDT,ETHUSDT")).split(",")
        if s.strip()
    ]
    max_age = _fenv("JARVISE_INGEST_HEALTH_MAX_AGE_MIN", 60.0)
    if not path.exists():
        payload: dict[str, Any] = {
            "ok": False,
            "timeframe": timeframe,
            "symbols": {},
            "alerts": [f"database missing: {path}"],
            "at_ms": int(time.time() * 1000),
            "paper_only": True,
        }
    else:
        conn = open_db(path)
        try:
            payload = ingest_health(conn, symbols, timeframe, max_age_min=max_age)
        finally:
            conn.close()
    payload["telegram_sent"] = notify_ingest_health(payload)
    publish_redis_status("jarvise:ingest:health", payload)
    return 0, payload
```

4. `ROUTES`: add `("POST", "/jobs/ingest-health"): "ingest_health",`
5. `_dispatch`: after the `paper_pending_digest` branch:

```python
        if action == "ingest_health":
            code, body = run_ingest_health()
            self._send(200 if code == 0 else 500, body)
            return
```

- [ ] **Step 5: n8n workflow + env**

Create `infra/n8n/workflows/jarvise-ingest-health.json`:

```json
{
  "id": "jrvsIngestHealth1h",
  "name": "Jarvise ingest health",
  "nodes": [
    {
      "parameters": {
        "rule": {
          "interval": [
            {
              "field": "hours",
              "hoursInterval": 1
            }
          ]
        }
      },
      "id": "cron-ingest-health",
      "name": "Every 1h",
      "type": "n8n-nodes-base.scheduleTrigger",
      "typeVersion": 1.2,
      "position": [0, 0]
    },
    {
      "parameters": {
        "method": "POST",
        "url": "http://jobs:8090/jobs/ingest-health",
        "options": {
          "timeout": 60000
        }
      },
      "id": "http-ingest-health",
      "name": "POST ingest health",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.2,
      "position": [260, 0],
      "notes": "Read-only: ema_200 warm-up, freshness, gaps on the paper timeframe. Telegram alert only when something is wrong. Never triggers ingest, never places orders. Jobs writes jarvise:ingest:health."
    }
  ],
  "connections": {
    "Every 1h": {
      "main": [[{ "node": "POST ingest health", "type": "main", "index": 0 }]]
    }
  },
  "settings": {
    "executionOrder": "v1"
  },
  "meta": {
    "templateCredsSetupCompleted": false,
    "jarvise": {
      "paper_only": true,
      "no_order_placement": true,
      "import": "n8n UI → Workflows → Import from File → infra/n8n/workflows/jarvise-ingest-health.json"
    }
  },
  "pinData": {},
  "active": false
}
```

`.env.example`, after the `JARVISE_PAPER_TIMEFRAME=4h` line:

```bash
# Ingest-health alert: newest paper-timeframe candle closed more than N minutes ago → Telegram.
JARVISE_INGEST_HEALTH_MAX_AGE_MIN=60
```

`docker-compose.yml` jobs `environment`, after `- JARVISE_APPROVAL_TIMEOUT_MIN=...`:

```yaml
      - JARVISE_INGEST_HEALTH_MAX_AGE_MIN=${JARVISE_INGEST_HEALTH_MAX_AGE_MIN:-60}
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest -q`
Expected: all PASS. Also `python -c "import json; json.load(open('infra/n8n/workflows/jarvise-ingest-health.json'))"` prints nothing.

- [ ] **Step 7: Commit**

```bash
git add src/jarvise_notify src/jarvise/jobs.py infra/n8n/workflows/jarvise-ingest-health.json .env.example docker-compose.yml tests/test_notify_telegram.py tests/test_jobs.py
git commit -m "feat(jobs): POST /jobs/ingest-health with Telegram alert + hourly n8n workflow"
```

---

### Task 8: Ops page — ingest health card

**Files:**
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/pages/OpsPage.tsx`

**Interfaces:**
- Consumes: `status.ingest_health` (Task 4 status key; Task 7 payload shape).
- Produces: `IngestHealthStatus` type.

- [ ] **Step 1: Add the type**

In `web/src/lib/api.ts` add before `StatusPayload`:

```ts
export type IngestHealthStatus = {
  ok: boolean;
  timeframe: string;
  symbols: Record<
    string,
    { rows: number; ema200_ready: number; newest_age_min: number | null; gaps: number }
  >;
  alerts: string[];
  backfill_hint?: string;
  at_ms: number;
  telegram_sent?: boolean;
};
```

and in `StatusPayload` after `paper_expire: unknown;` add `ingest_health?: IngestHealthStatus | null;`.

- [ ] **Step 2: Render the card in `OpsPage.tsx`**

Import `relativeAge` from `@/lib/utils` and `type IngestHealthStatus` from `@/lib/api`. Add a component above `OpsPage`:

```tsx
function IngestHealthCard({ health }: { health: IngestHealthStatus | null | undefined }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Ingest health</CardTitle>
        <CardDescription>
          EMA200 warm-up, freshness and gaps on the paper timeframe. Alert-only — fix with the one-shot backfill.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!health ? (
          <p className="text-[var(--color-muted)]">No health run recorded yet.</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={health.ok ? "ok" : "danger"}>{health.ok ? "healthy" : "attention"}</Badge>
              <span className="text-xs text-[var(--color-muted)]">
                {health.timeframe} · checked {relativeAge(health.at_ms)}
              </span>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              {Object.entries(health.symbols).map(([sym, s]) => (
                <div key={sym} className="rounded-md border border-[var(--color-border)] px-3 py-2">
                  <div className="font-medium">{sym}</div>
                  <div className="text-xs text-[var(--color-muted)]">
                    rows {s.rows} · EMA200 ready {s.ema200_ready} · age {s.newest_age_min ?? "—"} min · gaps {s.gaps}
                  </div>
                </div>
              ))}
            </div>
            {health.alerts.length ? (
              <ul className="list-disc space-y-1 pl-5 text-[var(--color-danger)]">
                {health.alerts.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            ) : null}
            {!health.ok && health.backfill_hint ? (
              <code className="block overflow-x-auto rounded-md bg-[#161d27] p-2 text-xs">
                {health.backfill_hint}
              </code>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
```

Insert `<IngestHealthCard health={status?.ingest_health} />` in `OpsPage` directly after the Pipeline `<Card>` and before the Qdrant card. Add `ingest_health: status?.ingest_health,` to the raw-JSON object.

- [ ] **Step 3: Build**

Run: `cd web && npm run build`
Expected: clean build.

- [ ] **Step 4: Commit**

```bash
git add web/src/lib/api.ts web/src/pages/OpsPage.tsx
git commit -m "feat(web): Ops ingest-health card"
```

---

# Phase C — Auto-decide (flag default off)

### Task 9: `llm_openrouter.py` — JSON-only chat client

**Files:**
- Create: `src/jarvise_paper/llm_openrouter.py`
- Test: `tests/test_llm_openrouter.py`

**Interfaces:**
- Produces: `OPENROUTER_URL`, `class OpenRouterError(RuntimeError)`, `chat_json(messages: list[dict[str, str]], *, model: str, timeout_s: float = 30.0, api_key: str | None = None, client: httpx.Client | None = None) -> dict[str, Any]`. One retry on transport error / 5xx; raises on missing key, 4xx, exhausted retry, malformed or non-object JSON.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_openrouter.py
import json
from unittest.mock import MagicMock

import httpx
import pytest

from jarvise_paper.llm_openrouter import OPENROUTER_URL, OpenRouterError, chat_json

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "{}"}]


def _resp(status: int, content: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"choices": [{"message": {"content": content}}]} if content is not None else {}
    return resp


def test_missing_key_raises_without_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = MagicMock(spec=httpx.Client)
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", client=client)
    assert client.post.call_count == 0


def test_ok_json_and_headers() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(200, json.dumps({"decision": "approve", "reason": "ok"}))
    out = chat_json(MSGS, model="anthropic/claude-sonnet-4.5", api_key="k", client=client)
    assert out == {"decision": "approve", "reason": "ok"}
    args, kwargs = client.post.call_args
    assert args[0] == OPENROUTER_URL
    assert kwargs["headers"]["Authorization"] == "Bearer k"
    assert kwargs["json"]["model"] == "anthropic/claude-sonnet-4.5"
    assert kwargs["json"]["response_format"] == {"type": "json_object"}


def test_fenced_json_is_accepted() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(200, '```json\n{"decision": "defer", "reason": "x"}\n```')
    assert chat_json(MSGS, model="m", api_key="k", client=client)["decision"] == "defer"


def test_4xx_raises_immediately() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(401)
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", api_key="k", client=client)
    assert client.post.call_count == 1


def test_5xx_retries_once_then_raises() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(503)
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", api_key="k", client=client)
    assert client.post.call_count == 2


def test_timeout_then_success() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = [httpx.ReadTimeout("slow"), _resp(200, '{"decision":"reject","reason":"r"}')]
    assert chat_json(MSGS, model="m", api_key="k", client=client)["decision"] == "reject"
    assert client.post.call_count == 2


def test_non_json_and_non_object_raise() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(200, "sure, approve it")
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", api_key="k", client=client)
    client.post.return_value = _resp(200, "[1, 2]")
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", api_key="k", client=client)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_llm_openrouter.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvise_paper.llm_openrouter'`

- [ ] **Step 3: Write the module**

```python
# src/jarvise_paper/llm_openrouter.py
"""Minimal OpenRouter chat client that returns one JSON object.

Second-layer paper reviewer only — the model never sees exchange credentials and
nothing here places orders. One retry on transport/5xx, then fail (caller defers).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_ATTEMPTS = 2


class OpenRouterError(RuntimeError):
    """Missing key, HTTP failure, timeout, or non-JSON-object content."""


def _parse_content(resp: httpx.Response) -> dict[str, Any]:
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise OpenRouterError("malformed response") from exc
    try:
        parsed = json.loads(_FENCE.sub("", str(content)).strip())
    except json.JSONDecodeError as exc:
        raise OpenRouterError("non-JSON content") from exc
    if not isinstance(parsed, dict):
        raise OpenRouterError("non-object JSON")
    return parsed


def chat_json(
    messages: list[dict[str, str]],
    *,
    model: str,
    timeout_s: float = 30.0,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise OpenRouterError("missing OPENROUTER_API_KEY")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://github.com/chatoe-norm/jarvise",
        "X-Title": "Jarvise paper auto-decide",
    }
    own = client is None
    http = client or httpx.Client(timeout=timeout_s)
    last_error = "openrouter failed"
    try:
        for _ in range(_ATTEMPTS):
            try:
                resp = http.post(OPENROUTER_URL, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"transport: {type(exc).__name__}"
                continue
            if resp.status_code >= 500:
                last_error = f"status {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise OpenRouterError(f"status {resp.status_code}")
            return _parse_content(resp)
    finally:
        if own:
            http.close()
    raise OpenRouterError(last_error)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_llm_openrouter.py -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/jarvise_paper/llm_openrouter.py tests/test_llm_openrouter.py
git commit -m "feat(paper): minimal OpenRouter JSON chat client (one retry, fail-closed)"
```

---

### Task 10: `auto_decide.py` — config, filter, brief, prompt, parser

**Files:**
- Create: `src/jarvise_paper/auto_decide.py`
- Test: `tests/test_auto_decide.py`

**Interfaces:**
- Consumes: `doctrine_query` (Task 2), `get_analysis_output`, `list_paper_positions`, `get_paper_position`, `ensure_paper_account`, `load_latest_candle` (db), `evaluate_from_db`, `load_risk_caps`, `estimated_notional`, `RiskCaps` (`jarvise_risk`).
- Produces: `PROMPT_VERSION = "2026-10-02.1"`, `DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"`, `UNPARSEABLE = "auto:claude:unparseable"`, `REASON_MAX = 280`, `SYSTEM_PROMPT`, `AutoDecideConfig(enabled, model, min_conf, max_per_run, timeout_s, api_key)`, `load_auto_decide_config() -> AutoDecideConfig`, `filter_candidates(conn, rows, *, min_conf, now_ms) -> tuple[list[dict], list[dict]]`, `build_brief(conn, row, *, doctrine: list[str], now_ms: int, caps: RiskCaps, min_conf: float) -> dict`, `brief_hash(brief: dict) -> str`, `parse_decision(obj: Any) -> tuple[str, str]`. Task 11 adds `run_auto_decide` to the same file.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auto_decide.py
from pathlib import Path

import pytest

from jarvise_ingest.db import (
    ensure_paper_account,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
    upsert_pending_approval,
    write_indicators,
)
from jarvise_paper.auto_decide import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    UNPARSEABLE,
    AutoDecideConfig,
    brief_hash,
    build_brief,
    filter_candidates,
    load_auto_decide_config,
    parse_decision,
)
from jarvise_risk import load_risk_caps

NOW = 1_700_000_000_000 + 14_400_000 + 60_000
FAR = 9_999_999_999_999


def seed_db(path: Path) -> object:
    conn = open_db(path)
    ensure_paper_account(conn)
    for sym, close in (("BTCUSDT", 85000.0), ("ETHUSDT", 3000.0)):
        upsert_market_technicals(
            conn,
            [{"symbol": sym, "timestamp": 1_700_000_000_000, "timeframe": "4h", "open": close,
              "high": close, "low": close, "close": close, "volume": 1.0}],
        )
        write_indicators(
            conn,
            [{"symbol": sym, "timeframe": "4h", "timestamp": 1_700_000_000_000, "atr_14": close * 0.01,
              "rsi_14": 60.0, "ema_20": close * 0.99, "ema_200": close * 0.9}],
        )
        upsert_analysis_output(
            conn,
            {"analysis_id": f"an-{sym}", "timestamp": 1_700_000_000_000, "symbol": sym, "timeframe": "4h",
             "regime_state": "trend_up", "confidence_score": 0.75, "action": "long",
             "invalidation_price": close * 0.98, "size_pct_equity": 1.125, "thesis": "trend_up"},
        )
    return conn


def pending(conn, approval_id: str, symbol: str, **over) -> dict:
    row = {
        "id": approval_id,
        "created_at_ms": 1_000,
        "expires_at_ms": FAR,
        "symbol": symbol,
        "timeframe": "4h",
        "analysis_id": f"an-{symbol}",
        "action": "long",
        "regime_state": "trend_up",
        "confidence_score": 0.75,
        "size_pct_equity": 1.125,
        "status": "pending",
    }
    row.update(over)
    return upsert_pending_approval(conn, row)


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "JARVISE_PAPER_AUTO_DECIDE", "JARVISE_AUTO_DECIDE_MODEL", "JARVISE_AUTO_DECIDE_MIN_CONF",
        "JARVISE_AUTO_DECIDE_MAX_PER_RUN", "JARVISE_AUTO_DECIDE_TIMEOUT_S", "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_auto_decide_config()
    assert cfg == AutoDecideConfig(
        enabled=False, model=DEFAULT_MODEL, min_conf=0.55, max_per_run=4, timeout_s=30.0, api_key=None
    )
    monkeypatch.setenv("JARVISE_PAPER_AUTO_DECIDE", "true")
    monkeypatch.setenv("JARVISE_AUTO_DECIDE_MAX_PER_RUN", "2")
    monkeypatch.setenv("OPENROUTER_API_KEY", " k ")
    cfg = load_auto_decide_config()
    assert cfg.enabled is True and cfg.max_per_run == 2 and cfg.api_key == "k"


def test_filter_candidates_reasons(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "f.db")
    # One pending row per symbol (unique index on pending symbol+timeframe). Filters never
    # need candles, so symbols without seeded OHLCV are fine here.
    pending(conn, "ok", "BTCUSDT", created_at_ms=5_000)
    pending(conn, "flat", "SOLUSDT", action="flat", size_pct_equity=0.0, created_at_ms=1_000)
    pending(conn, "zero", "ADAUSDT", size_pct_equity=0.0, created_at_ms=2_000)
    pending(conn, "low", "XRPUSDT", confidence_score=0.50, created_at_ms=3_000)
    pending(conn, "old", "ETHUSDT", expires_at_ms=NOW - 1, created_at_ms=4_000)
    rows = [dict(r) for r in conn.execute("SELECT * FROM approval_queue").fetchall()]
    eligible, filtered = filter_candidates(conn, rows, min_conf=0.55, now_ms=NOW)
    assert [r["id"] for r in eligible] == ["ok"]
    reasons = {f["id"]: f["reason"] for f in filtered}
    assert reasons == {"flat": "action_flat", "zero": "size_zero", "low": "low_conf", "old": "expired"}


def test_filter_force_flat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "ff.db")
    pending(conn, "ok", "BTCUSDT")

    class Unsafe:
        force_flat = True
        reasons = ["book_missing"]

    monkeypatch.setattr("jarvise_paper.auto_decide.evaluate_from_db", lambda *a, **k: Unsafe())
    rows = [dict(r) for r in conn.execute("SELECT * FROM approval_queue").fetchall()]
    eligible, filtered = filter_candidates(conn, rows, min_conf=0.55, now_ms=NOW)
    assert eligible == []
    assert filtered[0]["reason"] == "force_flat"


def test_build_brief_shape_and_hash(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "b.db")
    row = pending(conn, "ok", "BTCUSDT")
    brief = build_brief(conn, row, doctrine=["structure first"], now_ms=NOW,
                        caps=load_risk_caps(), min_conf=0.55)
    assert brief["candidate"]["symbol"] == "BTCUSDT"
    assert brief["candidate"]["invalidation_price"] == 85000.0 * 0.98
    assert brief["indicators"]["ema_200"] == 85000.0 * 0.9
    assert brief["ledger"] == {"equity": 10000.0, "cash": 10000.0, "open_positions": 0, "same_symbol_open": False}
    assert brief["market_safety"]["reasons"] == []
    assert brief["doctrine"] == ["structure first"]
    assert brief["policy"]["estimated_notional_usd"] == 112.5
    assert brief["policy"]["max_notional_per_order_usd"] == 2000.0
    assert brief["prompt_version"] == PROMPT_VERSION
    assert brief_hash(brief) == brief_hash(dict(brief))
    assert len(brief_hash(brief)) == 16


def test_parse_decision_strict() -> None:
    assert parse_decision({"decision": "approve", "reason": "ok"}) == ("approve", "ok")
    assert parse_decision({"decision": "REJECT", "reason": "x" * 300}) == ("reject", "x" * 280)
    assert parse_decision({"decision": "approve"}) == ("defer", UNPARSEABLE)
    assert parse_decision({"decision": "approve", "reason": "r", "extra": 1}) == ("defer", UNPARSEABLE)
    assert parse_decision({"decision": "maybe", "reason": "r"}) == ("defer", UNPARSEABLE)
    assert parse_decision("approve") == ("defer", UNPARSEABLE)


def test_system_prompt_pins_rules() -> None:
    assert "paper" in SYSTEM_PROMPT.lower()
    assert '"defer"' in SYSTEM_PROMPT
    assert "0.70" in SYSTEM_PROMPT
    assert PROMPT_VERSION in SYSTEM_PROMPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auto_decide.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvise_paper.auto_decide'`

- [ ] **Step 3: Write the module (Task 11 appends `run_auto_decide`)**

```python
# src/jarvise_paper/auto_decide.py
"""Second-layer paper reviewer: Jarvise filters, Claude (OpenRouter) confirms / rejects / defers.

Paper only. Decisions are applied exclusively through approve_approval / reject_approval,
so kill-switch, market safety, expiry and risk caps are re-checked by the existing code.
The auto path never submits exchange orders and refuses to run when live trading is on.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jarvise.rag import doctrine_snippets
from jarvise_ingest.db import (
    ensure_paper_account,
    get_analysis_output,
    get_approval,
    get_paper_position,
    insert_llm_review,
    list_approvals,
    list_paper_positions,
    load_latest_candle,
    set_approval_resolve_reason,
)
from jarvise_paper.approval import approve_approval, reject_approval
from jarvise_paper.llm_openrouter import OpenRouterError, chat_json
from jarvise_paper.recommendation import doctrine_query
from jarvise_risk import RiskCaps, estimated_notional, evaluate_from_db, load_risk_caps
from jarvise_trade import live_trading_enabled

PROMPT_VERSION = "2026-10-02.1"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
DECISIONS = frozenset({"approve", "reject", "defer"})
REASON_MAX = 280
UNPARSEABLE = "auto:claude:unparseable"
_TRUE = {"1", "true", "yes", "on"}

SYSTEM_PROMPT = f"""You are the second-layer reviewer for Jarvise, a PAPER trading ledger (no real orders).
Jarvise already filtered this candidate with deterministic rules. Your only job is to confirm, reject, or defer it.
Rules:
1. Reply with ONE JSON object and nothing else: {{"decision": "approve" | "reject" | "defer", "reason": "<= 280 chars"}}.
2. You MUST answer "defer" when ANY of these hold: doctrine is empty AND candidate.confidence_score < 0.70; ledger.same_symbol_open is true; market_safety.reasons is non-empty.
3. Never change size, direction, or price. Never suggest live orders.
4. "approve" only when indicators, doctrine and policy agree with the candidate's thesis; "reject" on a clear contradiction; otherwise "defer".
5. Write the reason in Thai, short, for an owner who does not read charts.
Prompt version: {PROMPT_VERSION}"""

ChatFn = Callable[..., dict[str, Any]]
DoctrineFn = Callable[[str], list[dict[str, Any]]]


@dataclass(frozen=True)
class AutoDecideConfig:
    enabled: bool
    model: str
    min_conf: float
    max_per_run: int
    timeout_s: float
    api_key: str | None


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def load_auto_decide_config() -> AutoDecideConfig:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip() or None
    return AutoDecideConfig(
        enabled=(os.environ.get("JARVISE_PAPER_AUTO_DECIDE") or "").strip().lower() in _TRUE,
        model=(os.environ.get("JARVISE_AUTO_DECIDE_MODEL") or DEFAULT_MODEL).strip(),
        min_conf=_fenv("JARVISE_AUTO_DECIDE_MIN_CONF", 0.55),
        max_per_run=max(0, int(_fenv("JARVISE_AUTO_DECIDE_MAX_PER_RUN", 4))),
        timeout_s=_fenv("JARVISE_AUTO_DECIDE_TIMEOUT_S", 30.0),
        api_key=key,
    )


def filter_candidates(
    conn: Any,
    rows: list[dict[str, Any]],
    *,
    min_conf: float,
    now_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Hard filters before any LLM call. Returns (eligible oldest-first, filtered_out)."""
    eligible: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r.get("created_at_ms") or 0)):
        reason: str | None = None
        action = str(row.get("action") or "flat").lower()
        size = float(row.get("size_pct_equity") or 0.0)
        conf = float(row.get("confidence_score") or 0.0)
        if action == "flat":
            reason = "action_flat"
        elif size <= 0.0:
            reason = "size_zero"
        elif conf < min_conf:
            reason = "low_conf"
        elif int(row.get("expires_at_ms") or 0) <= now_ms:
            reason = "expired"
        elif evaluate_from_db(conn, str(row["symbol"]), now_ms=now_ms).force_flat:
            reason = "force_flat"
        if reason:
            filtered.append({"id": row["id"], "symbol": row.get("symbol"), "reason": reason})
        else:
            eligible.append(row)
    return eligible, filtered


def build_brief(
    conn: Any,
    row: dict[str, Any],
    *,
    doctrine: list[str],
    now_ms: int,
    caps: RiskCaps,
    min_conf: float,
) -> dict[str, Any]:
    symbol = str(row["symbol"]).upper()
    timeframe = str(row["timeframe"])
    candle = load_latest_candle(conn, symbol, timeframe) or {}
    analysis = (
        get_analysis_output(conn, str(row["analysis_id"])) if row.get("analysis_id") else None
    ) or {}
    account = ensure_paper_account(conn)
    positions = list_paper_positions(conn)
    size = float(row.get("size_pct_equity") or 0.0)
    safety = evaluate_from_db(conn, symbol, now_ms=now_ms).as_dict()
    return {
        "candidate": {
            "symbol": symbol,
            "timeframe": timeframe,
            "action": row.get("action"),
            "regime_state": row.get("regime_state"),
            "confidence_score": row.get("confidence_score"),
            "size_pct_equity": size,
            "invalidation_price": analysis.get("invalidation_price"),
            "thesis": analysis.get("thesis"),
            "expires_at_ms": row.get("expires_at_ms"),
        },
        "indicators": {
            key: candle.get(key) for key in ("close", "ema_20", "ema_200", "rsi_14", "atr_14")
        },
        "ledger": {
            "equity": float(account["equity"]),
            "cash": float(account["cash"]),
            "open_positions": len(positions),
            "same_symbol_open": get_paper_position(conn, symbol) is not None,
        },
        "market_safety": {
            "ok": safety["ok"],
            "force_flat": safety["force_flat"],
            "reasons": list(safety["reasons"]),
        },
        "doctrine": list(doctrine),
        "policy": {
            "min_conf": min_conf,
            "max_notional_per_order_usd": float(caps.max_notional_per_order),
            "max_daily_loss_usd": float(caps.max_daily_loss_usd),
            "estimated_notional_usd": round(
                estimated_notional(equity=float(account["equity"]), size_pct_equity=size), 2
            ),
            "mode": "paper only — no real orders; never change size or direction",
        },
        "prompt_version": PROMPT_VERSION,
    }


def brief_hash(brief: dict[str, Any]) -> str:
    raw = json.dumps(brief, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def parse_decision(obj: Any) -> tuple[str, str]:
    """Strict contract: exactly {decision, reason}. Anything else → defer/unparseable."""
    if not isinstance(obj, dict) or set(obj) != {"decision", "reason"}:
        return "defer", UNPARSEABLE
    decision = str(obj.get("decision") or "").strip().lower()
    if decision not in DECISIONS:
        return "defer", UNPARSEABLE
    reason = str(obj.get("reason") or "").strip()[:REASON_MAX]
    return decision, reason
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_auto_decide.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/jarvise_paper/auto_decide.py tests/test_auto_decide.py
git commit -m "feat(paper): auto-decide config, hard filters, Claude brief and strict parser"
```

---

### Task 11: `run_auto_decide` — apply decisions through existing approval code

**Files:**
- Modify: `src/jarvise_paper/auto_decide.py` (append)
- Test: `tests/test_auto_decide.py` (append — reuses `seed_db`, `pending`, `NOW` already defined there in Task 10; `tests/` is not a package, so do not create a second file that would need to import them)

**Interfaces:**
- Consumes: everything from Task 10; `approve_approval(conn, id, *, kill_switch, now_ms)`, `reject_approval(conn, id, *, reason, now_ms)`; `insert_llm_review`, `set_approval_resolve_reason`, `get_approval`, `list_approvals`.
- Produces: `run_auto_decide(conn, *, now_ms: int | None = None, kill_switch: bool = False, config: AutoDecideConfig | None = None, chat: ChatFn = chat_json, doctrine_lookup: DoctrineFn | None = None) -> dict[str, Any]` returning the §9 payload: `{ok, paper_only, model, prompt_version, at_ms, skipped?, reason?, processed, approved[], rejected[], deferred[], filtered_out[], apply_failed[], doctrine_unavailable, duration_s}`.

- [ ] **Step 1: Write the failing tests**

Extend the top-level imports of `tests/test_auto_decide.py`: add `get_approval, get_latest_llm_review, get_paper_position, list_paper_orders` to the `jarvise_ingest.db` import block, add `run_auto_decide` to the `jarvise_paper.auto_decide` import block, and add `from jarvise_paper.llm_openrouter import OpenRouterError`. Then append:

```python
CFG = AutoDecideConfig(
    enabled=True, model="test/model", min_conf=0.55, max_per_run=4, timeout_s=1.0, api_key="k"
)
NO_DOCTRINE = lambda _q: []  # noqa: E731
DOCTRINE = lambda _q: [{"text": "structure first", "source": "d.md", "score": 0.9}]  # noqa: E731


@pytest.fixture(autouse=True)
def _live_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)


def _chat(decision: str, reason: str = "เหตุผล"):
    calls: list[dict] = []

    def fn(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return {"decision": decision, "reason": reason}

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def test_flag_off_skips_and_touches_nothing(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "a.db")
    pending(conn, "ok", "BTCUSDT")
    chat = _chat("approve")
    out = run_auto_decide(conn, now_ms=NOW, config=AutoDecideConfig(**{**CFG.__dict__, "enabled": False}),
                          chat=chat, doctrine_lookup=DOCTRINE)
    assert out["ok"] is True and out["skipped"] is True
    assert out["reason"] == "auto_decide_disabled" and out["paper_only"] is True
    assert chat.calls == []
    assert get_approval(conn, "ok")["status"] == "pending"


def test_live_on_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    conn = seed_db(tmp_path / "l.db")
    pending(conn, "ok", "BTCUSDT")
    chat = _chat("approve")
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=chat, doctrine_lookup=DOCTRINE)
    assert out["ok"] is False and out["skipped"] is True and out["reason"] == "live_trading_enabled"
    assert chat.calls == []
    assert get_approval(conn, "ok")["status"] == "pending"


def test_approve_path_fills_paper_and_prefixes_reason(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "ap.db")
    pending(conn, "ok", "BTCUSDT")
    chat = _chat("approve", "แนวโน้มชัด")
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=chat, doctrine_lookup=DOCTRINE)
    assert out["ok"] is True and out["processed"] == 1
    assert [a["id"] for a in out["approved"]] == ["ok"]
    assert out["rejected"] == [] and out["deferred"] == [] and out["apply_failed"] == []
    assert out["doctrine_unavailable"] is False
    row = get_approval(conn, "ok")
    assert row["status"] == "approved"
    assert row["resolve_reason"] == "auto:claude:approve"
    assert get_paper_position(conn, "BTCUSDT") is not None
    assert list_paper_orders(conn)
    review = get_latest_llm_review(conn, "ok")
    assert review["decision"] == "approve" and review["model"] == "test/model"
    assert len(chat.calls) == 1
    assert chat.calls[0]["model"] == "test/model"
    assert chat.calls[0]["messages"][0]["role"] == "system"


def test_reject_path_prefixes_reason_and_no_fill(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "rj.db")
    pending(conn, "ok", "BTCUSDT")
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=_chat("reject", "ขัดกับ doctrine"),
                          doctrine_lookup=DOCTRINE)
    assert [r["id"] for r in out["rejected"]] == ["ok"]
    row = get_approval(conn, "ok")
    assert row["status"] == "rejected"
    assert row["resolve_reason"] == "auto:claude:reject:ขัดกับ doctrine"
    assert get_paper_position(conn, "BTCUSDT") is None


def test_defer_error_and_garbage_leave_pending(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "df.db")
    pending(conn, "a", "BTCUSDT", created_at_ms=1)
    pending(conn, "b", "ETHUSDT", created_at_ms=2)
    answers = iter([OpenRouterError("status 503"), {"decision": "yes"}])

    def chat(messages, **kwargs):
        nxt = next(answers)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=chat, doctrine_lookup=NO_DOCTRINE)
    reasons = {d["id"]: d["reason"] for d in out["deferred"]}
    assert reasons["a"].startswith("auto:claude:error:")
    assert reasons["b"] == "auto:claude:unparseable"
    assert out["doctrine_unavailable"] is True
    assert get_approval(conn, "a")["status"] == "pending"
    assert get_approval(conn, "b")["status"] == "pending"
    assert get_latest_llm_review(conn, "b")["decision"] == "defer"
    assert list_paper_orders(conn) == []


def test_cap_and_missing_key_defer_without_calls(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "cap.db")
    pending(conn, "a", "BTCUSDT", created_at_ms=1)
    pending(conn, "b", "ETHUSDT", created_at_ms=2)
    chat = _chat("approve")
    out = run_auto_decide(conn, now_ms=NOW, config=AutoDecideConfig(**{**CFG.__dict__, "max_per_run": 1}),
                          chat=chat, doctrine_lookup=DOCTRINE)
    assert [a["id"] for a in out["approved"]] == ["a"]
    assert out["deferred"] == [{"id": "b", "symbol": "ETHUSDT", "reason": "deferred_cap"}]
    assert len(chat.calls) == 1

    conn2 = seed_db(tmp_path / "key.db")
    pending(conn2, "a", "BTCUSDT")
    chat2 = _chat("approve")
    out2 = run_auto_decide(conn2, now_ms=NOW, config=AutoDecideConfig(**{**CFG.__dict__, "api_key": None}),
                           chat=chat2, doctrine_lookup=DOCTRINE)
    assert out2["deferred"] == [{"id": "a", "symbol": "BTCUSDT", "reason": "missing_api_key"}]
    assert chat2.calls == []


def test_filtered_rows_never_reach_claude(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "fl.db")
    pending(conn, "flat", "BTCUSDT", action="flat", size_pct_equity=0.0)
    chat = _chat("approve")
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=chat, doctrine_lookup=DOCTRINE)
    assert out["filtered_out"] == [{"id": "flat", "symbol": "BTCUSDT", "reason": "action_flat"}]
    assert chat.calls == [] and out["processed"] == 0


def test_apply_failure_recorded_with_prefix(tmp_path: Path) -> None:
    conn = seed_db(tmp_path / "af.db")
    pending(conn, "big", "BTCUSDT", size_pct_equity=50.0)  # 5000 USD > 2000 cap → risk breach
    out = run_auto_decide(conn, now_ms=NOW, config=CFG, chat=_chat("approve"), doctrine_lookup=DOCTRINE)
    assert out["approved"] == []
    assert out["apply_failed"][0]["id"] == "big"
    assert "max_notional" in out["apply_failed"][0]["error"]
    row = get_approval(conn, "big")
    assert row["status"] == "failed"
    assert row["resolve_reason"].startswith("auto:apply_failed:max_notional")
```

Note: the risk-breach test calls `engage_kill_switch`, which is a no-op without `REDIS_URL`; `conftest.py` never sets it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auto_decide.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'run_auto_decide'`

- [ ] **Step 3: Append `run_auto_decide`**

Append to `src/jarvise_paper/auto_decide.py`:

```python
def _default_doctrine(query: str) -> list[dict[str, Any]]:
    return doctrine_snippets(query, limit=3)


def run_auto_decide(
    conn: Any,
    *,
    now_ms: int | None = None,
    kill_switch: bool = False,
    config: AutoDecideConfig | None = None,
    chat: ChatFn = chat_json,
    doctrine_lookup: DoctrineFn | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    cfg = config or load_auto_decide_config()
    started = time.monotonic()
    base: dict[str, Any] = {
        "ok": True,
        "paper_only": True,
        "model": cfg.model,
        "prompt_version": PROMPT_VERSION,
        "at_ms": ts,
    }
    if not cfg.enabled:
        return {**base, "skipped": True, "reason": "auto_decide_disabled"}
    if kill_switch:
        return {**base, "ok": False, "skipped": True, "reason": "kill_switch engaged"}
    if live_trading_enabled():
        return {**base, "ok": False, "skipped": True, "reason": "live_trading_enabled"}

    lookup = doctrine_lookup or _default_doctrine
    caps = load_risk_caps()
    pending_rows = list_approvals(conn, status="pending", limit=100)
    eligible, filtered_out = filter_candidates(conn, pending_rows, min_conf=cfg.min_conf, now_ms=ts)
    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    apply_failed: list[dict[str, Any]] = []
    doctrine_unavailable = False
    processed = 0

    for index, row in enumerate(eligible):
        entry = {"id": row["id"], "symbol": row.get("symbol")}
        if index >= cfg.max_per_run:
            deferred.append({**entry, "reason": "deferred_cap"})
            continue
        if not cfg.api_key:
            deferred.append({**entry, "reason": "missing_api_key"})
            continue
        processed += 1
        hits = lookup(doctrine_query(row))
        doctrine = [str(h.get("text")) for h in hits if isinstance(h, dict) and h.get("text")]
        if not doctrine:
            doctrine_unavailable = True
        brief = build_brief(conn, row, doctrine=doctrine, now_ms=ts, caps=caps, min_conf=cfg.min_conf)
        try:
            raw = chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(brief, ensure_ascii=False, default=str)},
                ],
                model=cfg.model,
                timeout_s=cfg.timeout_s,
                api_key=cfg.api_key,
            )
            decision, reason = parse_decision(raw)
        except OpenRouterError as exc:
            decision, reason = "defer", f"auto:claude:error:{exc}"[:REASON_MAX]
        insert_llm_review(
            conn,
            {
                "approval_id": row["id"],
                "model": cfg.model,
                "decision": decision,
                "reason": reason,
                "brief_hash": brief_hash(brief),
                "created_at_ms": ts,
            },
        )
        entry["reason"] = reason
        if decision == "approve":
            result = approve_approval(conn, row["id"], kill_switch=kill_switch, now_ms=ts)
            if result.get("ok"):
                set_approval_resolve_reason(conn, row["id"], "auto:claude:approve")
                approved.append({**entry, "fills": len(result.get("fills") or [])})
            else:
                inner = str(result.get("error") or "approve failed")
                after = get_approval(conn, row["id"])
                if after is not None and after["status"] != "pending":
                    set_approval_resolve_reason(conn, row["id"], f"auto:apply_failed:{inner}"[:400])
                apply_failed.append({**entry, "error": inner})
        elif decision == "reject":
            reject_approval(conn, row["id"], reason=f"auto:claude:reject:{reason}"[:400], now_ms=ts)
            rejected.append(entry)
        else:
            deferred.append(entry)

    return {
        **base,
        "processed": processed,
        "approved": approved,
        "rejected": rejected,
        "deferred": deferred,
        "filtered_out": filtered_out,
        "apply_failed": apply_failed,
        "doctrine_unavailable": doctrine_unavailable,
        "duration_s": round(time.monotonic() - started, 3),
    }
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_auto_decide.py tests/test_paper_approval.py -q`
Expected: all PASS (6 from Task 10 + 8 new)

- [ ] **Step 5: Commit**

```bash
git add src/jarvise_paper/auto_decide.py tests/test_auto_decide.py
git commit -m "feat(paper): run_auto_decide applies Claude decisions via approve/reject_approval"
```

---

### Task 12: Jobs `POST /jobs/paper-auto-decide` + Telegram + env + n8n chain

**Files:**
- Modify: `src/jarvise_notify/telegram.py`, `src/jarvise_notify/__init__.py`
- Modify: `src/jarvise/jobs.py`
- Modify: `infra/n8n/workflows/jarvise-paper-run.json`
- Modify: `.env.example`, `docker-compose.yml` (jobs env)
- Test: `tests/test_notify_telegram.py` (append), `tests/test_jobs.py` (append)

**Interfaces:**
- Consumes: `run_auto_decide` (Task 11), `kill_switch_engaged`, `publish_redis_status`.
- Produces: `format_auto_decide_message(payload: dict) -> str`, `notify_auto_decide(payload: dict, *, client=None) -> bool`, `run_paper_auto_decide() -> tuple[int, dict]`, route `POST /jobs/paper-auto-decide` (200 ok / 409 kill-switch or live refused / 500 otherwise), Redis `jarvise:paper_auto:last`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_notify_telegram.py`, add `format_auto_decide_message` and `notify_auto_decide` to the top-level `from jarvise_notify.telegram import (...)` block (alphabetical), then append:

```python
def test_auto_decide_message_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = {"ok": False, "skipped": True, "reason": "live_trading_enabled", "model": "m"}
    assert "REFUSED" in format_auto_decide_message(refused)
    assert "JARVISE_LIVE_TRADING" in format_auto_decide_message(refused)

    payload = {
        "ok": True,
        "model": "anthropic/claude-sonnet-4.5",
        "approved": [{"id": "a", "symbol": "BTCUSDT", "reason": "ok", "fills": 1}],
        "rejected": [],
        "deferred": [{"id": "b", "symbol": "ETHUSDT", "reason": "auto:claude:error:status 503"}],
        "apply_failed": [{"id": "c", "symbol": "SOLUSDT", "error": "approval expired"}],
    }
    text = format_auto_decide_message(payload)
    assert "approved=1 rejected=0 deferred=1 failed=1" in text
    assert "DEFER ETHUSDT id=b" in text
    assert "FAILED SOLUSDT id=c" in text

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp
    assert notify_auto_decide(payload, client=client) is True
    quiet = {**payload, "deferred": [], "apply_failed": []}
    assert notify_auto_decide(quiet, client=client) is False
    assert notify_auto_decide(refused, client=client) is True
    assert client.post.call_count == 2
```

In `tests/test_jobs.py`, add `run_paper_auto_decide` to the top-level `from jarvise.jobs import (...)` block and `from jarvise_ingest.db import open_db` to the top-level imports, then append:

```python
def test_paper_auto_decide_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: True)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_paper_auto_decide()
    assert code == 3 and body["skipped"] is True
    assert published[0][0] == "jarvise:paper_auto:last"


def test_paper_auto_decide_runs_and_publishes(monkeypatch, tmp_path) -> None:
    db = tmp_path / "auto.db"
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)
    monkeypatch.setattr(
        "jarvise.jobs.run_auto_decide",
        lambda conn, **kw: {"ok": True, "paper_only": True, "deferred": [], "apply_failed": []},
    )
    monkeypatch.setattr("jarvise.jobs.notify_auto_decide", lambda payload: False)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "jarvise.jobs.publish_redis_status",
        lambda key, payload: published.append((key, payload)),
    )
    code, body = run_paper_auto_decide()
    assert code == 0 and body["ok"] is True and body["telegram_sent"] is False
    assert published[0][0] == "jarvise:paper_auto:last"


def test_paper_auto_decide_live_refused_is_409(monkeypatch, tmp_path) -> None:
    db = tmp_path / "auto2.db"
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    monkeypatch.setattr("jarvise.jobs.kill_switch_engaged", lambda: False)
    monkeypatch.setattr(
        "jarvise.jobs.run_auto_decide",
        lambda conn, **kw: {"ok": False, "skipped": True, "reason": "live_trading_enabled", "paper_only": True},
    )
    monkeypatch.setattr("jarvise.jobs.notify_auto_decide", lambda payload: True)
    monkeypatch.setattr("jarvise.jobs.publish_redis_status", lambda *a, **k: None)
    code, body = run_paper_auto_decide()
    assert code == 3 and body["telegram_sent"] is True


def test_paper_auto_decide_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "jarvise.jobs.run_paper_auto_decide",
        lambda: (0, {"ok": True, "skipped": True, "reason": "auto_decide_disabled", "paper_only": True}),
    )
    handler = _Handler()
    handler.path = "/jobs/paper-auto-decide"
    handler._dispatch()
    assert handler._status == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notify_telegram.py tests/test_jobs.py -q`
Expected: FAIL with `ImportError: cannot import name 'format_auto_decide_message'`

- [ ] **Step 3: Telegram formatter + notifier**

Append to `src/jarvise_notify/telegram.py`:

```python
def format_auto_decide_message(payload: dict[str, Any]) -> str:
    if payload.get("skipped") and payload.get("reason") == "live_trading_enabled":
        return (
            "Jarvise auto-decide REFUSED: JARVISE_LIVE_TRADING=true.\n"
            "Auto path is paper-only; queue left for the owner."
        )
    approved = payload.get("approved") or []
    rejected = payload.get("rejected") or []
    deferred = payload.get("deferred") or []
    failed = payload.get("apply_failed") or []
    lines = [
        f"Jarvise paper auto-decide ({payload.get('model')})",
        f"approved={len(approved)} rejected={len(rejected)} deferred={len(deferred)} failed={len(failed)}",
    ]
    for d in deferred[:DIGEST_CAP]:
        lines.append(f"- DEFER {d.get('symbol')} id={d.get('id')}: {d.get('reason')}")
    for f in failed[:DIGEST_CAP]:
        lines.append(f"- FAILED {f.get('symbol')} id={f.get('id')}: {f.get('error')}")
    lines.append("Decide on Home (:8080) or: jarvise paper approve|reject <id>")
    return "\n".join(lines)


def notify_auto_decide(
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> bool:
    """One message per run when something needs the owner (defer / failure / live refusal)."""
    refused = bool(payload.get("skipped")) and payload.get("reason") == "live_trading_enabled"
    needs_owner = refused or bool(payload.get("deferred")) or bool(payload.get("apply_failed"))
    if not needs_owner or not notify_configured():
        return False
    return send_telegram_message(format_auto_decide_message(payload), client=client)
```

Add `format_auto_decide_message` and `notify_auto_decide` to the import list and `__all__` in `src/jarvise_notify/__init__.py`.

- [ ] **Step 4: Jobs route**

In `src/jarvise/jobs.py`:

1. Imports: change the notify import to `from jarvise_notify import notify_auto_decide, notify_ingest_health, notify_pending_digest` and add `from jarvise_paper.auto_decide import run_auto_decide`.
2. Add after `run_ingest_health`:

```python
def run_paper_auto_decide() -> tuple[int, dict[str, Any]]:
    """Second-layer Claude review of pending paper candidates. Paper only; flag default off."""
    key = "jarvise:paper_auto:last"
    if kill_switch_engaged():
        payload = _skipped()
        publish_redis_status(key, payload)
        return 3, payload
    raw = os.environ.get("JARVISE_DB") or "data/analytics/jarvise.db"
    path = Path(raw)
    if not path.exists():
        payload = {"ok": False, "skipped": True, "reason": "no_database", "paper_only": True}
        publish_redis_status(key, payload)
        return 1, payload
    conn = open_db(path)
    try:
        payload = run_auto_decide(conn)
    finally:
        conn.close()
    payload["telegram_sent"] = notify_auto_decide(payload)
    publish_redis_status(key, payload)
    if payload.get("skipped") and payload.get("reason") == "live_trading_enabled":
        return 3, payload
    return (0 if payload.get("ok") else 1), payload
```

3. `ROUTES`: add `("POST", "/jobs/paper-auto-decide"): "paper_auto_decide",`
4. `_dispatch`: after the `ingest_health` branch:

```python
        if action == "paper_auto_decide":
            code, body = run_paper_auto_decide()
            self._send(200 if code == 0 else 409 if code == 3 else 500, body)
            return
```

- [ ] **Step 5: Env + compose + n8n chain**

`.env.example`, after the `JARVISE_INGEST_HEALTH_MAX_AGE_MIN=60` block:

```bash
# Paper auto-decide (phase 1 of owner-controlled autonomy). Default OFF.
# When on: after each paper-run, Claude via OpenRouter (inside jobs) confirms / rejects / defers
# candidates that already passed Jarvise filters. Paper only — refuses when JARVISE_LIVE_TRADING=true.
JARVISE_PAPER_AUTO_DECIDE=false
JARVISE_AUTO_DECIDE_MODEL=anthropic/claude-sonnet-4.5
JARVISE_AUTO_DECIDE_MIN_CONF=0.55
JARVISE_AUTO_DECIDE_MAX_PER_RUN=4
JARVISE_AUTO_DECIDE_TIMEOUT_S=30
# Reuses OPENROUTER_API_KEY above; missing key → every candidate is deferred to you.
```

`docker-compose.yml` jobs `environment`, after `- JARVISE_INGEST_HEALTH_MAX_AGE_MIN=...`:

```yaml
      - JARVISE_PAPER_AUTO_DECIDE=${JARVISE_PAPER_AUTO_DECIDE:-false}
      - JARVISE_AUTO_DECIDE_MODEL=${JARVISE_AUTO_DECIDE_MODEL:-anthropic/claude-sonnet-4.5}
      - JARVISE_AUTO_DECIDE_MIN_CONF=${JARVISE_AUTO_DECIDE_MIN_CONF:-0.55}
      - JARVISE_AUTO_DECIDE_MAX_PER_RUN=${JARVISE_AUTO_DECIDE_MAX_PER_RUN:-4}
      - JARVISE_AUTO_DECIDE_TIMEOUT_S=${JARVISE_AUTO_DECIDE_TIMEOUT_S:-30}
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
```

`infra/n8n/workflows/jarvise-paper-run.json`: add a third node and connection. Insert after the `http-paper-run` node object:

```json
    {
      "parameters": {
        "method": "POST",
        "url": "http://jobs:8090/jobs/paper-auto-decide",
        "options": {
          "timeout": 180000
        }
      },
      "id": "http-paper-auto-decide",
      "name": "POST paper auto-decide",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.2,
      "position": [520, 0],
      "notes": "Second-layer Claude review (OpenRouter inside jobs). No-op unless JARVISE_PAPER_AUTO_DECIDE=true on jobs. Paper only; refuses when live trading is on. Writes jarvise:paper_auto:last. Only runs when paper-run returned 2xx."
    }
```

and replace the `connections` block with:

```json
  "connections": {
    "Every 4h": {
      "main": [[{ "node": "POST paper run", "type": "main", "index": 0 }]]
    },
    "POST paper run": {
      "main": [[{ "node": "POST paper auto-decide", "type": "main", "index": 0 }]]
    }
  },
```

Update the `http-paper-run` node `notes` to end with: `"… Approve still required on Home unless auto-decide is enabled."`

- [ ] **Step 6: Run tests + JSON check**

Run: `python -m pytest -q && python -c "import json; json.load(open('infra/n8n/workflows/jarvise-paper-run.json'))"`
Expected: all PASS, no JSON error.

- [ ] **Step 7: Commit**

```bash
git add src/jarvise_notify src/jarvise/jobs.py infra/n8n/workflows/jarvise-paper-run.json .env.example docker-compose.yml tests/test_notify_telegram.py tests/test_jobs.py
git commit -m "feat(jobs): POST /jobs/paper-auto-decide chained after paper-run (flag default off)"
```

---

### Task 13: Ops page — paper auto-decide card

**Files:**
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/pages/OpsPage.tsx`

**Interfaces:**
- Consumes: `status.paper_auto` (payload from Task 11/12).
- Produces: `PaperAutoStatus` type.

- [ ] **Step 1: Add the type**

In `web/src/lib/api.ts`, after `IngestHealthStatus`:

```ts
export type PaperAutoStatus = {
  ok: boolean;
  skipped?: boolean;
  reason?: string;
  model?: string;
  prompt_version?: string;
  processed?: number;
  approved?: Array<{ id: string; symbol?: string; reason?: string; fills?: number }>;
  rejected?: Array<{ id: string; symbol?: string; reason?: string }>;
  deferred?: Array<{ id: string; symbol?: string; reason?: string }>;
  filtered_out?: Array<{ id: string; symbol?: string; reason?: string }>;
  apply_failed?: Array<{ id: string; symbol?: string; error?: string }>;
  doctrine_unavailable?: boolean;
  duration_s?: number;
  at_ms?: number;
  telegram_sent?: boolean;
};
```

and in `StatusPayload` after `ingest_health?: ...` add `paper_auto?: PaperAutoStatus | null;`.

- [ ] **Step 2: Render the card**

In `OpsPage.tsx` import `type PaperAutoStatus` and add above `OpsPage`:

```tsx
function PaperAutoCard({ auto }: { auto: PaperAutoStatus | null | undefined }) {
  const counts = auto
    ? [
        ["approved", auto.approved?.length ?? 0],
        ["rejected", auto.rejected?.length ?? 0],
        ["deferred", auto.deferred?.length ?? 0],
        ["filtered", auto.filtered_out?.length ?? 0],
        ["failed", auto.apply_failed?.length ?? 0],
      ]
    : [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Paper auto-decide</CardTitle>
        <CardDescription>
          Claude (OpenRouter) second-layer review after each paper run. Paper only; off by default
          (JARVISE_PAPER_AUTO_DECIDE).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!auto ? (
          <p className="text-[var(--color-muted)]">No auto-decide run recorded yet.</p>
        ) : auto.skipped ? (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={auto.reason === "auto_decide_disabled" ? "muted" : "danger"}>
              {auto.reason === "auto_decide_disabled" ? "off" : "skipped"}
            </Badge>
            <span className="text-xs text-[var(--color-muted)]">
              {auto.reason} · {relativeAge(auto.at_ms)}
            </span>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={auto.ok ? "ok" : "danger"}>{auto.ok ? "ran" : "error"}</Badge>
              <span className="text-xs text-[var(--color-muted)]">
                {auto.model} · {relativeAge(auto.at_ms)} · {auto.duration_s ?? "—"}s
                {auto.doctrine_unavailable ? " · doctrine unavailable" : ""}
              </span>
            </div>
            <div className="grid grid-cols-5 gap-2">
              {counts.map(([label, n]) => (
                <div key={String(label)}>
                  <div className="text-xs text-[var(--color-muted)]">{label}</div>
                  <div className="font-medium tabular-nums">{n}</div>
                </div>
              ))}
            </div>
            {auto.deferred?.length ? (
              <ul className="list-disc space-y-1 pl-5">
                {auto.deferred.slice(0, 5).map((d) => (
                  <li key={d.id}>
                    <span className="font-medium">{d.symbol}</span> — {d.reason}
                  </li>
                ))}
              </ul>
            ) : null}
            {auto.apply_failed?.length ? (
              <ul className="list-disc space-y-1 pl-5 text-[var(--color-danger)]">
                {auto.apply_failed.slice(0, 5).map((f) => (
                  <li key={f.id}>
                    <span className="font-medium">{f.symbol}</span> — {f.error}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
```

Insert `<PaperAutoCard auto={status?.paper_auto} />` directly after `<IngestHealthCard … />`. Add `paper_auto: status?.paper_auto,` to the raw-JSON object.

- [ ] **Step 3: Build**

Run: `cd web && npm run build`
Expected: clean build.

- [ ] **Step 4: Commit**

```bash
git add web/src/lib/api.ts web/src/pages/OpsPage.tsx
git commit -m "feat(web): Ops paper auto-decide card"
```

---

### Task 14: Docs + manual VPS checklist

**Files:**
- Modify: `docs/product-usage.md`

- [ ] **Step 1: Update §2 automatic jobs list**

In `docs/product-usage.md` §2, replace the four-bullet "Automatic" list with:

```markdown
- Every ~15 minutes: `POST /jobs/ingest` → refresh market data
- Every ~6 hours: `POST /jobs/rag-refresh` → refresh doctrine RAG
- Every ~4 hours: `POST /jobs/paper-run` → enqueue paper candidates (`paper_core` @ `4h` by default; **no** `--auto-fill`), then `POST /jobs/paper-auto-decide` → no-op unless `JARVISE_PAPER_AUTO_DECIDE=true`
- Every ~1 hour: `POST /jobs/paper-expire` → mark timed-out approvals (no FLAT)
- Every ~1 hour: `POST /jobs/ingest-health` → Telegram alert when `ema_200` warm-up is missing, candles are stale, or the series has gaps (alert only; fix with the one-shot backfill shown in the message)
```

- [ ] **Step 2: Add the owner flow after the Telegram paragraph in §2**

```markdown
**Recommendation card (Thai):** every pending row on Home has "ดูคำแนะนำ" — what happened, dollar risk, doctrine, and a checklist, so you can Approve/Reject without reading charts. The Approve button is labelled "(แนะนำ)" or "(ระวัง)" from the template rule (`conf ≥ 0.70` approve, `0.55–0.69` caution, otherwise reject). Buttons are never disabled by the card.

**Paper auto-decide (optional, default off):** set `JARVISE_PAPER_AUTO_DECIDE=true` + `OPENROUTER_API_KEY` on the `jobs` service. After each paper-run, Claude reviews candidates that passed Jarvise's filters and approves (simulated fill, reason `auto:claude:approve`), rejects (`auto:claude:reject:<reason>`), or defers. Deferred rows stay pending and you get one Telegram summary per run. Spec: [paper auto-decide](superpowers/specs/2026-10-02-paper-auto-decide-design.md). Live stays off; the job refuses when `JARVISE_LIVE_TRADING=true`.
```

- [ ] **Step 3: Manual VPS checklist (run after deploy, paper only)**

Record results in the PR description:

1. Flag off (default): open Home, click "ดูคำแนะนำ" on a pending row → Thai card renders; `GET /api/status` shows `paper_auto: {"skipped": true, "reason": "auto_decide_disabled"}` after the next 4h run (or trigger via compose exec as in the backfill session).
2. `docker compose … exec -T jobs python -c "import urllib.request;print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8090/jobs/ingest-health', method='POST')).read()[:400])"` → payload with `ema200_ready > 0` for BTCUSDT/ETHUSDT 4h and `alerts: []`; Ops page shows the Ingest health card green.
3. Set `JARVISE_PAPER_AUTO_DECIDE=true` in `/opt/jarvise/.env`, `docker compose … up -d jobs`, trigger `POST /jobs/paper-auto-decide` → payload lists `approved` or `deferred`; Paper page shows a fill with reason `auto:claude:approve` if approved; Decisions shows `auto:` prefixed reasons.
4. Force a defer: temporarily blank `OPENROUTER_API_KEY` for jobs, re-trigger → every candidate `missing_api_key`, one Telegram message, rows still pending. Restore the key.
5. Set the flag back to `false` unless the owner decides to leave it on.

- [ ] **Step 4: Commit**

```bash
git add docs/product-usage.md
git commit -m "docs: recommendation card, auto-decide flag, ingest-health in product usage"
```

---

## Plan self-review

- **Spec coverage:** §4 components → Tasks 1–13 (every path in the spec's component table has a task; `web/src/lib/api.ts` is touched in 5, 8, 13). §5 env → Tasks 4, 7, 12. §6 card contract + SPA behaviour → Tasks 2, 4, 5. §7 brief + prompt + strict parse → Task 10. §8 error table → Tasks 9 (HTTP), 10 (filters), 11 (defer/apply_failed/cap/missing key/live), 12 (kill-switch 409, Telegram), 6–7 (ingest-health alerts). §9 audit → Tasks 1, 11, 12, 13. §10 tests → each task; manual checklist → Task 14. §11 non-goals respected (no live, no cadence change, no OpenClaw skill).
- **Placeholder scan:** no TBD / "add validation" / "similar to task N"; every code step shows the code.
- **Type consistency:** `build_recommendation(approval, candle, analysis, account, position, safety, doctrine, claude)` used identically in Tasks 2, 4; `doctrine_snippets` returns `[{text, source, score}]` consumed by Tasks 3, 4 (`fetch_doctrine` maps to `list[str]`) and 11 (`DoctrineFn`); `run_auto_decide(conn, *, now_ms, kill_switch, config, chat, doctrine_lookup)` in Tasks 11, 12; review row keys `approval_id, model, decision, reason, brief_hash, created_at_ms` in Tasks 1, 4, 11; Redis keys and env names match Global Constraints throughout.
