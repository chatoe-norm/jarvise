"""Thin private control + paper analytics UI for Jarvise Docker stack."""

from __future__ import annotations

import html
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from jarvise_exchange.binance_spot import BinanceSpotClient, resolve_binance_auth
from jarvise_exchange.sync import sync_spot_balances
from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_account,
    list_analysis_output,
    list_paper_orders,
    list_paper_positions,
    open_db,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Jarvise Control", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "jarvise_doctrine")
PAPER_ONLY = os.environ.get("JARVISE_PAPER_ONLY", "true").lower() in {"1", "true", "yes"}
KILL_SWITCH_KEY = "jarvise:kill_switch"
INGEST_KEY = "jarvise:ingest:last"
RAG_KEY = "jarvise:rag:last"
DEFAULT_DB = Path("data/analytics/jarvise.db")


def db_path() -> Path:
    raw = os.environ.get("JARVISE_DB") or str(DEFAULT_DB)
    return Path(raw)


def _redis():
    import redis

    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    user = os.environ.get("WEB_BASIC_AUTH_USER") or ""
    password = os.environ.get("WEB_BASIC_AUTH_PASSWORD") or ""
    if not user and not password:
        return
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username, user)
    pass_ok = secrets.compare_digest(credentials.password, password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def redis_get_json(key: str) -> Any:
    try:
        r = _redis()
        raw = r.get(key)
        if not raw:
            return None
        import json

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            preview = raw if len(raw) <= 240 else raw[:240] + "…"
            return {
                "ok": False,
                "error": f"invalid JSON in Redis ({exc})",
                "raw_preview": preview,
                "hint": "Re-run jarvise rag refresh/index so publish_redis_status writes JSON",
            }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def redis_get(key: str) -> str | None:
    try:
        return _redis().get(key)
    except Exception:
        return None


def qdrant_info() -> dict[str, Any]:
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{QDRANT_URL}/collections/{COLLECTION}")
            if resp.status_code == 404:
                return {"exists": False, "points": 0}
            resp.raise_for_status()
            result = resp.json().get("result") or {}
            return {
                "exists": True,
                "points": (result.get("points_count") or result.get("indexed_vectors_count") or 0),
                "status": result.get("status"),
            }
    except Exception as exc:  # noqa: BLE001
        return {"exists": False, "error": str(exc)}


def nav_html(active: str) -> str:
    control_cls = "active" if active == "control" else ""
    analytics_cls = "active" if active == "analytics" else ""
    return f"""
    <nav class="row" style="margin-bottom:1rem;gap:1rem">
      <a class="btn {control_cls}" href="/">Control</a>
      <a class="btn {analytics_cls}" href="/analytics">Analytics</a>
    </nav>
    """


def page(body: str, title: str = "Jarvise", *, active: str = "control") -> HTMLResponse:
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(title)}</title>
  <style>
    :root {{ --bg:#0f1419; --fg:#e7ecf1; --muted:#8b9aab; --accent:#3d9cf0; --danger:#e35d6a; --ok:#3ecf8e; }}
    body {{ margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background:var(--bg); color:var(--fg); }}
    main {{ max-width:960px; margin:0 auto; padding:2rem 1.25rem; }}
    h1 {{ font-size:1.5rem; margin:0 0 .25rem; }}
    .banner {{ display:inline-block; padding:.2rem .55rem; border:1px solid var(--ok); color:var(--ok); border-radius:4px; font-size:.8rem; margin-bottom:1.25rem; }}
    .card {{ border:1px solid #243041; border-radius:8px; padding:1rem 1.1rem; margin-bottom:1rem; }}
    .muted {{ color:var(--muted); font-size:.9rem; }}
    .row {{ display:flex; gap:.75rem; flex-wrap:wrap; align-items:center; }}
    button, .btn {{ background:var(--accent); color:#041018; border:0; border-radius:6px; padding:.5rem .9rem; font-weight:600; cursor:pointer; text-decoration:none; }}
    a.btn.active {{ outline:2px solid var(--ok); }}
    button.danger {{ background:var(--danger); color:#fff; }}
    button.ok {{ background:var(--ok); color:#041018; }}
    code {{ font-size:.85rem; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:#161d27; padding:.75rem; border-radius:6px; font-size:.8rem; }}
    table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
    th, td {{ text-align:left; padding:.45rem .4rem; border-bottom:1px solid #243041; vertical-align:top; }}
    th {{ color:var(--muted); font-weight:600; }}
    input, select {{ background:#161d27; color:var(--fg); border:1px solid #243041; border-radius:4px; padding:.35rem .5rem; }}
    label {{ font-size:.85rem; color:var(--muted); }}
  </style>
</head>
<body>
<main>
  <h1>{html.escape(title)}</h1>
  <div class="banner">PAPER ONLY — no order placement</div>
  {nav_html(active)}
  {body}
</main>
</body>
</html>"""
    return HTMLResponse(html_doc)


def load_analysis_rows(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], str | None]:
    path = db_path()
    if not path.exists():
        return [], f"Database not found: {path}"
    try:
        conn = open_db(path)
        try:
            rows = list_analysis_output(
                conn,
                symbol=symbol or None,
                timeframe=timeframe or None,
                limit=limit,
            )
        finally:
            conn.close()
        return rows, None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "paper_only": PAPER_ONLY}


@app.get("/", response_class=HTMLResponse)
def dashboard(_: None = Depends(require_auth)) -> HTMLResponse:
    kill = redis_get(KILL_SWITCH_KEY) or "0"
    ingest = redis_get_json(INGEST_KEY)
    rag = redis_get_json(RAG_KEY)
    qd = qdrant_info()
    kill_on = kill in {"1", "true", "on", "yes"}

    body = f"""
    <div class="card">
      <div class="row">
        <strong>Kill switch:</strong>
        <span style="color:{'var(--danger)' if kill_on else 'var(--ok)'}">{'ENGAGED' if kill_on else 'clear'}</span>
      </div>
      <form class="row" method="post" action="/kill-switch" style="margin-top:.75rem">
        <button class="danger" name="state" value="on" type="submit">Engage kill switch</button>
        <button class="ok" name="state" value="off" type="submit">Clear kill switch</button>
      </form>
      <p class="muted">Halts automated paper schedules that respect <code>{KILL_SWITCH_KEY}</code>. Live trading stays gated off.</p>
    </div>
    <div class="card">
      <strong>Qdrant</strong> <span class="muted">{html.escape(COLLECTION)}</span>
      <pre>{html.escape(str(qd))}</pre>
    </div>
    <div class="card">
      <strong>Last ingest</strong> <span class="muted">{html.escape(INGEST_KEY)}</span>
      <pre>{html.escape(str(ingest))}</pre>
    </div>
    <div class="card">
      <strong>Last RAG</strong> <span class="muted">{html.escape(RAG_KEY)}</span>
      <pre>{html.escape(str(rag))}</pre>
    </div>
    <p class="muted">Perimeter: Tailscale. Optional basic auth via WEB_BASIC_AUTH_*.</p>
    <p class="muted"><a class="btn" href="/analytics">Analytics</a></p>
    """
    return page(body, title="Jarvise control", active="control")


def _exchange_panel_html() -> str:
    """Soft-fail: return empty string when keys missing or sync fails."""
    try:
        auth = resolve_binance_auth()
    except Exception as exc:  # noqa: BLE001
        logger.warning("exchange auth soft-fail: %s", type(exc).__name__)
        return ""
    if auth is None:
        return ""
    try:
        client = BinanceSpotClient(auth)
        result = sync_spot_balances(client=client, db_path=db_path(), dry_run=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("exchange sync soft-fail: %s", type(exc).__name__)
        return ""
    if not result.ok:
        return ""
    if not result.balances:
        rows_html = "<p class=\"muted\">no non-zero assets</p>"
    else:
        lines = [
            "<tr><th>Asset</th><th>Free</th><th>Locked</th><th>Total</th></tr>"
        ]
        for b in result.balances:
            lines.append(
                f"<tr><td>{html.escape(b.asset)}</td><td>{html.escape(str(b.free))}</td>"
                f"<td>{html.escape(str(b.locked))}</td><td>{html.escape(str(b.total))}</td></tr>"
            )
        rows_html = (
            "<table>"
            + "".join(lines)
            + "</table>"
        )
    return f"""
    <div class="card">
      <strong>Exchange (spot)</strong>
      <span class="muted">binance · fetched_at_ms={result.fetched_at_ms} · read-only</span>
      {rows_html}
    </div>
    """


@app.post("/kill-switch")
def set_kill_switch(
    state: str = Form(...),
    _: None = Depends(require_auth),
) -> RedirectResponse:
    value = "1" if state.lower() in {"on", "1", "true", "engage"} else "0"
    try:
        _redis().set(KILL_SWITCH_KEY, value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse("/", status_code=303)


def load_paper_ledger() -> tuple[dict[str, Any], str | None]:
    path = db_path()
    if not path.exists():
        return {"account": None, "positions": [], "orders": []}, f"Database not found: {path}"
    try:
        conn = open_db(path)
        try:
            ensure_paper_account(conn)
            payload = {
                "account": get_paper_account(conn),
                "positions": list_paper_positions(conn),
                "orders": list_paper_orders(conn, limit=20),
            }
        finally:
            conn.close()
        return payload, None
    except Exception as exc:  # noqa: BLE001
        return {"account": None, "positions": [], "orders": []}, str(exc)


def _paper_ledger_html() -> str:
    ledger, err = load_paper_ledger()
    if err and ledger.get("account") is None:
        return (
            f'<div class="card"><strong>Paper ledger</strong>'
            f'<p class="muted">{html.escape(err)}</p></div>'
        )
    acct = ledger.get("account") or {}
    positions = ledger.get("positions") or []
    orders = ledger.get("orders") or []
    pos_rows = []
    for p in positions:
        pos_rows.append(
            "<tr>"
            f"<td>{html.escape(str(p.get('symbol') or ''))}</td>"
            f"<td>{html.escape(str(p.get('side') or ''))}</td>"
            f"<td>{html.escape(str(p.get('qty') or ''))}</td>"
            f"<td>{html.escape(str(p.get('entry_price') or ''))}</td>"
            f"<td>{html.escape(str(p.get('unrealized_pnl') or ''))}</td>"
            "</tr>"
        )
    pos_table = (
        "<p class='muted'>No open paper positions. Run <code>jarvise paper run</code>.</p>"
        if not pos_rows
        else (
            "<table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th>"
            "<th>Entry</th><th>uPnL</th></tr></thead>"
            f"<tbody>{''.join(pos_rows)}</tbody></table>"
        )
    )
    order_bits = []
    for o in orders[:10]:
        order_bits.append(
            f"{o.get('ts')} {o.get('symbol')} {o.get('side')} "
            f"qty={o.get('qty')} @ {o.get('price')} ({o.get('reason')})"
        )
    orders_pre = html.escape("\n".join(order_bits) if order_bits else "(no fills yet)")
    return f"""
    <div class="card">
      <strong>Paper ledger</strong>
      <p class="muted">Simulated only — no exchange orders.
        equity={html.escape(str(acct.get('equity', '')))}
        cash={html.escape(str(acct.get('cash', '')))}
      </p>
      {pos_table}
      <pre>{orders_pre}</pre>
    </div>
    """


@app.get("/analytics", response_class=HTMLResponse)
def analytics(
    _: None = Depends(require_auth),
    symbol: str = Query(""),
    timeframe: str = Query(""),
) -> HTMLResponse:
    sym = symbol.strip().upper() or None
    tf = timeframe.strip() or None
    rows, err = load_analysis_rows(symbol=sym, timeframe=tf, limit=50)
    ingest = redis_get_json(INGEST_KEY)
    rag = redis_get_json(RAG_KEY)

    form = f"""
    <div class="card">
      <form class="row" method="get" action="/analytics">
        <label>Symbol <input name="symbol" value="{html.escape(symbol.strip())}" placeholder="BTCUSDT"/></label>
        <label>Timeframe <input name="timeframe" value="{html.escape(timeframe.strip())}" placeholder="4h"/></label>
        <button type="submit">Filter</button>
        <a class="btn" href="/analytics">Clear</a>
      </form>
      <p class="muted" style="margin:.75rem 0 0">DB: <code>{html.escape(str(db_path()))}</code></p>
    </div>
    {_exchange_panel_html()}
    {_paper_ledger_html()}
    <div class="card">
      <strong>Pipeline status</strong>
      <pre>ingest={html.escape(str(ingest))}
rag={html.escape(str(rag))}</pre>
    </div>
    """

    if err:
        table = f'<div class="card"><p class="muted">{html.escape(err)}</p></div>'
    elif not rows:
        table = '<div class="card"><p class="muted">No analysis rows. Run <code>jarvise analyze</code> first.</p></div>'
    else:
        cells = []
        for r in rows:
            cells.append(
                "<tr>"
                f"<td>{html.escape(str(r.get('symbol') or ''))}</td>"
                f"<td>{html.escape(str(r.get('timeframe') or ''))}</td>"
                f"<td>{html.escape(str(r.get('regime_state') or ''))}</td>"
                f"<td>{html.escape(str(r.get('action') or ''))}</td>"
                f"<td>{html.escape(str(r.get('confidence_score') or ''))}</td>"
                f"<td>{html.escape(str(r.get('invalidation_price') if r.get('invalidation_price') is not None else ''))}</td>"
                f"<td>{html.escape(str(r.get('size_pct_equity') if r.get('size_pct_equity') is not None else ''))}</td>"
                f"<td>{html.escape(str(r.get('timestamp') or ''))}</td>"
                f"<td>{html.escape(str(r.get('thesis') or ''))}</td>"
                "</tr>"
            )
        table = f"""
        <div class="card" style="overflow-x:auto">
          <table>
            <thead>
              <tr>
                <th>Symbol</th><th>TF</th><th>Regime</th><th>Action</th>
                <th>Conf</th><th>Invalidation</th><th>Size%</th><th>Ts</th><th>Thesis</th>
              </tr>
            </thead>
            <tbody>
              {''.join(cells)}
            </tbody>
          </table>
        </div>
        """

    return page(form + table, title="Jarvise analytics", active="analytics")


@app.get("/api/analysis")
def api_analysis(
    _: None = Depends(require_auth),
    symbol: str = Query(""),
    timeframe: str = Query(""),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    sym = symbol.strip().upper() or None
    tf = timeframe.strip() or None
    rows, err = load_analysis_rows(symbol=sym, timeframe=tf, limit=limit)
    payload: dict[str, Any] = {
        "paper_only": PAPER_ONLY,
        "db": str(db_path()),
        "rows": rows,
    }
    if err:
        payload["ok"] = False
        payload["error"] = err
    else:
        payload["ok"] = True
    return payload


@app.get("/api/paper")
def api_paper(_: None = Depends(require_auth)) -> dict[str, Any]:
    ledger, err = load_paper_ledger()
    payload: dict[str, Any] = {
        "paper_only": PAPER_ONLY,
        "db": str(db_path()),
        "equity": (ledger.get("account") or {}).get("equity"),
        "cash": (ledger.get("account") or {}).get("cash"),
        "account": ledger.get("account"),
        "positions": ledger.get("positions") or [],
        "orders": ledger.get("orders") or [],
    }
    if err and ledger.get("account") is None:
        payload["ok"] = False
        payload["error"] = err
    else:
        payload["ok"] = True
    return payload


@app.get("/api/status")
def api_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    kill = redis_get(KILL_SWITCH_KEY) or "0"
    return {
        "paper_only": PAPER_ONLY,
        "kill_switch": kill in {"1", "true", "on", "yes"},
        "ingest": redis_get_json(INGEST_KEY),
        "rag": redis_get_json(RAG_KEY),
        "qdrant": qdrant_info(),
    }
