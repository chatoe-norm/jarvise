"""Menu wording — new labels, legacy self-heal, toggle style, Quick-first layout."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.assist_helpers import assist_toggle_confirm
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis

from jarvise_assist import listen as listen_mod
from jarvise_assist.keyboard import (
    BTN_ANYTHING,
    BTN_POSITIONS,
    BTN_QUICK,
    BTN_RISK_PREFIX,
    BTN_TOGGLE,
    BTN_WEEKLY,
    LEGACY_BTN_ANYTHING,
    LEGACY_BTN_EXPLORE,
    LEGACY_BTN_RISK_PREFIX,
    LEGACY_BTN_TOGGLE,
    reply_keyboard,
    reply_keyboard_for,
    risk_button_label,
)
from jarvise_assist.menu import (
    _apply_or_confirm_risk,
    handle_menu_text,
    handle_risk_callback,
    match_menu_label,
)
from jarvise_assist.quick_card import parse_quick_command
from jarvise_assist.store import ensure_settings, write_settings
from jarvise_ingest.db import open_db


def _send_capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int:
        mid = len(sent) + 1
        sent.append({"text": text, "reply_markup": reply_markup, "message_id": mid})
        return mid

    return sent, send


def _toggle_btn(markup: dict[str, Any]) -> dict[str, Any]:
    return markup["keyboard"][2][1]


def test_reply_keyboard_enabled_true_exact() -> None:
    assert reply_keyboard(1.0, enabled=True) == {
        "keyboard": [
            [{"text": BTN_POSITIONS}, {"text": BTN_ANYTHING}],
            [{"text": BTN_WEEKLY}, {"text": "⚙️ ความเสี่ยงต่อไม้ (🟢 1%)"}],
            [{"text": BTN_QUICK}, {"text": BTN_TOGGLE, "style": "success"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }
    risk_btn = reply_keyboard(1.0, enabled=True)["keyboard"][1][1]
    assert "style" not in risk_btn
    texts = [btn["text"] for row in reply_keyboard(1.0, enabled=True)["keyboard"] for btn in row]
    assert all("เหรียญทดลอง" not in t for t in texts)


def test_reply_keyboard_enabled_false_exact() -> None:
    kb = reply_keyboard(1.0, enabled=False)
    assert kb == {
        "keyboard": [
            [{"text": BTN_POSITIONS}, {"text": BTN_ANYTHING}],
            [{"text": BTN_WEEKLY}, {"text": "⚙️ ความเสี่ยงต่อไม้ (🟢 1%)"}],
            [{"text": BTN_QUICK}, {"text": BTN_TOGGLE}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }
    assert "style" not in _toggle_btn(kb)
    assert "style" not in kb["keyboard"][1][1]
    texts = [btn["text"] for row in kb["keyboard"] for btn in row]
    assert all("เหรียญทดลอง" not in t for t in texts)


def test_risk_button_label_formatting() -> None:
    assert risk_button_label(1.0) == "⚙️ ความเสี่ยงต่อไม้ (🟢 1%)"
    assert risk_button_label(1.5) == "⚙️ ความเสี่ยงต่อไม้ (🟢 1.5%)"


def test_match_menu_label_new_and_legacy() -> None:
    assert match_menu_label(BTN_POSITIONS) == "positions"
    assert match_menu_label(BTN_ANYTHING) == "anything"
    assert match_menu_label(BTN_WEEKLY) == "weekly"
    assert match_menu_label(risk_button_label(1.0)) == "risk"
    assert match_menu_label(f"{BTN_RISK_PREFIX} (🟢 2%)") == "risk"
    assert match_menu_label(BTN_TOGGLE) == "toggle"
    assert match_menu_label(BTN_QUICK) == "quick"
    assert match_menu_label(LEGACY_BTN_ANYTHING) == "anything"
    assert match_menu_label(f"{LEGACY_BTN_RISK_PREFIX} (ตอนนี้ 2%)") == "risk"
    assert match_menu_label(LEGACY_BTN_TOGGLE) == "toggle"
    assert match_menu_label(LEGACY_BTN_EXPLORE) == "explore"


def test_toggle_reply_markup_style_matches_enabled(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tog_style.db")
    sent, send = _send_capture()
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    assert ensure_settings(conn)["enabled"] is True
    assist_toggle_confirm(conn, now_ms=day, send=send)
    assert ensure_settings(conn)["enabled"] is False
    assert "style" not in _toggle_btn(sent[-1]["reply_markup"])
    assist_toggle_confirm(conn, now_ms=day, send=send)
    assert ensure_settings(conn)["enabled"] is True
    assert _toggle_btn(sent[-1]["reply_markup"])["style"] == "success"
    conn.close()


def test_risk_save_markup_toggle_success_when_enabled(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "risk_style.db")
    sent, send = _send_capture()
    write_settings(conn, enabled=True, risk_pct=1.0)
    out = _apply_or_confirm_risk(conn, 1.0, send=send)
    assert out.get("ok") is True
    assert out.get("risk_pct") == 1.0
    markup = sent[-1]["reply_markup"]
    assert markup == reply_keyboard_for(ensure_settings(conn))
    assert _toggle_btn(markup)["style"] == "success"
    conn.close()


def test_legacy_anything_sends_menu_refresh(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "legacy_any.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    until = day + 3_600_000
    write_settings(conn, paused_until_ms=until)
    sent, send = _send_capture()
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")})
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=day - 60_000)
    out = handle_menu_text(conn, LEGACY_BTN_ANYTHING, futures=fake, now_ms=day, send=send)
    assert out.get("menu_refreshed") is True
    assert any(s["text"] == "เมนูอัปเดตแล้ว" for s in sent)
    refresh = next(s for s in sent if s["text"] == "เมนูอัปเดตแล้ว")
    assert refresh["reply_markup"] == reply_keyboard_for(ensure_settings(conn))
    conn.close()


def test_new_anything_does_not_refresh(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "new_any.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    until = day + 3_600_000
    write_settings(conn, paused_until_ms=until)
    sent, send = _send_capture()
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")})
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=day - 60_000)
    out = handle_menu_text(conn, BTN_ANYTHING, futures=fake, now_ms=day, send=send)
    assert out.get("menu_refreshed") is not True
    assert all(s["text"] != "เมนูอัปเดตแล้ว" for s in sent)
    conn.close()


def test_legacy_toggle_asks_confirm_without_extra_refresh(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "legacy_tog.db")
    sent, send = _send_capture()
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    assert ensure_settings(conn)["enabled"] is True
    out = handle_menu_text(conn, LEGACY_BTN_TOGGLE, now_ms=day, send=send)
    assert out.get("kind") == "toggle"
    assert out.get("confirm") is True
    assert ensure_settings(conn)["enabled"] is True
    assert out.get("menu_refreshed") is not True
    assert all(s["text"] != "เมนูอัปเดตแล้ว" for s in sent)
    conn.close()


def test_parse_quick_command_ignores_menu_labels() -> None:
    assert parse_quick_command(BTN_ANYTHING) is None
    assert parse_quick_command(BTN_TOGGLE) is None


@pytest.mark.parametrize(
    "label",
    [
        LEGACY_BTN_ANYTHING,
        f"{LEGACY_BTN_RISK_PREFIX} (ตอนนี้ 1%)",
        LEGACY_BTN_EXPLORE,
    ],
)
@pytest.mark.parametrize("enabled", [True, False])
def test_legacy_refresh_keeps_enabled_and_pause(tmp_path: Path, label: str, enabled: bool) -> None:
    conn = open_db(tmp_path / f"legacy_keep_{int(enabled)}_{hash(label) & 0xFFFF}.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    write_settings(conn, enabled=enabled, paused_until_ms=day + 3_600_000)
    sent, send = _send_capture()
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")})
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=day - 60_000)
    out = handle_menu_text(conn, label, futures=fake, now_ms=day, send=send)
    assert out.get("menu_refreshed") is True
    assert sum(1 for s in sent if s["text"] == "เมนูอัปเดตแล้ว") == 1
    settings = ensure_settings(conn)
    assert settings["enabled"] is enabled
    assert settings["paused_until_ms"] == day + 3_600_000
    refresh = next(s for s in sent if s["text"] == "เมนูอัปเดตแล้ว")
    toggle = _toggle_btn(refresh["reply_markup"])
    if enabled:
        assert toggle["style"] == "success"
    else:
        assert "style" not in toggle
    conn.close()


def test_risk_save_markup_plain_when_assistant_off(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "risk_off.db")
    sent, send = _send_capture()
    write_settings(conn, enabled=False, risk_pct=1.0)

    out = _apply_or_confirm_risk(conn, 0.5, send=send)
    assert out.get("ok") is True
    assert "style" not in _toggle_btn(sent[-1]["reply_markup"])
    assert ensure_settings(conn)["enabled"] is False

    out_yes = handle_risk_callback(conn, "risk_yes:2.5", send=send)
    assert out_yes.get("ok") is True
    assert out_yes.get("risk_pct") == 2.5
    assert "style" not in _toggle_btn(sent[-1]["reply_markup"])
    assert ensure_settings(conn)["enabled"] is False
    conn.close()


def test_ensure_keyboard_toggle_style_follows_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []

    def capture(text: str, *, reply_markup: dict[str, Any] | None = None) -> int:
        sent.append({"text": text, "reply_markup": reply_markup})
        return 1

    monkeypatch.setattr("jarvise_assist.listen.send_message", capture)

    conn_off = open_db(tmp_path / "kb_off.db")
    write_settings(conn_off, enabled=False)
    listen_mod._ensure_keyboard(conn_off)
    assert "style" not in _toggle_btn(sent[-1]["reply_markup"])
    conn_off.close()

    sent.clear()
    conn_on = open_db(tmp_path / "kb_on.db")
    write_settings(conn_on, enabled=True)
    listen_mod._ensure_keyboard(conn_on)
    assert _toggle_btn(sent[-1]["reply_markup"])["style"] == "success"
    conn_on.close()


def test_menu_clamp_markup_plain_when_assistant_off(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "clamp_off.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    write_settings(conn, enabled=False, risk_pct=5.0)
    sent, send = _send_capture()
    handle_menu_text(conn, BTN_POSITIONS, futures=FakeFutures(), now_ms=day, send=send)
    clamp = next(s for s in sent if s.get("reply_markup") and "keyboard" in s["reply_markup"])
    assert "ปรับเป็น 3%" in clamp["text"]
    assert "style" not in _toggle_btn(clamp["reply_markup"])
    conn.close()


def test_risk_confirm_yes_markup_toggle_success_when_enabled(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "risk_on.db")
    sent, send = _send_capture()
    write_settings(conn, enabled=True, risk_pct=1.0)

    confirm = _apply_or_confirm_risk(conn, 2.5, send=send)
    assert confirm.get("confirm") is True

    out = handle_risk_callback(conn, "risk_yes:2.5", send=send)
    assert out == {"ok": True, "risk_pct": 2.5}
    assert sent[-1]["text"] == "บันทึกความเสี่ยง 2.5% แล้ว"
    markup = sent[-1]["reply_markup"]
    assert markup == reply_keyboard_for(ensure_settings(conn))
    assert _toggle_btn(markup)["style"] == "success"
    assert markup["keyboard"][1][1]["text"] == risk_button_label(2.5)
    assert ensure_settings(conn)["enabled"] is True
    conn.close()


def test_menu_clamp_markup_toggle_success_when_enabled(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "clamp_on.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    write_settings(conn, enabled=True, risk_pct=5.0)
    sent, send = _send_capture()
    handle_menu_text(conn, BTN_POSITIONS, futures=FakeFutures(), now_ms=day, send=send)
    clamp = next(s for s in sent if s.get("reply_markup") and "keyboard" in s["reply_markup"])
    assert clamp["text"] == "ความเสี่ยงที่บันทึกไว้เกิน 3% — ปรับเป็น 3% แล้ว"
    assert _toggle_btn(clamp["reply_markup"])["style"] == "success"
    assert clamp["reply_markup"]["keyboard"][1][1]["text"] == risk_button_label(3.0)
    assert ensure_settings(conn)["risk_pct"] == 3.0
    conn.close()
