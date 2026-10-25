"""Disk free-space edge alerts on ingest-health."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvise.disk import DISK_LOW_KEY, disk_free, read_disk_low_flag
from jarvise.jobs import run_ingest_health
from jarvise_ingest.db import open_db


def _flag(conn: Any) -> float | None:
    row = conn.execute(
        "SELECT value FROM paper_account WHERE key=?",
        (DISK_LOW_KEY,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def test_disk_free_reports_numbers(tmp_path: Path) -> None:
    db = tmp_path / "analytics" / "jarvise.db"
    db.parent.mkdir(parents=True)
    open_db(db).close()
    out = disk_free(db)
    assert out.get("disk_error") is None
    assert out["disk_total_bytes"] and out["disk_total_bytes"] > 0
    assert out["disk_free_bytes"] is not None
    assert out["disk_free_pct"] is not None


def test_ingest_health_disk_low_alerts_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "j.db"))
    monkeypatch.setenv("JARVISE_DISK_MIN_FREE_PCT", "99")
    monkeypatch.setenv("JARVISE_DISK_MIN_FREE_GB", "999999")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr("jarvise.jobs.notify_ingest_health", lambda *_a, **_k: False)
    monkeypatch.setattr("jarvise.jobs._publish_best_effort", lambda *_a, **_k: None)
    monkeypatch.setattr("jarvise.jobs.resolve_active_ingest_symbols", lambda: "BTCUSDT")
    alerts: list[str] = []
    monkeypatch.setattr(
        "jarvise.jobs.notify_disk_space",
        lambda **kwargs: alerts.append("low" if kwargs.get("low") else "ok") or True,
    )
    open_db(tmp_path / "j.db").close()

    code, body = run_ingest_health()
    assert code == 0
    assert body["disk_low"] is True
    assert body["disk_alert"] == "low"
    assert alerts == ["low"]

    code2, body2 = run_ingest_health()
    assert code2 == 0
    assert body2["disk_low"] is True
    assert body2.get("disk_alert") is None
    assert alerts == ["low"]
    assert _flag(open_db(tmp_path / "j.db")) == 1.0


def test_ingest_health_disk_recovery_alerts_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "j.db"))
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr("jarvise.jobs.notify_ingest_health", lambda *_a, **_k: False)
    monkeypatch.setattr("jarvise.jobs._publish_best_effort", lambda *_a, **_k: None)
    monkeypatch.setattr("jarvise.jobs.resolve_active_ingest_symbols", lambda: "BTCUSDT")
    alerts: list[str] = []
    monkeypatch.setattr(
        "jarvise.jobs.notify_disk_space",
        lambda **kwargs: alerts.append("low" if kwargs.get("low") else "ok") or True,
    )
    open_db(tmp_path / "j.db").close()

    monkeypatch.setenv("JARVISE_DISK_MIN_FREE_PCT", "99")
    monkeypatch.setenv("JARVISE_DISK_MIN_FREE_GB", "999999")
    assert run_ingest_health()[1]["disk_alert"] == "low"

    monkeypatch.setenv("JARVISE_DISK_MIN_FREE_PCT", "0")
    monkeypatch.setenv("JARVISE_DISK_MIN_FREE_GB", "0")
    body = run_ingest_health()[1]
    assert body["disk_low"] is False
    assert body["disk_alert"] == "ok"
    assert alerts == ["low", "ok"]
    body2 = run_ingest_health()[1]
    assert body2.get("disk_alert") is None
    assert alerts == ["low", "ok"]


def test_disk_usage_oserror_soft_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "j.db"))
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr("jarvise.jobs.notify_ingest_health", lambda *_a, **_k: False)
    monkeypatch.setattr("jarvise.jobs._publish_best_effort", lambda *_a, **_k: None)
    monkeypatch.setattr("jarvise.jobs.resolve_active_ingest_symbols", lambda: "BTCUSDT")
    alerts: list[str] = []
    monkeypatch.setattr(
        "jarvise.jobs.notify_disk_space",
        lambda **kwargs: alerts.append("x") or True,
    )
    open_db(tmp_path / "j.db").close()
    conn = open_db(tmp_path / "j.db")
    from jarvise.disk import write_disk_low_flag

    write_disk_low_flag(conn, False)
    conn.close()

    def _boom(_path: Any) -> Any:
        raise OSError("nope")

    monkeypatch.setattr("jarvise.disk.shutil.disk_usage", _boom)
    code, body = run_ingest_health()
    assert code == 0
    assert body["disk_low"] is False
    assert "disk_error" in body
    assert alerts == []
    assert read_disk_low_flag(open_db(tmp_path / "j.db")) is False
