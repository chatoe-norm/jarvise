"""Private control + Command Dashboard API for Jarvise Docker stack."""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from jarvise_exchange.binance_spot import BinanceSpotClient, resolve_binance_auth
from jarvise_exchange.sync import sync_spot_balances
from jarvise_exchange.value import value_spot_balances
from jarvise_ingest.db import (
    count_analysis_output,
    ensure_paper_account,
    get_paper_account,
    list_analysis_output,
    list_approvals,
    list_paper_orders,
    list_paper_positions,
    open_db,
)
from jarvise_paper.approval import approve_approval, reject_approval
from jarvise_paper.metrics import compute_paper_metrics, persist_metrics_snapshot
from jarvise_risk import load_risk_caps
from jarvise_trade import live_trading_enabled

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
PAPER_KEY = "jarvise:paper:last"
PAPER_EXPIRE_KEY = "jarvise:paper:expire:last"
DEFAULT_DB = Path("data/analytics/jarvise.db")

_PKG_DIR = Path(__file__).resolve().parent
_STATIC_CANDIDATES = [
    Path(os.environ["JARVISE_WEB_DIST"]) if os.environ.get("JARVISE_WEB_DIST") else None,
    _PKG_DIR / "static",
    Path(__file__).resolve().parents[2] / "web" / "dist",
]


def static_dir() -> Path | None:
    """Prefer a built SPA (has assets/), else any index.html fallback."""
    found: Path | None = None
    for candidate in _STATIC_CANDIDATES:
        if candidate is None or not (candidate / "index.html").is_file():
            continue
        if (candidate / "assets").is_dir():
            return candidate
        if found is None:
            found = candidate
    return found


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


def wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return True
    return (request.query_params.get("format") or "").lower() == "json"


