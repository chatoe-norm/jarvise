"""Thin private control UI for Jarvise Docker stack (paper-only)."""

from __future__ import annotations

import os
import secrets
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

app = FastAPI(title="Jarvise Control", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "jarvise_doctrine")
PAPER_ONLY = os.environ.get("JARVISE_PAPER_ONLY", "true").lower() in {"1", "true", "yes"}
KILL_SWITCH_KEY = "jarvise:kill_switch"
INGEST_KEY = "jarvise:ingest:last"
RAG_KEY = "jarvise:rag:last"


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

        return json.loads(raw)
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


def page(body: str, title: str = "Jarvise") -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
  <style>
    :root {{ --bg:#0f1419; --fg:#e7ecf1; --muted:#8b9aab; --accent:#3d9cf0; --danger:#e35d6a; --ok:#3ecf8e; }}
    body {{ margin:0; font-family: ui-sans-serif, system-ui, sans-serif; background:var(--bg); color:var(--fg); }}
    main {{ max-width:720px; margin:0 auto; padding:2rem 1.25rem; }}
    h1 {{ font-size:1.5rem; margin:0 0 .25rem; }}
    .banner {{ display:inline-block; padding:.2rem .55rem; border:1px solid var(--ok); color:var(--ok); border-radius:4px; font-size:.8rem; margin-bottom:1.25rem; }}
    .card {{ border:1px solid #243041; border-radius:8px; padding:1rem 1.1rem; margin-bottom:1rem; }}
    .muted {{ color:var(--muted); font-size:.9rem; }}
    .row {{ display:flex; gap:.75rem; flex-wrap:wrap; align-items:center; }}
    button, .btn {{ background:var(--accent); color:#041018; border:0; border-radius:6px; padding:.5rem .9rem; font-weight:600; cursor:pointer; text-decoration:none; }}
    button.danger {{ background:var(--danger); color:#fff; }}
    button.ok {{ background:var(--ok); color:#041018; }}
    code {{ font-size:.85rem; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:#161d27; padding:.75rem; border-radius:6px; font-size:.8rem; }}
  </style>
</head>
<body>
<main>
  <h1>Jarvise control</h1>
  <div class="banner">PAPER ONLY — no order placement</div>
  {body}
</main>
</body>
</html>"""
    return HTMLResponse(html)


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
      <strong>Qdrant</strong> <span class="muted">{COLLECTION}</span>
      <pre>{qd}</pre>
    </div>
    <div class="card">
      <strong>Last ingest</strong> <span class="muted">{INGEST_KEY}</span>
      <pre>{ingest}</pre>
    </div>
    <div class="card">
      <strong>Last RAG</strong> <span class="muted">{RAG_KEY}</span>
      <pre>{rag}</pre>
    </div>
    <p class="muted">Perimeter: Tailscale. Optional basic auth via WEB_BASIC_AUTH_*.</p>
    """
    return page(body)


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
