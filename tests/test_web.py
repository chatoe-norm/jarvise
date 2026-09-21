from __future__ import annotations

from fastapi.testclient import TestClient

from jarvise_web.app import app


def test_healthz() -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["paper_only"] is True


def test_dashboard_renders_without_auth(monkeypatch) -> None:
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)

    def fake_redis_get(key: str):
        return "0"

    def fake_json(key: str):
        return {"ok": True}

    def fake_qdrant():
        return {"exists": True, "points": 0}

    monkeypatch.setattr("jarvise_web.app.redis_get", fake_redis_get)
    monkeypatch.setattr("jarvise_web.app.redis_get_json", fake_json)
    monkeypatch.setattr("jarvise_web.app.qdrant_info", fake_qdrant)

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"PAPER ONLY" in resp.content
