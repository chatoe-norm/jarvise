"""Menu wording — new labels, legacy self-heal, toggle style, Quick-first layout."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis

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
    handle_menu_text(conn, BTN_TOGGLE, now_ms=day, send=send)
    assert ensure_settings(conn)["enabled"] is False
    assert "style" not in _toggle_btn(sent[-1]["reply_markup"])
    handle_menu_text(conn, BTN_TOGGLE, now_ms=day, send=send)
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


def test_legacy_toggle_still_toggles_without_extra_refresh(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "legacy_tog.db")
    sent, send = _send_capture()
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    assert ensure_settings(conn)["enabled"] is True
    out = handle_menu_text(conn, LEGACY_BTN_TOGGLE, now_ms=day, send=send)
    assert out.get("kind") == "toggle"
    assert ensure_settings(conn)["enabled"] is False
    assert out.get("menu_refreshed") is not True
    assert all(s["text"] != "เมนูอัปเดตแล้ว" for s in sent)
    assert "style" not in _toggle_btn(sent[-1]["reply_markup"])
    conn.close()


def test_parse_quick_command_ignores_menu_labels() -> None:
    assert parse_quick_command(BTN_ANYTHING) is None
    assert parse_quick_command(BTN_TOGGLE) is None
