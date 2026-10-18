"""Owner written review: EV-gate reset and kill-switch clear."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jarvise_ingest.db import open_db
from jarvise_paper.cli import run
from jarvise_paper.feedback import (
    latch_ev_gate,
    list_owner_decisions,
    read_ev_gate_latch,
    reset_ev_gate_latch,
)
from jarvise_web.app import app


class _Redis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


def test_reset_rejects_short_reason_and_logs_the_sample(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "owner.db")
    latch_ev_gate(conn, at_ms=1_000, reason="auto_ev_gate", n=10, ev=-0.65)
    try:
        reset_ev_gate_latch(conn, reason="too short", at_ms=2_000)
    except ValueError as exc:
        assert "10" in str(exc)
    else:
        raise AssertionError("short reason must be refused")
    assert read_ev_gate_latch(conn) is not None
    reset_ev_gate_latch(conn, reason="owner reviewed the sample", at_ms=2_000)
    assert read_ev_gate_latch(conn) is None
    row = list_owner_decisions(conn, limit=1)[0]
    assert row["kind"] == "ev_gate_reset"
    assert row["reason"] == "owner reviewed the sample"
    payload = conn.execute("SELECT payload_json FROM owner_decision_log").fetchone()[0]
    assert '"sample_n":10' in payload
    assert "-0.65" in payload
    conn.close()


def test_cli_reset_requires_a_long_reason(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    conn = open_db(db)
    latch_ev_gate(conn, at_ms=1_000, reason="auto_ev_gate", n=10, ev=-0.65)
    conn.close()
    assert run(["ev-gate", "reset", "--confirm", "--db", str(db)]) == 2
    assert run(["ev-gate", "reset", "--confirm", "--reason", "short", "--db", str(db)]) == 2
    assert run(["ev-gate", "reset", "--confirm", "--reason", "   ", "--db", str(db)]) == 2
    conn = open_db(db)
    assert read_ev_gate_latch(conn) is not None
    conn.close()
    assert (
        run(
            [
                "ev-gate",
                "reset",
                "--confirm",
                "--reason",
                "owner reviewed the sample",
                "--db",
                str(db),
                "--json",
            ]
        )
        == 0
    )
    conn = open_db(db)
    assert read_ev_gate_latch(conn) is None
    assert list_owner_decisions(conn)[0]["kind"] == "ev_gate_reset"
    conn.close()


def _client(monkeypatch, db: Path) -> tuple[TestClient, _Redis]:
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    store = _Redis()
    monkeypatch.setattr("jarvise_web.app._redis", lambda: store)
    return TestClient(app), store


def test_api_reset_returns_422_until_the_reason_is_long(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "api.db"
    client, _store = _client(monkeypatch, db)
    conn = open_db(db)
    latch_ev_gate(conn, at_ms=1_000, reason="auto_ev_gate", n=10, ev=-0.65)
    conn.close()
    missing = client.post("/api/paper/ev-gate/reset", json={"confirm": True})
    assert missing.status_code == 422
    short = client.post("/api/paper/ev-gate/reset", json={"confirm": True, "reason": "short"})
    assert short.status_code == 422
    blank = client.post("/api/paper/ev-gate/reset", json={"confirm": True, "reason": "   "})
    assert blank.status_code == 422
    conn = open_db(db)
    assert read_ev_gate_latch(conn) is not None
    conn.close()
    ok = client.post(
        "/api/paper/ev-gate/reset",
        json={"confirm": True, "reason": "owner reviewed the sample"},
    )
    assert ok.status_code == 200
    assert ok.json()["latched"] is False


def test_kill_switch_engage_is_never_blocked_by_a_missing_reason(monkeypatch, tmp_path: Path) -> None:
    client, store = _client(monkeypatch, tmp_path / "ks.db")
    headers = {"Accept": "application/json"}
    engaged = client.post("/kill-switch", data={"state": "on"}, headers=headers)
    assert engaged.status_code == 200
    assert engaged.json()["kill_switch"] is True
    assert store.data["jarvise:kill_switch"] == "1"
    refused = client.post("/kill-switch", data={"state": "off", "reason": "no"}, headers=headers)
    assert refused.status_code == 422
    assert store.data["jarvise:kill_switch"] == "1"
    empty = client.post("/kill-switch", data={"state": "off"}, headers=headers)
    assert empty.status_code == 422
    assert store.data["jarvise:kill_switch"] == "1"
    cleared = client.post(
        "/kill-switch",
        data={"state": "off", "reason": "reviewed the halt cause"},
        headers=headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["kill_switch"] is False
    assert store.data["jarvise:kill_switch"] == "0"
    conn = open_db(tmp_path / "ks.db")
    rows = list_owner_decisions(conn, limit=10)
    assert rows[0]["kind"] == "kill_switch_off"
    assert rows[0]["reason"] == "reviewed the halt cause"
    assert any(row["kind"] == "kill_switch_on" and not row["reason"] for row in rows)
    conn.close()


def test_kill_switch_engage_survives_a_log_failure(monkeypatch, tmp_path: Path) -> None:
    client, store = _client(monkeypatch, tmp_path / "ks-fail.db")

    def _boom(_path: Path):
        raise OSError("ledger busy")

    monkeypatch.setattr("jarvise_web.app.open_db", _boom)
    engaged = client.post(
        "/kill-switch",
        data={"state": "on"},
        headers={"Accept": "application/json"},
    )
    assert engaged.status_code == 200
    assert store.data["jarvise:kill_switch"] == "1"
