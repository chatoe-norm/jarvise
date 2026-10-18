"""Weekly report text. Read-only snapshots, no ledger writes."""

from jarvise_notify.weekly import (
    SHADOW_WINDOW_ACTION,
    _health_items,
    build_weekly_report,
    format_weekly_report,
)


def _quiet_week() -> dict:
    return {
        "live": False,
        "auto_decide": False,
        "ev_gate": {"latched": False, "n": 0, "ev": None},
        "kill_switch": False,
        "kill_switch_known": True,
        "shadow": {
            "n_scored": 2,
            "n_no_objection": 2,
            "n_veto": 0,
            "elapsed_days": 3,
            "window_closed": False,
        },
        "paper": {"equity": 10000, "open_positions": 0, "closed_7d": 0, "pnl_7d": 0},
        "health": [
            {"line": "ingest อายุ 5 นาที", "level": "OK", "note": ""},
            {"line": "ข่าว อายุ 2.0 ชม.", "level": "OK", "note": ""},
            {"line": "risk monitor อายุ 5 นาที", "level": "OK", "note": ""},
        ],
        "jobs": [{"name": "ingest", "result": "ok", "level": "OK", "note": ""}],
    }


def test_weekly_report_nothing_to_do() -> None:
    text = format_weekly_report(_quiet_week())["text"]
    assert text.endswith("ไม่ต้องทำอะไร")
    assert "ต้องทำ:" not in text
    assert "ดู:" not in text
    assert "Fear" not in text
    assert "fear" not in text


def test_weekly_report_window_closed() -> None:
    snap = _quiet_week()
    snap["shadow"] = {**snap["shadow"], "n_scored": 40, "window_closed": True}
    report = format_weekly_report(snap)
    assert f"ต้องทำ: {SHADOW_WINDOW_ACTION}" in report["text"]
    assert report["text"].endswith(f"ต้องทำ: {SHADOW_WINDOW_ACTION}")


def test_weekly_report_stale_digest() -> None:
    snap = _quiet_week()
    snap["health"] = [
        {"line": "ingest อายุ 5 นาที", "level": "OK", "note": ""},
        {"line": "ข่าว อายุ 40.0 ชม.", "level": "ดู", "note": "ข่าวเก่า 40.0 ชม."},
        {"line": "risk monitor อายุ 5 นาที", "level": "OK", "note": ""},
    ]
    text = format_weekly_report(snap)["text"]
    assert "ดู: ข่าวเก่า 40.0 ชม." in text
    assert "ไม่ต้องทำอะไร" not in text


def test_weekly_report_kill_switch_on() -> None:
    snap = _quiet_week()
    snap["kill_switch"] = True
    text = format_weekly_report(snap)["text"]
    assert "ระบบ: kill switch เปิด [ต้องทำ]" in text
    assert "ต้องทำ: kill switch เปิด" in text
    assert "ไม่ต้องทำอะไร" not in text


def test_weekly_send_soft_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "jarvise_notify.weekly.collect_weekly_snapshot",
        lambda **_k: _quiet_week(),
    )
    monkeypatch.setattr("jarvise_notify.weekly.notify_configured", lambda: True)
    monkeypatch.setattr("jarvise_notify.weekly.send_telegram_message", lambda _text: False)
    report = build_weekly_report(send=True)
    assert report["ok"] is True
    assert report["telegram_sent"] is False
    assert report["telegram_error"] == "send_failed"


def test_ingest_age_uses_health_blob_when_ingest_has_no_at_ms() -> None:
    now = 1_700_000_000_000
    fresh = _health_items(
        {
            "ingest": {"ok": True, "paper_only": True, "exit_code": 0, "timeframes": {}},
            "ingest_health": {"ok": True, "at_ms": now - 5 * 60_000},
            "digest_age_hours": 2.0,
            "risk_monitor": {"ok": True, "at_ms": now - 5 * 60_000},
        },
        now_ms=now,
        freshness={"market_safety_max_age_min": 90, "risk_monitor_stale_after_min": 35},
    )
    ingest = fresh[0]
    assert ingest["level"] == "OK"
    stale = _health_items(
        {
            "ingest": {"ok": True, "exit_code": 0},
            "ingest_health": {"ok": True, "at_ms": now - 200 * 60_000},
            "digest_age_hours": 2.0,
            "risk_monitor": {"ok": True, "at_ms": now - 5 * 60_000},
        },
        now_ms=now,
        freshness={"market_safety_max_age_min": 90, "risk_monitor_stale_after_min": 35},
    )
    assert stale[0]["level"] == "ดู"
