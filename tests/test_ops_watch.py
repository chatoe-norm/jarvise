"""Stale OpenClaw digest and OpenRouter credit edge alerts. No network."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from jarvise.jobs import run_ingest_health
from jarvise.ops_watch import OPENROUTER_KEY_URL
from jarvise_ingest.db import open_db
from jarvise_notify.telegram import (
    format_digest_recovered_message,
    format_digest_stale_message,
    format_openrouter_credit_message,
)
from jarvise_notify.weekly import _health_items

NOW = 1_700_000_000.0
PAYLOAD_KEYS = (
    "digest_newest",
    "digest_age_h",
    "digest_stale",
    "openrouter_remaining",
    "openrouter_low",
    "openrouter_error",
)


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _health_env(tmp_path: Path, monkeypatch: Any, digest: Path) -> None:
    digest.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "j.db"))
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("jarvise.jobs.notify_ingest_health", lambda *_a, **_k: False)
    monkeypatch.setattr("jarvise.jobs.notify_disk_space", lambda **_k: False)
    monkeypatch.setattr("jarvise.jobs._publish_best_effort", lambda *_a, **_k: None)
    monkeypatch.setattr("jarvise.jobs.resolve_active_ingest_symbols", lambda: "BTCUSDT")
    monkeypatch.setattr("jarvise.jobs.digest_dir", lambda: digest)
    monkeypatch.setattr("jarvise.ops_watch.time.time", lambda: NOW)
    open_db(tmp_path / "j.db").close()


def _healthy_credit() -> dict[str, Any]:
    return {"openrouter_remaining": 10.0, "openrouter_low": False, "openrouter_error": None}


def _touch(path: Path, *, age_h: float) -> None:
    path.write_text("digest\n", encoding="utf-8")
    stamp = NOW - age_h * 3600.0
    os.utime(path, (stamp, stamp))


def test_fresh_digest_sends_no_alert(tmp_path: Path, monkeypatch: Any) -> None:
    digest = tmp_path / "digests"
    _health_env(tmp_path, monkeypatch, digest)
    _touch(digest / "2026-11-10-00.md", age_h=1.0)
    sent: list[str] = []
    monkeypatch.setattr(
        "jarvise.jobs.notify_digest_freshness",
        lambda **kwargs: sent.append("stale" if kwargs.get("stale") else "ok") or True,
    )
    monkeypatch.setattr("jarvise.jobs.fetch_openrouter_credit", lambda *_a, **_k: _healthy_credit())
    code, body = run_ingest_health()
    assert code == 0
    assert body["digest_stale"] is False
    assert body["digest_newest"] == "2026-11-10-00.md"
    assert body["digest_alert"] is None
    assert sent == []
    for key in PAYLOAD_KEYS:
        assert key in body


def test_stale_digest_alerts_once_then_recovers(tmp_path: Path, monkeypatch: Any) -> None:
    digest = tmp_path / "digests"
    _health_env(tmp_path, monkeypatch, digest)
    _touch(digest / "2026-11-03-12.md", age_h=27.0)
    sent: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        "jarvise.jobs.notify_digest_freshness",
        lambda **kwargs: sent.append((bool(kwargs.get("stale")), kwargs.get("filename"))) or True,
    )
    monkeypatch.setattr("jarvise.jobs.fetch_openrouter_credit", lambda *_a, **_k: _healthy_credit())

    first = run_ingest_health()[1]
    assert first["digest_stale"] is True
    assert first["digest_alert"] == "stale"
    assert sent == [(True, "2026-11-03-12.md")]

    second = run_ingest_health()[1]
    assert second["digest_stale"] is True
    assert second["digest_alert"] is None
    assert sent == [(True, "2026-11-03-12.md")]

    _touch(digest / "2026-11-10-07.md", age_h=0.5)
    third = run_ingest_health()[1]
    assert third["digest_stale"] is False
    assert third["digest_newest"] == "2026-11-10-07.md"
    assert third["digest_alert"] == "ok"
    assert sent == [(True, "2026-11-03-12.md"), (False, "2026-11-10-07.md")]


def test_empty_digest_dir_is_stale(tmp_path: Path, monkeypatch: Any) -> None:
    digest = tmp_path / "digests"
    _health_env(tmp_path, monkeypatch, digest)
    sent: list[str | None] = []
    monkeypatch.setattr(
        "jarvise.jobs.notify_digest_freshness",
        lambda **kwargs: sent.append(kwargs.get("filename")) or True,
    )
    monkeypatch.setattr("jarvise.jobs.fetch_openrouter_credit", lambda *_a, **_k: _healthy_credit())
    body = run_ingest_health()[1]
    assert body["digest_stale"] is True
    assert body["digest_newest"] is None
    assert body["digest_age_h"] is None
    assert body["digest_alert"] == "stale"
    assert sent == [None]
    assert format_digest_stale_message(hours=None, filename=None).startswith(
        "⚠️ OpenClaw ไม่ได้เขียนข่าวมา (ไม่มีไฟล์)"
    )


def test_openrouter_low_then_recovery(tmp_path: Path, monkeypatch: Any) -> None:
    digest = tmp_path / "digests"
    _health_env(tmp_path, monkeypatch, digest)
    _touch(digest / "fresh.md", age_h=1.0)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls: list[dict[str, Any]] = []

    def _get(url: str, **kwargs: Any) -> _Response:
        assert url == OPENROUTER_KEY_URL
        assert "Authorization" in kwargs["headers"]
        remaining = calls_remaining[0]
        return _Response(200, {"data": {"limit_remaining": remaining, "usage": 1.0}})

    calls_remaining = [1.5]
    monkeypatch.setattr("jarvise.ops_watch.httpx.get", _get)
    monkeypatch.setattr(
        "jarvise.jobs.notify_openrouter_credit",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    monkeypatch.setattr("jarvise.jobs.notify_digest_freshness", lambda **_k: False)

    low = run_ingest_health()[1]
    assert low["openrouter_low"] is True
    assert low["openrouter_remaining"] == 1.5
    assert low["openrouter_error"] is None
    assert low["openrouter_alert"] == "low"
    assert calls[-1]["kind"] == "low"

    again = run_ingest_health()[1]
    assert again["openrouter_alert"] is None
    assert len(calls) == 1

    calls_remaining[0] = 3.0
    recovered = run_ingest_health()[1]
    assert recovered["openrouter_low"] is False
    assert recovered["openrouter_remaining"] == 3.0
    assert recovered["openrouter_alert"] == "ok"
    assert calls[-1]["kind"] == "ok"
    assert format_openrouter_credit_message(kind="low", remaining=1.5) == (
        "⚠️ เครดิต OpenRouter เหลือ 1.5 USD — เติมก่อนหมด (OpenClaw และ shadow veto ใช้ key นี้)"
    )
    assert format_openrouter_credit_message(kind="ok", remaining=3.0) == "✅ เครดิต OpenRouter เหลือ 3 USD"


def test_openrouter_401_is_error_not_low(tmp_path: Path, monkeypatch: Any) -> None:
    digest = tmp_path / "digests"
    _health_env(tmp_path, monkeypatch, digest)
    _touch(digest / "fresh.md", age_h=1.0)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-SECRET")
    monkeypatch.setattr("jarvise.ops_watch.httpx.get", lambda *_a, **_k: _Response(401, {"error": "nope"}))
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "jarvise.jobs.notify_openrouter_credit",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    monkeypatch.setattr("jarvise.jobs.notify_digest_freshness", lambda **_k: False)
    body = run_ingest_health()[1]
    assert body["openrouter_low"] is False
    assert body["openrouter_remaining"] is None
    assert body["openrouter_error"] == "401"
    assert body["openrouter_alert"] == "error"
    assert calls == [{"kind": "error", "status": "401"}]
    quiet = run_ingest_health()[1]
    assert quiet["openrouter_alert"] is None
    assert calls == [{"kind": "error", "status": "401"}]
    assert format_openrouter_credit_message(kind="error", status="401") == "อ่านสถานะ OpenRouter ไม่ได้: 401"
    assert "sk-or-v1-SECRET" not in str(body)


def test_openrouter_network_error_does_not_break_health(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    digest = tmp_path / "digests"
    _health_env(tmp_path, monkeypatch, digest)
    _touch(digest / "fresh.md", age_h=1.0)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-SECRET")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise httpx.ConnectError("down")

    monkeypatch.setattr("jarvise.ops_watch.httpx.get", _boom)
    monkeypatch.setattr("jarvise.jobs.notify_openrouter_credit", lambda **_k: True)
    monkeypatch.setattr("jarvise.jobs.notify_digest_freshness", lambda **_k: False)
    with caplog.at_level(logging.WARNING):
        code, body = run_ingest_health()
    assert code == 0
    assert isinstance(body["symbols"], dict)
    assert "alerts" in body
    assert body["disk_low"] is False
    assert body["openrouter_low"] is False
    assert body["openrouter_error"] == "ConnectError"
    assert body["openrouter_alert"] == "error"
    for key in PAYLOAD_KEYS:
        assert key in body
    assert "sk-or-v1-SECRET" not in caplog.text


def test_quiet_mode_does_not_suppress_digest_alert(tmp_path: Path, monkeypatch: Any) -> None:
    digest = tmp_path / "digests"
    _health_env(tmp_path, monkeypatch, digest)
    _touch(digest / "2026-11-03-12.md", age_h=27.0)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr("jarvise_notify.quiet.quiet_mode_enabled", lambda: True)
    monkeypatch.setattr("jarvise.jobs.fetch_openrouter_credit", lambda *_a, **_k: _healthy_credit())
    sent: list[str] = []
    monkeypatch.setattr(
        "jarvise_notify.telegram.send_telegram_message",
        lambda text, **_k: sent.append(text) or True,
    )
    body = run_ingest_health()[1]
    assert body["digest_alert"] == "stale"
    assert sent == [
        format_digest_stale_message(hours=27.0, filename="2026-11-03-12.md"),
    ]
    assert sent[0] == (
        "⚠️ OpenClaw ไม่ได้เขียนข่าวมา 27 ชม. (ล่าสุด 2026-11-03-12.md) — เช็กเครดิต OpenRouter และ log ของ openclaw"
    )
    assert (
        format_digest_recovered_message("2026-11-10-07.md") == "✅ OpenClaw เขียนข่าวได้แล้ว (2026-11-10-07.md)"
    )


def test_weekly_line_uses_health_blob() -> None:
    now = 1_700_000_000_000
    items = _health_items(
        {
            "ingest": {"ok": True, "at_ms": now},
            "digest_age_hours": 2.0,
            "ingest_health": {
                "ok": True,
                "at_ms": now,
                "digest_age_h": 30.0,
                "digest_stale": True,
                "openrouter_remaining": 1.5,
                "openrouter_low": True,
                "openrouter_error": None,
            },
            "risk_monitor": {"ok": True, "at_ms": now},
        },
        now_ms=now,
        freshness={"market_safety_max_age_min": 90, "risk_monitor_stale_after_min": 35},
    )
    credit = next(item for item in items if "OpenRouter" in item["line"])
    assert credit["level"] == "ต้องทำ"
    assert credit["line"] == "ข่าว 30.0 ชม. · OpenRouter เหลือ 1.50 USD"

    clear = _health_items(
        {
            "ingest": {"ok": True, "at_ms": now},
            "digest_age_hours": 40.0,
            "ingest_health": {
                "at_ms": now,
                "digest_age_h": 4.0,
                "digest_stale": False,
                "openrouter_remaining": None,
                "openrouter_low": False,
                "openrouter_error": "401",
            },
            "risk_monitor": {"ok": True, "at_ms": now},
        },
        now_ms=now,
        freshness={"market_safety_max_age_min": 90, "risk_monitor_stale_after_min": 35},
    )
    looked = next(item for item in clear if "OpenRouter" in item["line"])
    assert looked["level"] == "ดู"
    assert looked["line"] == "ข่าว 4.0 ชม. · OpenRouter เหลือ — USD"
