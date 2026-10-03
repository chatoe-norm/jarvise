"""Internal job HTTP API for n8n. Docker network only. Paper only. No order placement."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from jarvise.rag import doctrine_snippets, publish_redis_status
from jarvise_ingest.db import list_approvals, open_db
from jarvise_ingest.health import ingest_health
from jarvise_notify import (
    notify_auto_decide,
    notify_ingest_health,
    notify_job_failure,
    notify_pending_digest,
)
from jarvise_obs.metrics import observe_job, render_prometheus, set_gauge
from jarvise_paper.auto_decide import run_auto_decide
from jarvise_risk import kill_switch_state
from jarvise_trade import reconcile_live_orders

# Subprocess wall-clock limits (seconds). A hung CLI must never wedge n8n or the health probe.
JOB_TIMEOUTS_S: dict[str, float] = {
    "ingest": 900.0,
    "paper_run": 300.0,
    "paper_expire": 120.0,
    "rag": 900.0,
}
# Single-flight lock TTLs (seconds): a crashed holder frees the lock automatically.
JOB_LOCK_TTL_S: dict[str, int] = {
    "ingest": 1200,
    "paper_run": 600,
    "paper_expire": 300,
    "rag": 1200,
    "paper_pending_digest": 120,
    "paper_auto_decide": 900,
    "live_reconcile": 300,
}
TIMEOUT_EXIT_CODE = 124


def kill_switch_engaged() -> bool:
    """Fail closed: unset REDIS_URL, missing driver, or a connection error all count as engaged."""
    return bool(kill_switch_state(strict=True)["engaged"])


def _skipped() -> dict[str, Any]:
    state = kill_switch_state(strict=True)
    if state["known"]:
        reason = "kill_switch engaged"
    else:
        reason = f"kill_switch unreadable: {state.get('error') or 'unknown'}"
    return {
        "ok": False,
        "skipped": True,
        "reason": reason,
        "kill_switch_known": bool(state["known"]),
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


def job_timeout_s(job: str) -> float:
    return _fenv(f"JARVISE_JOB_TIMEOUT_{job.upper()}_S", JOB_TIMEOUTS_S.get(job, 600.0))


class _TimedOut:
    """Stand-in for CompletedProcess when the subprocess hit its wall-clock limit."""

    returncode = TIMEOUT_EXIT_CODE

    def __init__(self, job: str, timeout_s: float, partial_stdout: str, partial_stderr: str) -> None:
        self.stdout = partial_stdout
        self.stderr = f"timeout after {timeout_s:.0f}s running {job}\n{partial_stderr}".strip()
        self.job = job
        self.timeout_s = timeout_s


def _run_cmd(job: str, cmd: list[str]) -> Any:
    """subprocess.run with a hard timeout; on timeout alert the owner and return a failure shape."""
    timeout_s = job_timeout_s(job)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        notify_job_failure(job, f"timeout after {timeout_s:.0f}s")
        return _TimedOut(job, timeout_s, out, err)


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock(job: str) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(job)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[job] = lock
        return lock


_RELEASE_IF_OWNER = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


@contextlib.contextmanager
def single_flight(job: str) -> Iterator[bool]:
    """Yield True when this process holds the job lock, False when another run is active.

    Redis ``SET jarvise:lock:<job> <token> NX EX <ttl>`` coordinates across processes
    (jobs container, manual CLI, worker). When Redis is unavailable, fall back to an
    in-process lock so a single jobs process still never overlaps itself.
    """
    ttl = JOB_LOCK_TTL_S.get(job, 600)
    key = f"jarvise:lock:{job}"
    token = secrets.token_hex(8)
    redis_url = os.environ.get("REDIS_URL")
    client = None
    if redis_url:
        try:
            import redis

            client = redis.Redis.from_url(
                redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3
            )
            acquired = bool(client.set(key, token, nx=True, ex=ttl))
        except Exception:  # noqa: BLE001 — Redis down → local fallback below
            client = None
    if client is None:
        local = _local_lock(job)
        acquired = local.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                local.release()
        return
    try:
        yield acquired
    finally:
        if acquired:
            try:
                client.eval(_RELEASE_IF_OWNER, 1, key, token)
            except Exception:  # noqa: BLE001 — TTL will free it
                pass


def _running(job: str) -> dict[str, Any]:
    return {
        "ok": False,
        "skipped": True,
        "reason": f"{job}_running",
        "paper_only": True,
        "at_ms": int(time.time() * 1000),
    }


def _finish_cli_payload(job: str, proc: Any, key: str) -> tuple[int, dict[str, Any]]:
    payload = _parse_stdout(proc)
    payload["paper_only"] = True
    payload["exit_code"] = proc.returncode
    if proc.returncode == TIMEOUT_EXIT_CODE:
        payload["ok"] = False
        payload["error"] = f"timeout after {job_timeout_s(job):.0f}s"
    _publish_best_effort(key, payload)
    return proc.returncode, payload


def run_ingest() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        _publish_best_effort("jarvise:ingest:last", payload)
        return 3, payload
    with single_flight("ingest") as acquired:
        if not acquired:
            return 3, _running("ingest")
        symbols = os.environ.get("JARVISE_INGEST_SYMBOLS", "BTCUSDT,ETHUSDT")
        paper_tf = os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
        # 1h microstructure + paper TF + 1d HTF for analyzer MTF confirm (T2.3).
        timeframes = ["1h"]
        if paper_tf not in timeframes:
            timeframes.append(paper_tf)
        htf = (os.environ.get("JARVISE_ANALYZE_HTF") or "1d").strip() or "1d"
        if htf not in timeframes:
            timeframes.append(htf)

        by_tf: dict[str, Any] = {}
        exit_code = 0
        timed_out = False
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
            proc = _run_cmd("ingest", cmd)
            by_tf[tf] = _parse_stdout(proc)
            if proc.returncode != 0:
                exit_code = proc.returncode
            if proc.returncode == TIMEOUT_EXIT_CODE:
                timed_out = True

        payload = {
            "ok": exit_code == 0,
            "paper_only": True,
            "exit_code": exit_code,
            "timeframes": by_tf,
        }
        if timed_out:
            payload["error"] = f"timeout after {job_timeout_s('ingest'):.0f}s"
        # Prefer primary 1h shape at top-level for older Pipeline readers when single-tf.
        if "1h" in by_tf and isinstance(by_tf["1h"], dict):
            for key in ("db", "market_technicals", "errors", "duration_s"):
                if key in by_tf["1h"]:
                    payload[key] = by_tf["1h"][key]
        _publish_best_effort("jarvise:ingest:last", payload)
        return exit_code, payload


def run_rag_refresh() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        _publish_best_effort("jarvise:rag:last", payload)
        return 3, payload
    with single_flight("rag") as acquired:
        if not acquired:
            return 3, _running("rag")
        cmd = [sys.executable, "-m", "jarvise", "rag", "refresh", "--output", "json"]
        proc = _run_cmd("rag", cmd)
        # CLI also publishes; mirror ingest so Redis stays JSON even if CLI publish is skipped.
        return _finish_cli_payload("rag", proc, "jarvise:rag:last")


def run_paper_run() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        _publish_best_effort("jarvise:paper:last", payload)
        return 3, payload
    with single_flight("paper_run") as acquired:
        if not acquired:
            return 3, _running("paper_run")
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
        proc = _run_cmd("paper_run", cmd)
        return _finish_cli_payload("paper_run", proc, "jarvise:paper:last")


def run_paper_expire() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        _publish_best_effort("jarvise:paper:expire:last", payload)
        return 3, payload
    with single_flight("paper_expire") as acquired:
        if not acquired:
            return 3, _running("paper_expire")
        cmd = [sys.executable, "-m", "jarvise", "paper", "expire", "--json"]
        proc = _run_cmd("paper_expire", cmd)
        return _finish_cli_payload("paper_expire", proc, "jarvise:paper:expire:last")


def run_paper_pending_digest() -> tuple[int, dict[str, Any]]:
    """Hourly soft-fail Telegram digest of pending approvals. Never blocks on kill-switch."""
    with single_flight("paper_pending_digest") as acquired:
        if not acquired:
            return 3, _running("paper_pending_digest")
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
            _publish_best_effort("jarvise:paper:digest:last", payload)
            return 0, payload
        conn = open_db(path)
        try:
            rows = list_approvals(conn, status="pending", limit=100)
        finally:
            conn.close()
        result = notify_pending_digest(rows)
        payload = {**result, "paper_only": True}
        _publish_best_effort("jarvise:paper:digest:last", payload)
        return 0, payload


def run_ingest_health() -> tuple[int, dict[str, Any]]:
    """Read-only warm-up/freshness check; alert only. Never triggers ingest, ignores kill-switch."""
    raw = os.environ.get("JARVISE_DB") or "data/analytics/jarvise.db"
    path = Path(raw)
    timeframe = os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"
    symbols = [
        s for s in (os.environ.get("JARVISE_INGEST_SYMBOLS", "BTCUSDT,ETHUSDT")).split(",") if s.strip()
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
    _publish_best_effort("jarvise:ingest:health", payload)
    return 0, payload


def run_paper_auto_decide() -> tuple[int, dict[str, Any]]:
    """Second-layer Claude review of pending paper candidates. Paper only; flag default off.

    Single-flight: a second trigger while a run is in progress is refused (409) and does not
    touch Redis. Any crash inside the run still publishes a payload and alerts the owner.
    """
    key = "jarvise:paper_auto:last"
    now = int(time.time() * 1000)
    with single_flight("paper_auto_decide") as acquired:
        if not acquired:
            return 3, {
                "ok": False,
                "skipped": True,
                "reason": "auto_decide_running",
                "paper_only": True,
                "at_ms": now,
            }
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
            payload = {
                "ok": False,
                "skipped": True,
                "reason": "no_database",
                "paper_only": True,
                "at_ms": now,
            }
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


def run_live_reconcile() -> tuple[int, dict[str, Any]]:
    """Read-only venue reconciliation of open live orders. Runs regardless of kill-switch
    (it places nothing) and is a no-op when live has never been enabled."""
    key = "jarvise:live:reconcile:last"
    now = int(time.time() * 1000)
    with single_flight("live_reconcile") as acquired:
        if not acquired:
            return 3, _running("live_reconcile")
        raw = os.environ.get("JARVISE_DB") or "data/analytics/jarvise.db"
        path = Path(raw)
        if not path.exists():
            payload = {
                "ok": True,
                "skipped": True,
                "reason": "no_database",
                "open": 0,
                "read_only": True,
                "at_ms": now,
            }
            _publish_best_effort(key, payload)
            return 0, payload
        conn = open_db(path)
        try:
            payload = reconcile_live_orders(conn, now_ms=now)
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "read_only": True, "error": f"{type(exc).__name__}: {exc}", "at_ms": now}
        finally:
            conn.close()
        _publish_best_effort(key, payload)
        return (0 if payload.get("ok") else 1), payload


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
    ("GET", "/metrics"): "metrics",
    ("GET", "/doctrine"): "doctrine",
    ("POST", "/jobs/ingest"): "ingest",
    ("POST", "/jobs/rag-refresh"): "rag",
    ("POST", "/jobs/paper-run"): "paper_run",
    ("POST", "/jobs/paper-expire"): "paper_expire",
    ("POST", "/jobs/paper-pending-digest"): "paper_pending_digest",
    ("POST", "/jobs/ingest-health"): "ingest_health",
    ("POST", "/jobs/paper-auto-decide"): "paper_auto_decide",
    ("POST", "/jobs/live-reconcile"): "live_reconcile",
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
        if action == "metrics":
            ks = kill_switch_state(strict=False)
            set_gauge("jarvise_kill_switch", 1.0 if ks.get("engaged") else 0.0)
            metrics_body = render_prometheus()
            self._send_text(200, metrics_body)
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
        runners = {
            "ingest": run_ingest,
            "rag": run_rag_refresh,
            "paper_run": run_paper_run,
            "paper_expire": run_paper_expire,
            "paper_pending_digest": run_paper_pending_digest,
            "ingest_health": run_ingest_health,
            "paper_auto_decide": run_paper_auto_decide,
            "live_reconcile": run_live_reconcile,
        }
        runner = runners.get(action or "")
        if runner is None or action is None:
            self._send(404, {"ok": False, "error": "not found"})
            return
        started = time.monotonic()
        code, payload = runner()
        observe_job(action, ok=(code == 0), duration_s=time.monotonic() - started)
        if action == "ingest_health":
            self._send(200 if code == 0 else 500, payload)
        else:
            self._send(200 if code == 0 else 409 if code == 3 else 500, payload)

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_text(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
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
