"""Weekly report text. Read-only snapshots, no ledger writes."""

from jarvise_notify.weekly import SHADOW_WINDOW_ACTION, build_weekly_report, format_weekly_report


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
