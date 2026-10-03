"""Private control + Command Dashboard API for Jarvise Docker stack."""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from jarvise_exchange.binance_spot import BinanceSpotClient, resolve_binance_auth
from jarvise_exchange.sync import sync_spot_balances
from jarvise_exchange.value import value_spot_balances
from jarvise_ingest.db import (
    count_analysis_output,
    ensure_paper_account,
    get_analysis_output,
    get_approval,
    get_latest_llm_review,
    get_paper_account,
    get_paper_position,
    list_analysis_output,
    list_approvals,
    list_paper_orders,
    list_paper_positions,
    load_latest_candle,
    open_db,
)
from jarvise_obs.metrics import render_prometheus, set_gauge
from jarvise_paper.approval import approve_approval, reject_approval
from jarvise_paper.metrics import compute_paper_metrics, persist_metrics_snapshot
from jarvise_paper.recommendation import build_recommendation, doctrine_query
from jarvise_risk import evaluate_from_db, load_risk_caps
from jarvise_trade import live_trading_enabled

logger = logging.getLogger(__name__)

app = FastAPI(title="Jarvise Control", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "jarvise_doctrine")
PAPER_ONLY = os.environ.get("JARVISE_PAPER_ONLY", "true").lower() in {"1", "true", "yes"}
KILL_SWITCH_KEY = "jarvise:kill_switch"
KILL_SWITCH_REASON_KEY = "jarvise:kill_switch:reason"
INGEST_KEY = "jarvise:ingest:last"
RAG_KEY = "jarvise:rag:last"
PAPER_KEY = "jarvise:paper:last"
PAPER_EXPIRE_KEY = "jarvise:paper:expire:last"
PAPER_AUTO_KEY = "jarvise:paper_auto:last"
INGEST_HEALTH_KEY = "jarvise:ingest:health"
JOBS_URL = os.environ.get("JARVISE_JOBS_URL", "http://jobs:8090")
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

    return redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=3.0,
        socket_timeout=3.0,
    )


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


def redis_get_strict(key: str) -> str | None:
    """Raise on Redis failure. Used where "unknown" must not read as "off" (kill-switch)."""
    return _redis().get(key)


def redis_get(key: str) -> str | None:
    """Soft read for status panels: Redis failure → None."""
    try:
        return redis_get_strict(key)
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


def kill_switch_state() -> dict[str, Any]:
    """Fail-closed kill-switch read: Redis unreachable → engaged=True, known=False."""
    try:
        raw = redis_get_strict(KILL_SWITCH_KEY)
    except Exception as exc:  # noqa: BLE001 — unknown must block, never read as "off"
        return {
            "engaged": True,
            "known": False,
            "reason": None,
            "error": f"{type(exc).__name__}",
        }
    engaged = str(raw or "0").strip().lower() in {"1", "true", "on", "yes"}
    reason = redis_get(KILL_SWITCH_REASON_KEY) if engaged else None
    return {"engaged": engaged, "known": True, "reason": reason, "error": None}


def kill_switch_engaged() -> bool:
    return bool(kill_switch_state()["engaged"])


def status_payload() -> dict[str, Any]:
    live = live_trading_enabled()
    ks = kill_switch_state()
    return {
        "paper_only": PAPER_ONLY and not live,
        "live_trading": live,
        "kill_switch": bool(ks["engaged"]),
        "kill_switch_state": ks,
        "ingest": redis_get_json(INGEST_KEY),
        "rag": redis_get_json(RAG_KEY),
        "paper": redis_get_json(PAPER_KEY),
        "paper_expire": redis_get_json(PAPER_EXPIRE_KEY),
        "paper_auto": redis_get_json(PAPER_AUTO_KEY),
        "ingest_health": redis_get_json(INGEST_HEALTH_KEY),
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
        if review is not None and int(review["created_at_ms"]) < int(row["created_at_ms"]):
            review = None
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
    # Unauthenticated liveness probe: no mode flags beyond paper_only (live state is on /api/status).
    live = live_trading_enabled()
    return {
        "ok": True,
        "paper_only": PAPER_ONLY and not live,
    }


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    """Prometheus scrape endpoint (unauthenticated; bind Tailscale/private only)."""
    ks = kill_switch_state(strict=False)
    set_gauge("jarvise_kill_switch", 1.0 if ks.get("engaged") else 0.0)
    pending = 0
    try:
        conn = open_db(db_path())
        try:
            pending = len(list_approvals(conn, status="pending", limit=500))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pending = -1
    set_gauge("jarvise_paper_queue_pending", float(pending))
    set_gauge("jarvise_live_trading", 1.0 if live_trading_enabled() else 0.0)
    body = render_prometheus()
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")


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


@app.get("/api/approvals/{approval_id}/recommendation")
def api_approval_recommendation(
    approval_id: str, _: None = Depends(require_auth)
) -> dict[str, Any]:
    card = load_recommendation(approval_id)
    if card is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return card


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
    ks = kill_switch_state()
    if not ks["known"]:
        # Fail closed: an unreadable switch must never let an approve through.
        detail = {
            "ok": False,
            "error": f"kill_switch unreadable ({ks.get('error') or 'redis'}); approve blocked",
            "kill_switch_state": ks,
            "id": id,
        }
        if wants_json(request):
            return JSONResponse(detail, status_code=503)
        return RedirectResponse("/", status_code=303)
    conn = open_db(db_path())
    try:
        result = approve_approval(conn, id, kill_switch=bool(ks["engaged"]))
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


class _AssetsBasicAuth(StaticFiles):
    """StaticFiles cannot take Depends(); enforce the same Basic Auth on bundled assets."""

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        user = os.environ.get("WEB_BASIC_AUTH_USER") or ""
        password = os.environ.get("WEB_BASIC_AUTH_PASSWORD") or ""
        if (user or password) and scope.get("type") == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
            if not _basic_header_ok(headers.get("authorization"), user, password):
                response = JSONResponse(
                    {"detail": "Auth required"},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    headers={"WWW-Authenticate": "Basic"},
                )
                await response(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


def _basic_header_ok(header: str | None, user: str, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    given_user, _, given_pass = raw.partition(":")
    return secrets.compare_digest(given_user, user) and secrets.compare_digest(given_pass, password)


_static = static_dir()
if _static is not None:
    assets = _static / "assets"
    if assets.is_dir():
        app.mount("/assets", _AssetsBasicAuth(directory=str(assets)), name="assets")