def parse_status_payload(raw: str) -> Any:
    """Parse Redis status: prefer JSON, fall back to CLI text emit (`key: value` lines)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    if ":" not in raw:
        raise json.JSONDecodeError("not JSON and not text status", raw, 0)
    parsed: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        low = value.lower()
        if low in {"true", "false"}:
            parsed[key] = low == "true"
        else:
            try:
                if "." in value:
                    parsed[key] = float(value)
                else:
                    parsed[key] = int(value)
            except ValueError:
                parsed[key] = value
    return parsed


def format_status_pre(payload: Any) -> str:
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, indent=2, default=str)
    return str(payload)


def redis_get(key: str) -> str | None:
    try:
        return _redis().get(key)
    except Exception:
        return None


def redis_get_json(key: str) -> Any:
    raw = redis_get(key)
    if raw is None:
        return None
    try:
        return parse_status_payload(raw)
    except Exception:
        return {"raw": raw}


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


def load_analysis_rows(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int, str | None]:
    path = db_path()
    if not path.exists():
        return [], 0, f"Database not found: {path}"
    try:
        conn = open_db(path)
        try:
            total = count_analysis_output(
                conn,
                symbol=symbol or None,
                timeframe=timeframe or None,
            )
            rows = list_analysis_output(
                conn,
                symbol=symbol or None,
                timeframe=timeframe or None,
                limit=limit,
                offset=offset,
            )
        finally:
            conn.close()
        return rows, total, None
    except Exception as exc:  # noqa: BLE001
        return [], 0, str(exc)


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


def kill_switch_engaged() -> bool:
    try:
        return (redis_get(KILL_SWITCH_KEY) or "0") in {"1", "true", "on", "yes"}
    except Exception:
        return True


def status_payload() -> dict[str, Any]:
    live = live_trading_enabled()
    return {
        "paper_only": PAPER_ONLY and not live,
        "live_trading": live,
        "kill_switch": kill_switch_engaged(),
        "ingest": redis_get_json(INGEST_KEY),
        "rag": redis_get_json(RAG_KEY),
        "paper": redis_get_json(PAPER_KEY),
        "paper_expire": redis_get_json(PAPER_EXPIRE_KEY),
        "risk_caps": load_risk_caps().as_dict(),
        "qdrant": qdrant_info(),
    }


def load_metrics() -> dict[str, Any]:
    path = db_path()
    if not path.exists():
        return {"ok": False, "paper_only": PAPER_ONLY, "error": "no database"}
    conn = open_db(path)
    try:
        ensure_paper_account(conn)
        report = compute_paper_metrics(conn)
    finally:
        conn.close()
    report["db"] = str(path)
    return report


def exchange_payload() -> dict[str, Any]:
    try:
        auth = resolve_binance_auth()
    except Exception as exc:  # noqa: BLE001
        logger.warning("exchange auth soft-fail: %s", type(exc).__name__)
        return {"ok": True, "available": False, "error": "auth"}
    if auth is None:
        return {"ok": True, "available": False}
    try:
        client = BinanceSpotClient(auth)
        result = sync_spot_balances(client=client, db_path=db_path(), dry_run=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("exchange sync soft-fail: %s", type(exc).__name__)
        return {"ok": True, "available": False, "error": type(exc).__name__}
    if not result.ok:
        return {"ok": True, "available": False, "error": result.error or "sync failed"}
    balances_out: list[dict[str, Any]] = []
    total_usd: float | None = None
    if result.balances:
        try:
            valued = value_spot_balances(result.balances)
        except Exception as exc:  # noqa: BLE001
            logger.warning("exchange value soft-fail: %s", type(exc).__name__)
            valued = None
        if valued is None:
            for b in result.balances:
                balances_out.append(
                    {
                        "asset": b.asset,
                        "free": str(b.free),
                        "locked": str(b.locked),
                        "total": str(b.total),
                        "usd": None,
                    }
                )
        else:
            total_usd = float(valued.total_usd)
            for row in valued.rows:
                b = row.balance
                balances_out.append(
                    {
                        "asset": b.asset,
                        "free": str(b.free),
                        "locked": str(b.locked),
                        "total": str(b.total),
                        "usd": float(row.usd) if row.usd is not None else None,
                    }
                )
    return {
        "ok": True,
        "available": True,
        "venue": result.venue,
        "fetched_at_ms": result.fetched_at_ms,
        "balances": balances_out,
        "total_usd": total_usd,
    }


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    live = live_trading_enabled()
    return {
        "ok": True,
        "paper_only": PAPER_ONLY and not live,
        "live_trading": live,
    }


@app.get("/api/status")
def api_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    return status_payload()


@app.get("/api/analysis")
def api_analysis(
    _: None = Depends(require_auth),
    symbol: str = Query(""),
    timeframe: str = Query(""),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    sym = symbol.strip().upper() or None
    tf = timeframe.strip() or None
    rows, total, err = load_analysis_rows(
        symbol=sym, timeframe=tf, limit=limit, offset=offset
    )
    payload: dict[str, Any] = {
        "paper_only": PAPER_ONLY,
        "db": str(db_path()),
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
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


@app.get("/api/paper/metrics")
def api_paper_metrics(_: None = Depends(require_auth)) -> dict[str, Any]:
    return load_metrics()


@app.get("/api/approvals")
def api_approvals(
    _: None = Depends(require_auth),
    status_filter: str = Query("pending", alias="status"),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    path = db_path()
    if not path.exists():
        return {"ok": True, "rows": []}
    conn = open_db(path)
    try:
        rows = list_approvals(conn, status=status_filter or None, limit=limit)
    finally:
        conn.close()
    return {"ok": True, "rows": rows}


@app.get("/api/exchange")
def api_exchange(_: None = Depends(require_auth)) -> dict[str, Any]:
    return exchange_payload()


@app.get("/api/dashboard")
def api_dashboard(_: None = Depends(require_auth)) -> dict[str, Any]:
    path = db_path()
    approvals: list[dict[str, Any]] = []
    if path.exists():
        conn = open_db(path)
        try:
            approvals = list_approvals(conn, status="pending", limit=20)
        finally:
            conn.close()
    paper_payload = api_paper()
    return {
        "ok": True,
        "status": status_payload(),
        "pending_count": len(approvals),
        "approvals": approvals,
        "paper": paper_payload,
        "metrics": load_metrics(),
    }


@app.post("/kill-switch")
async def set_kill_switch(
    request: Request,
    state: str = Form(...),
    _: None = Depends(require_auth),
) -> Any:
    value = "1" if state.lower() in {"on", "1", "true", "engage"} else "0"
    try:
        _redis().set(KILL_SWITCH_KEY, value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if wants_json(request):
        return JSONResponse({"ok": True, "kill_switch": value == "1"})
    return RedirectResponse("/", status_code=303)


@app.post("/approvals/approve")
async def approvals_approve(
    request: Request,
    id: str = Form(...),
    _: None = Depends(require_auth),
) -> Any:
    engaged = kill_switch_engaged()
    conn = open_db(db_path())
    try:
        result = approve_approval(conn, id, kill_switch=engaged)
    finally:
        conn.close()
    if wants_json(request):
        return JSONResponse({"ok": True, "result": result if isinstance(result, dict) else {"id": id}})
    return RedirectResponse("/", status_code=303)


@app.post("/approvals/reject")
async def approvals_reject(
    request: Request,
    id: str = Form(...),
    _: None = Depends(require_auth),
) -> Any:
    conn = open_db(db_path())
    try:
        reject_approval(conn, id, reason="ui")
    finally:
        conn.close()
    if wants_json(request):
        return JSONResponse({"ok": True, "id": id})
    return RedirectResponse("/", status_code=303)


@app.post("/paper/metrics/persist")
async def paper_metrics_persist(
    request: Request,
    _: None = Depends(require_auth),
) -> Any:
    path = db_path()
    report: dict[str, Any] | None = None
    if path.exists():
        conn = open_db(path)
        try:
            ensure_paper_account(conn)
            report = compute_paper_metrics(conn)
            persist_metrics_snapshot(conn, report)
        finally:
            conn.close()
    if wants_json(request):
        return JSONResponse({"ok": True, "metrics": report})
    return RedirectResponse("/", status_code=303)


def spa_index(_: None = Depends(require_auth)) -> FileResponse:
    root = static_dir()
    if root is None:
        raise HTTPException(
            status_code=503,
            detail="Command Dashboard assets missing — build web/ and set JARVISE_WEB_DIST",
        )
    return FileResponse(root / "index.html")


@app.get("/")
def home(_: None = Depends(require_auth)) -> FileResponse:
    return spa_index()


@app.get("/analytics")
def analytics_legacy(_: None = Depends(require_auth)) -> FileResponse:
    return spa_index()


@app.get("/paper")
@app.get("/decisions")
@app.get("/exchange")
@app.get("/ops")
def spa_routes(_: None = Depends(require_auth)) -> FileResponse:
    return spa_index()


_static = static_dir()
if _static is not None:
    assets = _static / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
