"""Internal job HTTP API for n8n. Docker network only. Paper only. No order placement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jarvise.rag import publish_redis_status


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
    }


def run_ingest() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        payload = _skipped()
        publish_redis_status("jarvise:ingest:last", payload)
        return 3, payload
    cmd = [
        sys.executable,
        "-m",
        "jarvise",
        "ingest",
        "--symbol",
        os.environ.get("JARVISE_INGEST_SYMBOLS", "BTCUSDT,ETHUSDT"),
        "--timeframe",
        "1h",
        "--limit",
        "200",
        "--skip-derivatives",
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    payload = _parse_stdout(proc)
    payload["paper_only"] = True
    payload["exit_code"] = proc.returncode
    publish_redis_status("jarvise:ingest:last", payload)
    return proc.returncode, payload


def run_rag_refresh() -> tuple[int, dict[str, Any]]:
    if kill_switch_engaged():
        return 3, _skipped()
    cmd = [sys.executable, "-m", "jarvise", "rag", "refresh", "--output", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    payload = _parse_stdout(proc)
    payload["paper_only"] = True
    payload["exit_code"] = proc.returncode
    return proc.returncode, payload


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
    ("POST", "/jobs/ingest"): "ingest",
    ("POST", "/jobs/rag-refresh"): "rag",
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
        if action == "ingest":
            code, body = run_ingest()
            self._send(200 if code == 0 else 409 if code == 3 else 500, body)
            return
        if action == "rag":
            code, body = run_rag_refresh()
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
