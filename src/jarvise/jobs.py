"""Internal job HTTP API for n8n. Docker network only. Paper only. No order placement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from jarvise.rag import doctrine_snippets, publish_redis_status
from jarvise_ingest.db import list_approvals, open_db
from jarvise_ingest.health import ingest_health
from jarvise_notify import notify_auto_decide, notify_ingest_health, notify_pending_digest
from jarvise_paper.auto_decide import run_auto_decide


def kill_switch_engaged() -> bool:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return False
    try:
        import redis
    except ImportError:
        return False
    value = redis.Redis.from_url(redis_url, decode_responses=True).get("jarvise:kill_switch")
    return value in {"1", "true", "on", "yes"}


def _skipped() -> dict[str, Any]:
    return {
        "ok": False,
        "skipped": True,
        "reason": "kill_switch engaged",
        "paper_only": True,
        "at_ms": int(time.time() * 1000),
    }


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _publish_best_effort(key: str, payload: dict[str, Any]) -> None:
    try:
        publish_redis_status(key, payload)
    except Exception:  # noqa: BLE001 — Redis down must not turn a 409/payload into a dropped connection
        return


_AUTO_DECIDE_LOCK = threading.Lock()


def run_ingest() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        publish_redis_status("jarvise:ingest:last", payload)
        return 3, payload
    symbols = os.environ.get("JARVISE_INGEST_SYMBOLS", "BTCUSDT,ETHUSDT")
    paper_tf = os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
    timeframes = ["1h"]
    if paper_tf != "1h":
        timeframes.append(paper_tf)

    by_tf: dict[str, Any] = {}
    exit_code = 0
    for tf in timeframes:
        cmd = [
            sys.executable,
            "-m",
            "jarvise",
            "ingest",
            "--symbol",
            symbols,
            "--timeframe",
            tf,
            "--limit",
            "200",
            "--json",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        by_tf[tf] = _parse_stdout(proc)
        if proc.returncode != 0:
            exit_code = proc.returncode

    payload: dict[str, Any] = {
        "ok": exit_code == 0,
        "paper_only": True,
        "exit_code": exit_code,
        "timeframes": by_tf,
    }
    # Prefer primary 1h shape at top-level for older Pipeline readers when single-tf.
    if "1h" in by_tf and isinstance(by_tf["1h"], dict):
        for key in ("db", "market_technicals", "errors", "duration_s"):
            if key in by_tf["1h"]:
                payload[key] = by_tf["1h"][key]
    publish_redis_status("jarvise:ingest:last", payload)
    return exit_code, payload


def run_rag_refresh() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        publish_redis_status("jarvise:rag:last", payload)
        return 3, payload
    cmd = [sys.executable, "-m", "jarvise", "rag", "refresh", "--output", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    payload = _parse_stdout(proc)
    payload["paper_only"] = True
    payload["exit_code"] = proc.returncode
    # CLI also publishes; mirror ingest so Redis stays JSON even if CLI publish is skipped.
    publish_redis_status("jarvise:rag:last", payload)
    return proc.returncode, payload


def run_paper_run() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        publish_redis_status("jarvise:paper:last", payload)
        return 3, payload
    universe = os.environ.get("JARVISE_PAPER_UNIVERSE") or "paper_core"
    timeframe = os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
    cmd = [
        sys.executable,
        "-m",
        "jarvise",
        "paper",
        "run",
        "--universe",
        universe,
        "--timeframe",
        timeframe,
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    payload = _parse_stdout(proc)
    payload["paper_only"] = True
    payload["exit_code"] = proc.returncode
    publish_redis_status("jarvise:paper:last", payload)
    return proc.returncode, payload


def run_paper_expire() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        publish_redis_status("jarvise:paper:expire:last", payload)
        return 3, payload
    cmd = [sys.executable, "-m", "jarvise", "paper", "expire", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    payload = _parse_stdout(proc)
    payload["paper_only"] = True
    payload["exit_code"] = proc.returncode
    publish_redis_status("jarvise:paper:expire:last", payload)
    return proc.returncode, payload


def run_paper_pending_digest() -> tuple[int, dict[str, Any]]:
    """Hourly soft-fail Telegram digest of pending approvals. Never blocks on kill-switch."""
    raw = os.environ.get("JARVISE_DB") or "data/analytics/jarvise.db"
    path = Path(raw)
    if not path.exists():
        payload = {
            "ok": True,
            "sent": False,
            "reason": "no_database",
            "count": 0,
            "paper_only": True,
        }
        publish_redis_status("jarvise:paper:digest:last", payload)
        return 0, payload
    conn = open_db(path)
    try:
        rows = list_approvals(conn, status="pending", limit=100)
    finally:
        conn.close()
    result = notify_pending_digest(rows)
    payload = {**result, "paper_only": True}
    publish_redis_status("jarvise:paper:digest:last", payload)
    return 0, payload


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


def run_paper_auto_decide() -> tuple[int, dict[str, Any]]:
    """Second-layer Claude review of pending paper candidates. Paper only; flag default off.

    Single-flight: a second trigger while a run is in progress is refused (409) and does not
    touch Redis. Any crash inside the run still publishes a payload and alerts the owner.
    """
    key = "jarvise:paper_auto:last"
    now = int(time.time() * 1000)
    if not _AUTO_DECIDE_LOCK.acquire(blocking=False):
        return 3, {"ok": False, "skipped": True, "reason": "auto_decide_running", "paper_only": True, "at_ms": now}
    try:
        unreadable = False
        try:
            engaged = kill_switch_engaged()
        except Exception:  # noqa: BLE001 — cannot read the switch → fail closed
            engaged = True
            unreadable = True
        if engaged:
            payload = {**_skipped(), "reason": "kill_switch unreadable"} if unreadable else _skipped()
            _publish_best_effort(key, payload)
            return 3, payload
        raw = os.environ.get("JARVISE_DB") or "data/analytics/jarvise.db"
        path = Path(raw)
        if not path.exists():
            payload = {"ok": False, "skipped": True, "reason": "no_database", "paper_only": True, "at_ms": now}
            _publish_best_effort(key, payload)
            return 1, payload
        conn = open_db(path)
        try:
            payload = run_auto_decide(conn, kill_switch_check=kill_switch_engaged)
        except Exception as exc:  # noqa: BLE001 — never leave Redis/Telegram silent on a crashed run
            payload = {"ok": False, "paper_only": True, "error": f"{type(exc).__name__}: {exc}", "at_ms": now}
        finally:
            conn.close()
        payload["telegram_sent"] = notify_auto_decide(payload)
        _publish_best_effort(key, payload)
        if payload.get("skipped") and payload.get("reason") == "live_trading_enabled":
            return 3, payload
        return (0 if payload.get("ok") else 1), payload
    finally:
        _AUTO_DECIDE_LOCK.release()


def run_doctrine_search(query: str, limit: int) -> dict[str, Any]:
    """GET-only doctrine lookup for web cards and the auto-decide brief."""
    hits = doctrine_snippets(query, limit=limit)
    return {"ok": True, "query": query, "hits": hits, "paper_only": True}


def _parse_stdout(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    text = (proc.stdout or "").strip()
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": proc.returncode == 0,
        "stdout": text[-2000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


ROUTES = {
    ("GET", "/healthz"): "health",
    ("GET", "/doctrine"): "doctrine",
    ("POST", "/jobs/ingest"): "ingest",
    ("POST", "/jobs/rag-refresh"): "rag",
    ("POST", "/jobs/paper-run"): "paper_run",
    ("POST", "/jobs/paper-expire"): "paper_expire",
    ("POST", "/jobs/paper-pending-digest"): "paper_pending_digest",
    ("POST", "/jobs/ingest-health"): "ingest_health",
    ("POST", "/jobs/paper-auto-decide"): "paper_auto_decide",
}


class JobHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        token = os.environ.get("JARVISE_JOBS_TOKEN") or ""
        if not token:
            return True
        return self.headers.get("X-Jarvise-Token") == token

    def _dispatch(self) -> None:
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        path = self.path.split("?", 1)[0]
        action = ROUTES.get((self.command, path))
        if action == "health":
            self._send(200, {"ok": True, "paper_only": True})
            return
        if action == "doctrine":
            params = parse_qs(self.path.partition("?")[2])
            query = (params.get("q") or [""])[0]
            try:
                limit = int((params.get("limit") or ["3"])[0])
            except ValueError:
                limit = 3
            self._send(200, run_doctrine_search(query, limit))
            return
        if action == "ingest":
            code, body = run_ingest()
            self._send(200 if code == 0 else 409 if code == 3 else 500, body)
            return
        if action == "rag":
            code, body = run_rag_refresh()
            self._send(200 if code == 0 else 409 if code == 3 else 500, body)
            return
        if action == "paper_run":
            code, body = run_paper_run()
            self._send(200 if code == 0 else 409 if code == 3 else 500, body)
            return
        if action == "paper_expire":
            code, body = run_paper_expire()
            self._send(200 if code == 0 else 409 if code == 3 else 500, body)
            return
        if action == "paper_pending_digest":
            code, body = run_paper_pending_digest()
            self._send(200 if code == 0 else 500, body)
            return
        if action == "ingest_health":
            code, body = run_ingest_health()
            self._send(200 if code == 0 else 500, body)
            return
        if action == "paper_auto_decide":
            code, body = run_paper_auto_decide()
            self._send(200 if code == 0 else 409 if code == 3 else 500, body)
            return
        self._send(404, {"ok": False, "error": "not found"})

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(host: str = "0.0.0.0", port: int = 8090) -> None:
    ThreadingHTTPServer((host, port), JobHandler).serve_forever()


def main() -> None:
    port = int(os.environ.get("JOBS_PORT", "8090"))
    serve(port=port)


if __name__ == "__main__":
    main()
