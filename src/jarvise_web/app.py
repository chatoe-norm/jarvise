"""Private control + Command Dashboard API for Jarvise Docker stack."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from jarvise.model_routing import load_model_routing_matrix
from jarvise_analyze.explain import confidence_breakdown
from jarvise_analyze.suitability import evaluate_symbol_suitability
from jarvise_context.news_sources import load_report, news_home_view
from jarvise_exchange.binance_spot import BinanceSpotClient, resolve_binance_auth
from jarvise_exchange.sync import sync_spot_balances
from jarvise_exchange.value import value_spot_balances
from jarvise_ingest.db import (
    add_universe_symbol,
    count_analysis_output,
    ensure_paper_account,
    get_analysis_output,
    get_approval,
    get_latest_llm_review,
    get_paper_position,
    iter_analysis_outcome_facts,
    latest_macro_sentiment,
    list_all_known_symbols,
    list_analysis_outcomes,
    list_analysis_output,
    list_approvals,
    load_candle_at,
    load_latest_analysis,
    load_latest_candle,
    load_recent_ohlcv,
    open_db,
    remove_universe_symbol,
    reset_universe_members,
    universe_as_of,
)
from jarvise_ingest.health import BACKFILL_HINT, ingest_health
from jarvise_ingest.universe import PAPER_CORE, PAPER_LIQUID, preset_members, seed_named_universe
from jarvise_obs.metrics import render_prometheus, set_gauge
from jarvise_paper.approval import approve_approval, reject_approval
from jarvise_paper.auto_decide import load_auto_decide_config
from jarvise_paper.close import close_position
from jarvise_paper.feedback import (
    auto_ev_gate_status,
    list_owner_decisions,
    optional_owner_reason,
    record_owner_decision,
    require_owner_reason,
    reset_ev_gate_latch,
)
from jarvise_paper.ledger import load_paper_ledger
from jarvise_paper.metrics import compute_paper_metrics, persist_metrics_snapshot
from jarvise_paper.recommendation import build_recommendation, doctrine_query
from jarvise_risk import (
    HARD_MAX_RISK_PER_TRADE_PCT,
    RISK_CAP_KEYS,
    evaluate_from_db,
    get_paper_auto_decide_enabled,
    load_risk_caps,
    max_risk_per_trade_pct,
    set_max_risk_per_trade_pct,
    set_paper_auto_decide_enabled,
    set_risk_cap,
)
from jarvise_trade import live_trading_enabled
from jarvise_web.outcomes import derive_outcome, empty_outcome_summary, summarize_outcomes
from jarvise_web.schemas import ApprovalsResponse, DashboardResponse

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
RISK_MONITOR_KEY = "jarvise:risk_monitor:last"
JOBS_URL = os.environ.get("JARVISE_JOBS_URL", "http://jobs:8090")
DEFAULT_DB = Path("data/analytics/jarvise.db")

_PKG_DIR = Path(__file__).resolve().parent


def static_dir() -> Path | None:
    """Prefer a built SPA (has assets/), else any index.html fallback."""
    env_dist = os.environ.get("JARVISE_WEB_DIST")
    if env_dist:
        p = Path(env_dist)
        if (p / "index.html").is_file():
            return p
    candidates = [
        _PKG_DIR / "static",
        Path(__file__).resolve().parents[2] / "web" / "dist",
    ]
    found: Path | None = None
    for candidate in candidates:
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


def live_auth_gap() -> str | None:
    if not live_trading_enabled():
        return None
    user = os.environ.get("WEB_BASIC_AUTH_USER") or ""
    password = os.environ.get("WEB_BASIC_AUTH_PASSWORD") or ""
    if not user or not password:
        return "live_trading requires WEB_BASIC_AUTH_USER and WEB_BASIC_AUTH_PASSWORD"
    return None


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
                return {
                    "exists": False,
                    "points": 0,
                    "collection": COLLECTION,
                    "url": QDRANT_URL,
                    "status": "red",
                }
            resp.raise_for_status()
            result = resp.json().get("result") or {}
            points = result.get("points_count") or result.get("indexed_vectors_count") or 0
            raw_status = result.get("status") or ("green" if points > 0 else "yellow")
            return {
                "exists": True,
                "collection": COLLECTION,
                "url": QDRANT_URL,
                "points": points,
                "status": raw_status,
                "vectors_count": result.get("vectors_count") or points,
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "exists": False,
            "collection": COLLECTION,
            "url": QDRANT_URL,
            "points": 0,
            "status": "red",
            "error": str(exc),
        }


def _outcome_from_lookup(row: dict[str, Any], lookup: dict[str, dict]) -> dict[str, Any]:
    o = lookup.get(str(row.get("analysis_id") or "")) or {}
    approval = None
    if o.get("approval_id"):
        approval = {
            "id": o.get("approval_id"),
            "status": o.get("approval_status"),
            "resolve_reason": o.get("resolve_reason"),
            "resolved_at_ms": o.get("resolved_at_ms"),
        }
    return derive_outcome(
        row.get("action"),
        approval,
        o.get("fills") or 0,
        list(o.get("fill_reasons") or []),
    )


def load_analysis_rows(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int, str | None, dict[str, Any]]:
    path = db_path()
    empty = empty_outcome_summary()
    if not path.exists():
        return [], 0, f"Database not found: {path}", empty
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
            lookup = list_analysis_outcomes(
                conn,
                [str(row.get("analysis_id") or "") for row in rows],
            )
            for row in rows:
                row["outcome"] = _outcome_from_lookup(row, lookup)
            facts = iter_analysis_outcome_facts(
                conn,
                symbol=symbol or None,
                timeframe=timeframe or None,
            )
            summary = summarize_outcomes(facts)
        finally:
            conn.close()
        return rows, total, None, summary
    except Exception as exc:  # noqa: BLE001
        return [], 0, str(exc), empty


def load_paper_snapshot() -> tuple[dict[str, Any], str | None]:
    return load_paper_ledger(db_path())


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
    macro = None
    gate = None
    decisions: list[dict[str, Any]] = []
    path = db_path()
    if path.exists():
        conn = open_db(path)
        try:
            macro = latest_macro_sentiment(conn)
            gate = auto_ev_gate_status(conn)
            decisions = list_owner_decisions(conn, limit=5)
        finally:
            conn.close()
    return {
        "paper_only": PAPER_ONLY and not live,
        "live_trading": live,
        "auth_warning": live_auth_gap(),
        "kill_switch": bool(ks["engaged"]),
        "kill_switch_state": ks,
        "ingest": redis_get_json(INGEST_KEY),
        "rag": redis_get_json(RAG_KEY),
        "paper": redis_get_json(PAPER_KEY),
        "paper_expire": redis_get_json(PAPER_EXPIRE_KEY),
        "paper_auto": redis_get_json(PAPER_AUTO_KEY),
        "auto_ev_gate": gate,
        "owner_decisions": decisions,
        "auto_decide_config": {
            "enabled": get_paper_auto_decide_enabled(),
            "max_risk_per_trade_pct": max_risk_per_trade_pct(),
            "hard_max_risk_pct": HARD_MAX_RISK_PER_TRADE_PCT,
            "model": load_auto_decide_config().model,
        },
        "ingest_health": redis_get_json(INGEST_HEALTH_KEY),
        "risk_monitor": redis_get_json(RISK_MONITOR_KEY),
        "risk_caps": load_risk_caps().as_dict(),
        "qdrant": qdrant_info(),
        "fear_greed_index": None if not macro else macro.get("fear_greed_index"),
        "altcoin_season_index": None if not macro else macro.get("altcoin_season_index"),
        "btc_dominance_pct": None if not macro else macro.get("btc_dominance_pct"),
        "global_market_cap_usd": None if not macro else macro.get("global_market_cap_usd"),
        "sentiment_note": (None if macro and macro.get("fear_greed_index") is not None else "ไม่มีข้อมูล"),
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


_PCT_RISK_CAPS = frozenset(
    {"drawdown_lock_pct", "max_gross_notional_pct", "max_symbol_notional_pct", "max_correlated_bucket_pct"}
)


def validate_risk_cap_updates(updates: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    """Parse owner cap edits. Every cap must be a positive finite number; blanks are skipped."""
    parsed: dict[str, float] = {}
    errors: dict[str, str] = {}
    for k, v in updates.items():
        if k not in RISK_CAP_KEYS and k != "max_risk_per_trade_pct":
            continue
        if v is None or v == "":
            continue
        try:
            val = float(v)
        except (ValueError, TypeError):
            errors[k] = "must be a number"
            continue
        if not math.isfinite(val) or val <= 0:
            errors[k] = "must be greater than 0"
        elif k == "max_open_positions" and val != int(val):
            errors[k] = "must be a whole number"
        elif k == "max_risk_per_trade_pct" and val > 1.0:
            errors[k] = "must be at most 1.0"
        elif k in _PCT_RISK_CAPS and val > 100.0:
            errors[k] = "must be at most 100"
        else:
            parsed[k] = val
    return parsed, errors


def jobs_auth_headers() -> dict[str, str]:
    """Headers for web -> jobs calls. The jobs service only checks ``X-Jarvise-Token``."""
    token = os.environ.get("JARVISE_JOBS_TOKEN") or ""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}", "X-Jarvise-Token": token}


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
        timeframe = str(row["timeframe"])
        candle = load_latest_candle(conn, symbol, timeframe)
        analysis = get_analysis_output(conn, str(row["analysis_id"])) if row.get("analysis_id") else None
        signal_candle = None
        if analysis is not None and analysis.get("timestamp") is not None:
            signal_candle = load_candle_at(conn, symbol, timeframe, int(analysis["timestamp"]))
        account = ensure_paper_account(conn)
        position = get_paper_position(conn, symbol)
        safety = evaluate_from_db(conn, symbol).as_dict()
        ev_gate = auto_ev_gate_status(conn)
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
        signal_candle=signal_candle,
        news_report=load_report(),
        ev_gate=ev_gate,
    )


def load_analysis_explain(analysis_id: str) -> dict[str, Any] | None:
    """Confidence ladder for one analysis_output row (SPA Decisions expand)."""
    path = db_path()
    if not path.exists():
        return None
    conn = open_db(path)
    try:
        analysis = get_analysis_output(conn, analysis_id)
        if analysis is None:
            return None
        symbol = str(analysis["symbol"])
        timeframe = str(analysis["timeframe"])
        ts = int(analysis["timestamp"])
        candle = load_candle_at(conn, symbol, timeframe, ts)
    finally:
        conn.close()
    if candle is None:
        breakdown = confidence_breakdown({})
    else:
        breakdown = confidence_breakdown(candle)
    candle_out: dict[str, Any] | None = None
    if candle is not None:
        candle_out = {
            "close": candle.get("close"),
            "ema_20": candle.get("ema_20"),
            "ema_200": candle.get("ema_200"),
            "rsi_14": candle.get("rsi_14"),
            "atr_14": candle.get("atr_14"),
        }
    return {
        "ok": True,
        "analysis_id": analysis_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": ts,
        "candle": candle_out,
        "breakdown": breakdown,
    }


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
    priced_count = 0
    unpriced_count = 0
    if result.balances:
        try:
            valued = value_spot_balances(result.balances)
        except Exception as exc:  # noqa: BLE001
            logger.warning("exchange value soft-fail: %s", type(exc).__name__)
            valued = None
        if valued is None:
            unpriced_count = len(result.balances)
            for b in result.balances:
                balances_out.append(
                    {
                        "asset": b.asset,
                        "free": str(b.free),
                        "locked": str(b.locked),
                        "total": str(b.total),
                        "usd": None,
                        "pricing": None,
                    }
                )
        else:
            total_usd = float(valued.total_usd)
            priced_count = int(valued.priced_count)
            unpriced_count = int(valued.unpriced_count)
            for row in valued.rows:
                b = row.balance
                balances_out.append(
                    {
                        "asset": b.asset,
                        "free": str(b.free),
                        "locked": str(b.locked),
                        "total": str(b.total),
                        "usd": float(row.usd) if row.usd is not None else None,
                        "pricing": row.pricing,
                    }
                )
    return {
        "ok": True,
        "available": True,
        "venue": result.venue,
        "fetched_at_ms": result.fetched_at_ms,
        "balances": balances_out,
        "total_usd": total_usd,
        "priced_count": priced_count,
        "unpriced_count": unpriced_count,
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
    ks = kill_switch_state()
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
    rows, total, err, summary = load_analysis_rows(symbol=sym, timeframe=tf, limit=limit, offset=offset)
    payload: dict[str, Any] = {
        "paper_only": PAPER_ONLY,
        "db": str(db_path()),
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": summary,
    }
    if err:
        payload["ok"] = False
        payload["error"] = err
    else:
        payload["ok"] = True
    return payload


@app.get("/api/analysis/{analysis_id}/explain")
def api_analysis_explain(analysis_id: str, _: None = Depends(require_auth)) -> dict[str, Any]:
    payload = load_analysis_explain(analysis_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="analysis not found")
    return payload


@app.get("/api/paper")
def api_paper(_: None = Depends(require_auth)) -> dict[str, Any]:
    ledger, err = load_paper_snapshot()
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


@app.post("/api/paper/ev-gate/reset")
async def api_paper_ev_gate_reset(
    request: Request,
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    """Clear the EV-gate latch. The body must include confirm=true."""
    confirmed = False
    reason = ""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        if isinstance(body, dict):
            confirmed = body.get("confirm") is True
            reason = str(body.get("reason") or "")
    if not confirmed:
        raise HTTPException(status_code=400, detail="confirm=true is required")
    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    conn = open_db(path)
    try:
        try:
            result = reset_ev_gate_latch(conn, reason=reason)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        gate = auto_ev_gate_status(conn)
    finally:
        conn.close()
    return {"ok": True, **result, "auto_ev_gate": gate}


@app.get("/api/approvals", response_model=ApprovalsResponse)
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
def api_approval_recommendation(approval_id: str, _: None = Depends(require_auth)) -> dict[str, Any]:
    card = load_recommendation(approval_id)
    if card is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return card


@app.get("/api/approvals/{approval_id}/ohlcv")
def api_approval_ohlcv(
    approval_id: str,
    _: None = Depends(require_auth),
    limit: int = Query(48, ge=1, le=120),
) -> dict[str, Any]:
    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="approval not found")
    conn = open_db(path)
    try:
        row = get_approval(conn, approval_id)
        if row is None:
            raise HTTPException(status_code=404, detail="approval not found")
        symbol = str(row["symbol"])
        timeframe = str(row["timeframe"])
        action = str(row.get("action") or "flat")
        raw = load_recent_ohlcv(conn, symbol, timeframe, limit=limit)
        analysis_id = row.get("analysis_id")
        analysis = get_analysis_output(conn, str(analysis_id)) if analysis_id else None
        inv = (analysis or {}).get("invalidation_price")
        invalidation = float(inv) if inv is not None else None
        signal_raw = (analysis or {}).get("timestamp")
        signal_ts = int(signal_raw) if signal_raw is not None else None
    finally:
        conn.close()

    def _stored(value: Any) -> float | None:
        return None if value is None else float(value)

    bars = [
        {
            "t": int(b["timestamp"]),
            "o": float(b["open"]),
            "h": float(b["high"]),
            "l": float(b["low"]),
            "c": float(b["close"]),
            "ema_20": _stored(b.get("ema_20")),
            "ema_200": _stored(b.get("ema_200")),
        }
        for b in raw
    ]
    return {
        "ok": True,
        "approval_id": approval_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": bars,
        "invalidation_price": invalidation,
        "signal_ts": signal_ts,
        "action": action,
    }


@app.get("/api/exchange")
def api_exchange(_: None = Depends(require_auth)) -> dict[str, Any]:
    return exchange_payload()


@app.get("/api/dashboard", response_model=DashboardResponse)
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
        "news": news_home_view(load_report()),
    }


def active_universe_id() -> str:
    return os.environ.get("JARVISE_PAPER_UNIVERSE") or "paper_core"


@app.get("/api/universe")
def api_universe(_: None = Depends(require_auth)) -> dict[str, Any]:
    path = db_path()
    universe_id = active_universe_id()
    timeframe = os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
    if not path.exists():
        return {
            "ok": True,
            "universe_id": universe_id,
            "timeframe": timeframe,
            "active_symbols": [],
            "items": [],
            "backfill_hint": "",
        }
    conn = open_db(path)
    try:
        seed_named_universe(conn, universe_id)
        now_ms = int(time.time() * 1000)
        active_symbols = universe_as_of(conn, universe_id, now_ms)
        known_symbols = list_all_known_symbols(conn)

        all_syms: list[str] = list(active_symbols)
        for s in known_symbols:
            if s not in all_syms:
                all_syms.append(s)

        health_report = ingest_health(conn, all_syms, timeframe)
        health_by_sym = health_report.get("symbols") or {}

        items: list[dict[str, Any]] = []
        for sym in all_syms:
            analysis = load_latest_analysis(conn, sym, timeframe)
            candle = load_latest_candle(conn, sym, timeframe)
            h = health_by_sym.get(sym) or {}
            pos = get_paper_position(conn, sym)
            suit = evaluate_symbol_suitability(
                sym,
                analysis=analysis,
                candle=candle,
                health=h,
                position=pos,
            )
            suit["is_active"] = sym in active_symbols
            items.append(suit)

        hint = BACKFILL_HINT.format(symbols=",".join(active_symbols), timeframe=timeframe)
        return {
            "ok": True,
            "universe_id": universe_id,
            "timeframe": timeframe,
            "active_symbols": active_symbols,
            "items": items,
            "backfill_hint": hint,
        }
    finally:
        conn.close()


SyncReport = dict[str, list[Any]]


def _ingest_argv(sym: str, timeframe: str, path: Path, *, since: str | None, skip_book: bool) -> list[str]:
    argv = ["--symbol", sym, "--timeframe", timeframe]
    argv += ["--since", since] if since else ["--limit", "200"]
    argv += ["--skip-derivatives"]
    if skip_book:
        argv += ["--skip-book"]
    return argv + ["--db", str(path)]


def _run_symbol_sync(
    sym: str,
    steps: list[tuple[str, Any, list[str]]],
    path: Path,
    timeframe: str,
    report: SyncReport,
) -> None:
    """Run one symbol's ingest/analyze steps and file it under synced / warnings / failed.

    ingest exits 1 on any provider error (macro, book) even when candles were written, so
    exit 1 is a warning. An exception, exit 2 (bad arguments) or no candles afterwards is a
    failure: the owner must not be told a backfill succeeded when it didn't.
    """
    from jarvise_ingest.db import count_market, open_db

    for label, fn, argv in steps:
        try:
            code = fn(argv)
        except Exception as exc:  # noqa: BLE001
            report["failed"].append(
                {"symbol": sym, "step": label, "error": f"{type(exc).__name__}: {exc}"[:300]}
            )
            return
        if code == 1:
            report["warnings"].append({"symbol": sym, "step": label, "exit_code": 1})
        elif code not in (0, None):
            report["failed"].append({"symbol": sym, "step": label, "exit_code": code})
            return
    conn = open_db(path)
    try:
        candles = count_market(conn, sym, timeframe)
    finally:
        conn.close()
    if candles <= 0:
        report["failed"].append({"symbol": sym, "step": f"ingest {timeframe}", "error": "no candles stored"})
        return
    report["synced"].append(sym)


def _sync_market_symbols(symbols: list[str], path: Path, timeframe: str, htf: str) -> SyncReport:
    from jarvise.charts import export_candle_graphs
    from jarvise_analyze.cli import main as analyze_main
    from jarvise_ingest.cli import main as ingest_main
    from jarvise_ingest.db import count_market, open_db

    report: SyncReport = {"synced": [], "warnings": [], "failed": []}
    for sym in symbols:
        c = open_db(path)
        try:
            # Under 200 bars: full history from 2021 so EMA200 warm-up is complete.
            since = "2021-01-01" if count_market(c, sym, timeframe) < 200 else None
        finally:
            c.close()
        steps: list[tuple[str, Any, list[str]]] = [
            (
                f"ingest {timeframe}",
                ingest_main,
                _ingest_argv(sym, timeframe, path, since=since, skip_book=since is not None),
            ),
            (f"ingest {htf}", ingest_main, _ingest_argv(sym, htf, path, since=since, skip_book=True)),
            ("ingest 1h", ingest_main, _ingest_argv(sym, "1h", path, since=None, skip_book=False)),
            (
                "analyze",
                analyze_main,
                ["--symbol", sym, "--timeframe", timeframe, "--db", str(path), "--json"],
            ),
        ]
        _run_symbol_sync(sym, steps, path, timeframe, report)

    try:
        export_candle_graphs(path, symbols, ["1h", timeframe])
    except Exception:
        pass

    return report


@app.post("/api/universe/add")
async def api_universe_add(
    request: Request,
    symbol: str = Form(...),
    asset_class: str = Form("crypto"),
    _: None = Depends(require_auth),
) -> Any:
    sym = symbol.strip().upper()
    if not sym or len(sym) < 3 or not sym.isalnum():
        raise HTTPException(status_code=400, detail="Invalid symbol format")
    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    universe_id = active_universe_id()
    timeframe = os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
    htf = (os.environ.get("JARVISE_ANALYZE_HTF") or "1d").strip() or "1d"
    conn = open_db(path)
    try:
        add_universe_symbol(conn, universe_id=universe_id, symbol=sym, asset_class=asset_class)
        now_ms = int(time.time() * 1000)
        active_symbols = universe_as_of(conn, universe_id, now_ms)
    finally:
        conn.close()

    # Automatically backfill and analyze the newly added coin so data is immediately complete
    sync: SyncReport = {"synced": [], "warnings": [], "failed": []}
    try:
        sync = await asyncio.to_thread(_sync_market_symbols, [sym], path, timeframe, htf)
    except Exception as exc:
        logger.warning("on-demand sync for %s soft-failed: %s", sym, exc)
        sync["failed"].append({"symbol": sym, "step": "sync", "error": type(exc).__name__})

    if wants_json(request):
        # The symbol is in the universe either way; sync_failed says its data is not ready yet.
        return JSONResponse(
            {
                "ok": True,
                "symbol": sym,
                "universe_id": universe_id,
                "active_symbols": active_symbols,
                "sync_failed": sync["failed"],
                "sync_warnings": sync["warnings"],
            }
        )
    return RedirectResponse("/ops", status_code=303)


@app.post("/api/universe/sync")
async def api_universe_sync(
    request: Request,
    symbol: str | None = Form(None),
    _: None = Depends(require_auth),
) -> Any:
    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    universe_id = active_universe_id()
    timeframe = os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
    htf = (os.environ.get("JARVISE_ANALYZE_HTF") or "1d").strip() or "1d"

    conn = open_db(path)
    try:
        now_ms = int(time.time() * 1000)
        active_syms = universe_as_of(conn, universe_id, now_ms)
    finally:
        conn.close()

    if symbol and symbol.strip():
        target_syms = [symbol.strip().upper()]
    else:
        target_syms = active_syms or ["BTCUSDT", "ETHUSDT"]

    report = await asyncio.to_thread(_sync_market_symbols, target_syms, path, timeframe, htf)

    if wants_json(request):
        return JSONResponse(
            {
                "ok": not report["failed"],
                "synced_symbols": report["synced"],
                "count": len(report["synced"]),
                "failed": report["failed"],
                "warnings": report["warnings"],
                "timeframe": timeframe,
            }
        )
    return RedirectResponse("/ops", status_code=303)


@app.post("/api/rag/sync")
async def api_rag_sync(
    request: Request,
    _: None = Depends(require_auth),
) -> Any:
    jobs_url = os.environ.get("JARVISE_JOBS_URL") or "http://jobs:8090"
    headers = jobs_auth_headers()
    data: dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{jobs_url}/jobs/rag-refresh", headers=headers)
            if resp.status_code in (200, 202):
                data = resp.json()
            else:
                data = {"ok": False, "status_code": resp.status_code, "error": resp.text[:500]}
    except Exception as exc:
        try:
            from jarvise import rag as ragmod

            nb_res = await asyncio.to_thread(ragmod.sync_notebook)
            idx_res = await asyncio.to_thread(ragmod.index_sources)
            data = {
                "ok": bool(idx_res.get("ok")),
                "notebook": nb_res,
                "index": idx_res,
                "profile": "chatoe",
            }
        except Exception as local_exc:
            data = {"ok": False, "error": f"jobs failed ({exc}); local fallback failed ({local_exc})"}

    data["notebook_id"] = "14e11c63-e2ee-4b49-898f-b0cc4c61cb4e"
    data["url"] = "https://notebook.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e"
    data["profile"] = "chatoe"

    if wants_json(request):
        return JSONResponse(data)
    return RedirectResponse("/ops", status_code=303)


@app.get("/api/doctrine/query")
async def api_doctrine_query(
    request: Request,
    q: str = Query("pullback EMA20"),
    limit: int = Query(3, ge=1, le=10),
    _: None = Depends(require_auth),
) -> Any:
    jobs_url = os.environ.get("JARVISE_JOBS_URL") or "http://jobs:8090"
    token = os.environ.get("JARVISE_JOBS_TOKEN") or ""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Jarvise-Token"] = token
    data: dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{jobs_url}/doctrine",
                params={"q": q, "limit": limit},
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
            else:
                data = {
                    "ok": False,
                    "query": q,
                    "hits": [],
                    "error": f"jobs returned status {resp.status_code}",
                }
    except Exception as exc:
        try:
            from jarvise.rag import doctrine_snippets

            hits = await asyncio.to_thread(doctrine_snippets, q, limit=limit)
            data = {"ok": True, "query": q, "hits": hits, "paper_only": True}
        except Exception as local_exc:
            data = {
                "ok": False,
                "query": q,
                "hits": [],
                "error": f"jobs failed ({exc}); local fallback failed ({local_exc})",
            }

    return JSONResponse(data)


@app.post("/api/universe/remove")
async def api_universe_remove(
    request: Request,
    symbol: str = Form(...),
    _: None = Depends(require_auth),
) -> Any:
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="Symbol required")
    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    universe_id = active_universe_id()
    conn = open_db(path)
    try:
        remove_universe_symbol(conn, universe_id=universe_id, symbol=sym)
        now_ms = int(time.time() * 1000)
        active_symbols = universe_as_of(conn, universe_id, now_ms)
    finally:
        conn.close()
    if wants_json(request):
        return JSONResponse(
            {"ok": True, "symbol": sym, "universe_id": universe_id, "active_symbols": active_symbols}
        )
    return RedirectResponse("/ops", status_code=303)


@app.post("/api/universe/reset")
async def api_universe_reset(
    request: Request,
    preset: str = Form("paper_liquid"),
    _: None = Depends(require_auth),
) -> Any:
    preset = preset.strip().lower()
    if preset not in (PAPER_CORE, PAPER_LIQUID):
        raise HTTPException(status_code=400, detail=f"Unsupported preset {preset}")
    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    universe_id = active_universe_id()
    conn = open_db(path)
    try:
        # Reset the universe the jobs actually trade, not the preset's own universe id.
        seed_named_universe(conn, universe_id)
        now_ms = int(time.time() * 1000)
        changes = reset_universe_members(
            conn, universe_id=universe_id, members=preset_members(preset), now_ms=now_ms
        )
        active_symbols = universe_as_of(conn, universe_id, now_ms)
    finally:
        conn.close()
    if wants_json(request):
        return JSONResponse(
            {
                "ok": True,
                "preset": preset,
                "universe_id": universe_id,
                "active_symbols": active_symbols,
                "removed": changes["removed"],
                "added": changes["added"],
            }
        )
    return RedirectResponse("/ops", status_code=303)


@app.get("/api/paper/auto-decide/config")
async def api_paper_auto_decide_get_config(_: None = Depends(require_auth)) -> dict[str, Any]:
    cfg = load_auto_decide_config()
    return {
        "ok": True,
        "enabled": get_paper_auto_decide_enabled(),
        "max_risk_per_trade_pct": max_risk_per_trade_pct(),
        "hard_max_risk_pct": HARD_MAX_RISK_PER_TRADE_PCT,
        "model": cfg.model,
    }


@app.get("/api/openclaw/model-routing")
async def api_openclaw_model_routing(_: None = Depends(require_auth)) -> Any:
    """Returns the OpenRouter Multi-Tier Model Routing Matrix for all autonomous agents."""
    return JSONResponse(load_model_routing_matrix())


@app.post("/api/paper/auto-decide/config")
async def api_paper_auto_decide_save_config(
    request: Request,
    _: None = Depends(require_auth),
) -> Any:
    enabled_val: bool | None = None
    risk_val: float | None = None
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        if isinstance(body, dict):
            if "enabled" in body:
                enabled_val = bool(body["enabled"])
            if "max_risk_per_trade_pct" in body and body["max_risk_per_trade_pct"] is not None:
                try:
                    risk_val = float(body["max_risk_per_trade_pct"])
                except (ValueError, TypeError):
                    pass
    else:
        form = await request.form()
        if "enabled" in form:
            enabled_val = str(form["enabled"]).lower() in {"1", "true", "on"}
        if "max_risk_per_trade_pct" in form:
            try:
                risk_val = float(str(form["max_risk_per_trade_pct"]))
            except (ValueError, TypeError):
                pass

    if enabled_val is not None:
        set_paper_auto_decide_enabled(enabled_val)
    if risk_val is not None:
        set_max_risk_per_trade_pct(risk_val)

    data = {
        "ok": True,
        "enabled": get_paper_auto_decide_enabled(),
        "max_risk_per_trade_pct": max_risk_per_trade_pct(),
        "hard_max_risk_pct": HARD_MAX_RISK_PER_TRADE_PCT,
    }
    if wants_json(request):
        return JSONResponse(data)
    return RedirectResponse("/ops", status_code=303)


@app.post("/api/paper/auto-decide/run")
async def api_paper_auto_decide_run(
    request: Request,
    _: None = Depends(require_auth),
) -> Any:
    jobs_url = os.environ.get("JARVISE_JOBS_URL") or "http://jobs:8090"
    headers = jobs_auth_headers()
    data: dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{jobs_url}/jobs/paper-auto-decide", headers=headers)
            if resp.status_code in (200, 202, 409):
                data = resp.json()
            else:
                data = {"ok": False, "status_code": resp.status_code, "error": resp.text[:500]}
    except Exception as exc:
        try:
            from jarvise.jobs import run_paper_auto_decide

            code, payload = await asyncio.to_thread(run_paper_auto_decide)
            data = payload
        except Exception as local_exc:
            data = {"ok": False, "error": f"jobs failed ({exc}); local fallback failed ({local_exc})"}

    if wants_json(request):
        return JSONResponse(data)
    return RedirectResponse("/ops", status_code=303)


@app.get("/api/risk/caps")
def api_risk_caps_get(_: None = Depends(require_auth)) -> dict[str, Any]:
    return {"ok": True, "risk_caps": load_risk_caps().as_dict()}


@app.post("/api/risk/caps")
async def api_risk_caps_save(
    request: Request,
    _: None = Depends(require_auth),
) -> Any:
    updates: dict[str, Any] = {}
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        if isinstance(body, dict):
            updates = body
    else:
        form = await request.form()
        updates = dict(form)

    parsed, field_errors = validate_risk_cap_updates(updates)
    if field_errors:
        # Apply nothing: a half-saved cap set (e.g. daily loss 0) can halt trading.
        data = {
            "ok": False,
            "error": "invalid risk caps",
            "field_errors": field_errors,
            "risk_caps": load_risk_caps().as_dict(),
        }
        if wants_json(request):
            return JSONResponse(data, status_code=400)
        raise HTTPException(status_code=400, detail=field_errors)
    for k, val in parsed.items():
        set_risk_cap(k, val)

    data = {"ok": True, "risk_caps": load_risk_caps().as_dict()}
    if wants_json(request):
        return JSONResponse(data)
    return RedirectResponse("/ops", status_code=303)


@app.post("/api/pipeline/run")
async def api_pipeline_run(
    request: Request,
    _: None = Depends(require_auth),
) -> Any:
    job_name = ""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        if isinstance(body, dict):
            job_name = str(body.get("job") or "").strip()
    else:
        form = await request.form()
        job_name = str(form.get("job") or "").strip()

    job_route_map = {
        "ingest": "/jobs/ingest",
        "rag": "/jobs/rag-refresh",
        "paper_run": "/jobs/paper-run",
        "paper_expire": "/jobs/paper-expire",
        "risk_monitor": "/jobs/risk-monitor",
        "ingest_health": "/jobs/ingest-health",
        "paper_auto_decide": "/jobs/paper-auto-decide",
    }
    endpoint = job_route_map.get(job_name)
    if not endpoint:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown pipeline job: '{job_name}'. Valid jobs: {list(job_route_map.keys())}",
        )

    jobs_url = os.environ.get("JARVISE_JOBS_URL") or "http://jobs:8090"
    token = os.environ.get("JARVISE_JOBS_TOKEN") or ""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Jarvise-Token"] = token

    data: dict[str, Any]
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{jobs_url}{endpoint}", headers=headers)
            if resp.status_code in (200, 202, 409):
                data = resp.json()
            else:
                data = {"ok": False, "status_code": resp.status_code, "error": resp.text[:500]}
    except Exception as exc:
        try:
            from jarvise import jobs as jmod

            runners = {
                "ingest": jmod.run_ingest,
                "rag": jmod.run_rag_refresh,
                "paper_run": jmod.run_paper_run,
                "paper_expire": jmod.run_paper_expire,
                "risk_monitor": jmod.run_risk_monitor,
                "ingest_health": jmod.run_ingest_health,
                "paper_auto_decide": jmod.run_paper_auto_decide,
            }
            runner = runners.get(job_name)
            if runner:
                code, payload = await asyncio.to_thread(runner)
                data = payload
                data["local_fallback"] = True
            else:
                data = {"ok": False, "error": f"No runner for job {job_name}"}
        except Exception as local_exc:
            data = {"ok": False, "error": f"jobs failed ({exc}); local fallback failed ({local_exc})"}

    duration_s = round(time.time() - started, 2)
    response_payload = {
        "ok": bool(data.get("ok", False) or data.get("skipped")),
        "job": job_name,
        "duration_s": duration_s,
        "result": data,
        "executed_at": int(time.time() * 1000),
    }
    if wants_json(request):
        return JSONResponse(response_payload)
    return RedirectResponse("/ops", status_code=303)


@app.post("/api/ingest/health/run")
async def api_ingest_health_run(
    request: Request,
    _: None = Depends(require_auth),
) -> Any:
    jobs_url = os.environ.get("JARVISE_JOBS_URL") or "http://jobs:8090"
    token = os.environ.get("JARVISE_JOBS_TOKEN") or ""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Jarvise-Token"] = token
    data: dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{jobs_url}/jobs/ingest-health", headers=headers)
            if resp.status_code in (200, 202, 500):
                data = resp.json()
            else:
                data = {"ok": False, "status_code": resp.status_code, "error": resp.text[:500]}
    except Exception as exc:
        try:
            from jarvise.jobs import run_ingest_health

            code, payload = await asyncio.to_thread(run_ingest_health)
            data = payload
        except Exception as local_exc:
            data = {"ok": False, "error": f"jobs failed ({exc}); local fallback failed ({local_exc})"}

    if wants_json(request):
        return JSONResponse(data)
    return RedirectResponse("/ops", status_code=303)


@app.post("/api/ingest/backfill")
async def api_ingest_backfill(
    request: Request,
    _: None = Depends(require_auth),
) -> Any:
    symbols: list[str] = []
    timeframe = ""
    since = ""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        if isinstance(body, dict):
            raw_syms = body.get("symbols")
            if isinstance(raw_syms, list):
                symbols = [str(s).strip().upper() for s in raw_syms if str(s).strip()]
            elif isinstance(raw_syms, str) and raw_syms.strip():
                symbols = [s.strip().upper() for s in raw_syms.split(",") if s.strip()]
            timeframe = str(body.get("timeframe") or "").strip()
            since = str(body.get("since") or "").strip()
    else:
        form = await request.form()
        raw_syms = form.get("symbols")
        if raw_syms:
            symbols = [s.strip().upper() for s in str(raw_syms).split(",") if s.strip()]
        timeframe = str(form.get("timeframe") or "").strip()
        since = str(form.get("since") or "").strip()

    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    tf = timeframe or os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
    htf = (os.environ.get("JARVISE_ANALYZE_HTF") or "1d").strip() or "1d"
    since_date = since or "2021-01-01"

    if not symbols:
        universe_id = active_universe_id()
        conn = open_db(path)
        try:
            now_ms = int(time.time() * 1000)
            symbols = universe_as_of(conn, universe_id, now_ms)
        finally:
            conn.close()

    if not symbols:
        symbols = ["BTCUSDT", "ETHUSDT"]

    def _do_backfill(
        sym_list: list[str], db_p: Path, t_frame: str, htf_frame: str, since_d: str
    ) -> SyncReport:
        from jarvise.charts import export_candle_graphs
        from jarvise_analyze.cli import main as analyze_main
        from jarvise_ingest.cli import main as ingest_main

        report: SyncReport = {"synced": [], "warnings": [], "failed": []}
        for sym in sym_list:
            steps: list[tuple[str, Any, list[str]]] = [
                (
                    f"ingest {t_frame}",
                    ingest_main,
                    _ingest_argv(sym, t_frame, db_p, since=since_d, skip_book=True),
                ),
                (
                    f"ingest {htf_frame}",
                    ingest_main,
                    _ingest_argv(sym, htf_frame, db_p, since=since_d, skip_book=True),
                ),
                ("ingest 1h", ingest_main, _ingest_argv(sym, "1h", db_p, since=None, skip_book=False)),
                (
                    "analyze",
                    analyze_main,
                    ["--symbol", sym, "--timeframe", t_frame, "--db", str(db_p), "--json"],
                ),
            ]
            _run_symbol_sync(sym, steps, db_p, t_frame, report)

        try:
            export_candle_graphs(db_p, sym_list, ["1h", t_frame])
        except Exception:
            pass
        return report

    report = await asyncio.to_thread(_do_backfill, symbols, path, tf, htf, since_date)
    response_payload = {
        "ok": not report["failed"],
        "synced_symbols": report["synced"],
        "count": len(report["synced"]),
        "failed": report["failed"],
        "warnings": report["warnings"],
        "timeframe": tf,
        "since": since_date,
    }
    if wants_json(request):
        return JSONResponse(response_payload)
    return RedirectResponse("/ops", status_code=303)


@app.get("/api/observability/preview")
def api_observability_preview(
    request: Request,
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    host = request.headers.get("host", "127.0.0.1").split(":")[0]
    ledger, _err = load_paper_snapshot()
    acct = ledger.get("account") or {}
    positions = ledger.get("positions") or []

    path = db_path()
    pending_count = 0
    if path.exists():
        conn = open_db(path)
        try:
            pending_count = len(list_approvals(conn, status="pending", limit=500))
        finally:
            conn.close()

    ks = kill_switch_state()
    return {
        "ok": True,
        "gauges": {
            "kill_switch": bool(ks.get("engaged")),
            "kill_switch_state": ks,
            "paper_equity": acct.get("equity", 10000.0),
            "paper_cash": acct.get("cash", 10000.0),
            "open_positions_count": len(positions),
            "pending_approvals_count": pending_count,
            "live_trading": live_trading_enabled(),
        },
        "job_runs": {
            "ingest": redis_get_json(INGEST_KEY),
            "rag": redis_get_json(RAG_KEY),
            "paper": redis_get_json(PAPER_KEY),
            "paper_expire": redis_get_json(PAPER_EXPIRE_KEY),
            "paper_auto": redis_get_json(PAPER_AUTO_KEY),
            "ingest_health": redis_get_json(INGEST_HEALTH_KEY),
            "risk_monitor": redis_get_json(RISK_MONITOR_KEY),
        },
        "metrics_summary": load_metrics(),
        "endpoints": {
            "grafana": f"http://{host}:3000",
            "prometheus": f"http://{host}:9090",
            "web_metrics": "/metrics",
        },
    }


@app.post("/api/observability/persist")
async def api_observability_persist(
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
    return RedirectResponse("/ops", status_code=303)


@app.post("/kill-switch")
async def set_kill_switch(
    request: Request,
    state: str = Form(...),
    reason: str = Form(""),
    _: None = Depends(require_auth),
) -> Any:
    gap = live_auth_gap()
    if gap:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": gap}, status_code=403)
        raise HTTPException(status_code=403, detail=gap)
    value = "1" if state.lower() in {"on", "1", "true", "engage"} else "0"
    clearing = value == "0"
    note = optional_owner_reason(reason)
    if clearing:
        try:
            note = require_owner_reason(reason)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    kind = "kill_switch_off" if clearing else "kill_switch_on"
    try:
        _redis().set(KILL_SWITCH_KEY, value)
    except Exception as exc:  # noqa: BLE001
        _log_owner_kill_switch(
            kind=kind,
            reason=note,
            payload={"ok": False, "error": type(exc).__name__},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _log_owner_kill_switch(kind=kind, reason=note, payload={"ok": True})
    if wants_json(request):
        return JSONResponse({"ok": True, "kill_switch": value == "1"})
    return RedirectResponse("/", status_code=303)


def _log_owner_kill_switch(*, kind: str, reason: str | None, payload: dict[str, Any]) -> None:
    """Record the Redis result. A log failure never changes the HTTP result."""
    path = db_path()
    try:
        conn = open_db(path)
        try:
            record_owner_decision(
                conn,
                at_ms=int(time.time() * 1000),
                kind=kind,
                reason=reason,
                actor="owner",
                payload=payload,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner kill-switch log soft-fail: %s", type(exc).__name__)


@app.post("/approvals/approve")
async def approvals_approve(
    request: Request,
    id: str = Form(...),
    _: None = Depends(require_auth),
) -> Any:
    gap = live_auth_gap()
    if gap:
        detail = {"ok": False, "error": gap, "id": id}
        if wants_json(request):
            return JSONResponse(detail, status_code=403)
        raise HTTPException(status_code=403, detail=gap)
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
        payload = result if isinstance(result, dict) else {"id": id}
        ok = bool(payload.get("ok", True))
        body = {"ok": ok, "error": None if ok else payload.get("error"), "result": payload}
        # 409 so the dashboard can never show a refused approve as a success.
        return JSONResponse(body, status_code=200 if ok else 409)
    return RedirectResponse("/", status_code=303)


@app.post("/approvals/reject")
async def approvals_reject(
    request: Request,
    id: str = Form(...),
    _: None = Depends(require_auth),
) -> Any:
    conn = open_db(db_path())
    try:
        result = reject_approval(conn, id, reason="ui")
    finally:
        conn.close()
    if wants_json(request):
        ok = bool(result.get("ok"))
        body = {"ok": ok, "id": id, "error": None if ok else result.get("error")}
        return JSONResponse(body, status_code=200 if ok else 409)
    return RedirectResponse("/", status_code=303)


@app.post("/paper/close")
async def paper_close(
    request: Request,
    symbol: str = Form(...),
    _: None = Depends(require_auth),
) -> Any:
    if live_trading_enabled():
        detail = {"ok": False, "error": "live_trading_enabled", "symbol": symbol}
        if wants_json(request):
            return JSONResponse(detail, status_code=409)
        raise HTTPException(status_code=409, detail="live_trading_enabled")
    path = db_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="no database")
    conn = open_db(path)
    try:
        result = close_position(conn, symbol)
    finally:
        conn.close()
    status_code = 200 if result.get("ok") else 400
    if wants_json(request):
        return JSONResponse(result, status_code=status_code)
    return RedirectResponse("/paper", status_code=303)


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


@app.get("/manifest.webmanifest")
@app.get("/manifest.json")
def manifest() -> FileResponse:
    root = static_dir()
    if root and (root / "manifest.webmanifest").is_file():
        return FileResponse(root / "manifest.webmanifest", media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="manifest not found")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    root = static_dir()
    if root and (root / "sw.js").is_file():
        # no-cache so a changed worker (new cache strategy) reaches browsers on the next visit.
        return FileResponse(
            root / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )
    raise HTTPException(status_code=404, detail="sw.js not found")


@app.get("/icon.svg")
def icon_svg() -> FileResponse:
    root = static_dir()
    if root and (root / "icon.svg").is_file():
        return FileResponse(root / "icon.svg", media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="icon.svg not found")


@app.get("/icon-192.png")
def icon_192() -> FileResponse:
    root = static_dir()
    if root and (root / "icon-192.png").is_file():
        return FileResponse(root / "icon-192.png", media_type="image/png")
    raise HTTPException(status_code=404, detail="icon-192.png not found")


@app.get("/icon-512.png")
def icon_512() -> FileResponse:
    root = static_dir()
    if root and (root / "icon-512.png").is_file():
        return FileResponse(root / "icon-512.png", media_type="image/png")
    raise HTTPException(status_code=404, detail="icon-512.png not found")


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
