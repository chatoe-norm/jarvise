"""QC1a golden tests for the Quick Thai error catalogue."""

from __future__ import annotations

import ast
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_quick_card import QuickFake, _seed_settings
from tests.test_assist_quick_guards import _insert_quick_trade, _pos, _stop_order
from tests.test_assist_quick_m1a1_round1 import _run_qk
from tests.test_assist_quick_p1a_minlot import BUG3_CAP15, BUG3_SHORT

from jarvise_assist import quick_m1a1
from jarvise_assist.quick import (
    MSG_QUICK_CONFIRM_STALE,
    QUICK_USAGE,
    get_quick_settings,
    handle_quick_callback,
    handle_quick_setting_text,
    parse_quick_setting_command,
    write_quick_settings,
)
from jarvise_assist.quick_card import (
    QUICK_REGISTER_PREFIX,
    REFUSAL,
    _fail,
    build_quick_card,
    handle_quick_register_callback,
    handle_quick_text,
    parse_quick_command,
    stop_far_refusal_text,
)
from jarvise_assist.quick_errors import (
    e_confirm_stale,
    e_confirm_stale_ttl,
    e_draft_stop_pct_range,
    e_draft_stop_token,
    e_parse_count,
    e_parse_side,
    e_parse_stop,
    e_parse_symbol,
    e_portfolio_cap,
    e_quick_cap,
    e_reg,
    e_setting_reject,
    e_setting_risk_over_cap,
    e_stop_far,
    e_stop_side,
)
from jarvise_assist.quick_m1a1 import (
    STOP_STEP_HINT,
    _registration_refusal_text,
    handle_quick_draft_text,
)
from jarvise_assist.quick_minlot import fmt_pct
from jarvise_ingest.db import open_db

NOW = _ms_bangkok(2026, 11, 23, 15, 0)
SRC = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"

G2 = (
    "❌ stop ห่าง 0.07% แคบกว่าขั้นต่ำ ≈ 0.20% (risk 1% ÷ 5x) · "
    "ค่าธรรมเนียมไป-กลับ ≈ 1.71R · "
    "👉 ขยาย stop ≥ 0.20% (≥ 59448.8) หรือลดความเสี่ยงในเมนู ⚡ Quick"
)
G2L = (
    "❌ stop ห่าง 0.07% แคบกว่าขั้นต่ำ ≈ 0.20% (risk 1% ÷ 5x) · "
    "ค่าธรรมเนียมไป-กลับ ≈ 1.71R · "
    "👉 ขยาย stop ≥ 0.20% (≤ 59211.4) หรือลดความเสี่ยงในเมนู ⚡ Quick"
)
G2B_TIGHT = (
    "❌ stop ห่าง 0.07% แคบกว่าขั้นต่ำ ≈ 0.20% (risk 1% ÷ 5x) · "
    "ค่าธรรมเนียมไป-กลับ ≈ 1.71R · "
    "👉 ขยาย stop ≥ 0.20% (≥ 59448.8) หรือลดความเสี่ยงในเมนู ⚡ Quick"
)
G2C = (
    "❌ stop ห่าง 0.07% แคบกว่าขั้นต่ำ ≈ 0.20% (risk 1% ÷ 5x) · "
    "ค่าธรรมเนียมไป-กลับ ≈ 1.71R · "
    "👉 ขยาย stop ≥ 0.20% (≥ 59448.8) หรือลดความเสี่ยงในเมนู ⚡ Quick"
)
G2D = (
    "❌ stop ห่าง 0.03% แคบกว่าขั้นต่ำ ≈ 0.16% (risk 0.76% ÷ 5x) · "
    "ค่าธรรมเนียมไป-กลับ ≈ 4.00R · "
    "👉 ขยาย stop ≥ 0.16% (≥ 59425.1) หรือลดความเสี่ยงในเมนู ⚡ Quick"
)
G3 = (
    "❌ เกินเพดาน Quick รวม 1% = 1.42 USDT · เปิดอยู่ 0.7 + ไม้นี้ 0.89 = 1.59 · "
    "เหลือ 0.72 USDT (0.50%) · "
    "👉 ตั้งความเสี่ยง 0.5% ในเมนู ⚡ Quick (ไม้นี้จะเสี่ยง 0.59 USDT) หรือปิดไม้/รอทะเบียนหมดอายุ"
)
G3B = (
    "❌ เกินเพดาน Quick รวม 1% = 1.42 USDT · เปิดอยู่ 0.83 + ไม้นี้ 0.89 = 1.72 · "
    "เหลือ 0.59 USDT (0.41%) · "
    "👉 ตั้งความเสี่ยง 0.5% ในเมนู ⚡ Quick (ไม้นี้จะเสี่ยง 0.59 USDT) หรือปิดไม้/รอทะเบียนหมดอายุ"
)
G3C = (
    "❌ เกินเพดาน Quick รวม 1% = 1.42 USDT · เปิดอยู่ 0.7 + ไม้นี้ 0.89 = 1.59 · "
    "เหลือ 0.72 USDT (0.50%) · 👉 ปิดไม้หรือรอทะเบียนหมดอายุ"
)
G4 = (
    "❌ เกินเพดาน Quick รวม 1% = 1.42 USDT · เปิดอยู่ 1.22 + ไม้นี้ 0.89 = 2.11 · "
    "เหลือ 0.2 USDT (0.14%) · 👉 ปิดไม้หรือรอทะเบียนหมดอายุ"
)
G4_WIDE = (
    "❌ เกินเพดาน Quick รวม 1% = 1.42 USDT · เปิดอยู่ 0.7 + ไม้นี้ 0.89 = 1.59 · "
    "เหลือ 0.72 USDT (0.50%) · 👉 ปิดไม้หรือรอทะเบียนหมดอายุ"
)
G4B = (
    "❌ เกินเพดาน Quick รวม 1% = 1.42 USDT · เปิดอยู่ 1.5 + ไม้นี้ 0.89 = 2.39 · "
    "เหลือ 0 USDT (0.00%) · 👉 ปิดไม้หรือรอทะเบียนหมดอายุ"
)
G5 = (
    "❌ margin ไม่พอ — ขนาดตาม risk 0.004 BTC ≈ 248.4 USDT "
    "ต้องใช้ ≈ 49.68 USDT ที่ 5x มี 43.32 USDT · 👉 พิมพ์ q ใหม่ บอทจะเสนอขนาดที่ margin พอ"
)
G6 = (
    "❌ ไม้เล็กกว่าขั้นต่ำ — ขนาดตาม risk 0.003 BTC ≈ 177.99 USDT "
    "ต่ำกว่าขั้นต่ำ 0.007 BTC / 400 USDT · "
    "👉 พิมพ์ q ใหม่ บอทจะเสนอขนาดขั้นต่ำถ้าความเสี่ยงไม่เกินเพดาน"
)
G7 = (
    "❌ ขนาดตาม risk ปัดเป็น 0 — งบ 1.42 USDT ÷ ระยะ stop 2669.9 (4.50%) "
    "= 0.00053 BTC ต่ำกว่า 1 step 0.001 BTC · 👉 พิมพ์ q ใหม่เพื่อดูขนาดขั้นต่ำและความเสี่ยงจริง"
)
G7B = "❌ คำนวณขนาดไม่ได้ — wallet 0 USDT · risk 1% · 👉 ตรวจยอดใน Futures"
G8 = "❌ SHORT ต้องตั้ง stop เหนือราคา — stop 59100.0 ต่ำกว่า mark 59330.1 · 👉 ใส่ราคา > 59330.1 หรือเป็น % เช่น 0.4%"
G8B = "❌ LONG ต้องตั้ง stop ใต้ราคา — stop 59500.0 สูงกว่า mark 59330.1 · 👉 ใส่ราคา < 59330.1 หรือเป็น % เช่น 0.4%"
G8C = "❌ SHORT ต้องตั้ง stop เหนือราคา — stop 40000.0 ต่ำกว่า mark 59330.1 · 👉 ใส่ราคา > 59330.1 หรือเป็น % เช่น 0.4%"
G9 = "❌ stop ห่าง 24.73% เกิน 20% · ถ้าหมายถึงเปอร์เซ็นต์ให้ใส่ % (เช่น 0.5%)"
G9L = "❌ stop ห่าง 24.15% เกิน 20% · ถ้าหมายถึงเปอร์เซ็นต์ให้ใส่ % (เช่น 0.5%)"
G9B = "❌ stop ห่าง 20.01% เกิน 20% · ถ้าหมายถึงเปอร์เซ็นต์ให้ใส่ % (เช่น 0.5%)"
G10 = "❌ BTCUSDT มีทะเบียน quick ค้างอยู่ (หมดอายุ 23:51) · 👉 รอหมดอายุหรือเปิดไม้นั้นใน Binance"
G10C = "❌ BTCUSDT มีทะเบียน quick ที่หมดเวลาแล้ว รอระบบปิด · 👉 ดูสถานะในเมนู ⚡ Quick"
G10B = "❌ BTCUSDT มีไม้ quick เปิดอยู่ (เข้า 59310) · 👉 ปิดไม้นั้นก่อนเปิดไม้ใหม่บนเหรียญนี้"
G11 = (
    "❌ เกินเพดานความเสี่ยงรวม (หลัก + quick ≤ 3%) = 4.26 USDT · "
    "หลัก 3.1 + quick 0.5 + ไม้นี้ 1.4 = 5 · 👉 ลดความเสี่ยงในเมนู ⚡ Quick หรือปิดไม้"
)

_RAW_CODES = (
    "quick_open",
    "main_position",
    "in_flight",
    "pending_order",
    "contract",
    "read_failed",
    "stop_wrong_side",
    "below_min_notional",
    "size_failed",
    "quick_cap",
    "portfolio_cap",
    "margin_cap",
    "leverage_cap",
    "card_expired",
    "bad_data",
    "stop_far",
    "week_loss_pause",
    "risk_over_1pct",
    "leverage_over_5x",
    "qty_non_positive",
    "invalid_side",
    "open_registration",
    "open_quick_trade",
    "open_assist_watch",
    "live_position",
    "same_side_cap",
    "no_balance",
    "bad_input",
)


def _conn(tmp_path: Path, name: str) -> Any:
    conn = open_db(tmp_path / name)
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0, cap_pct=1.0)
    return conn


def _fake(**kw: Any) -> QuickFake:
    defaults: dict[str, Any] = dict(
        balance=Decimal("142"),
        available=Decimal("142"),
        marks={
            "BTCUSDT": Decimal("59330.1"),
            "ETHUSDT": Decimal("3000"),
            "SOLUSDT": Decimal("150"),
        },
        ticks={"BTCUSDT": Decimal("0.1"), "ETHUSDT": Decimal("0.01"), "SOLUSDT": Decimal("0.01")},
        steps={"BTCUSDT": Decimal("0.001")},
        min_qty={"BTCUSDT": Decimal("0.001")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    defaults.update(kw)
    return QuickFake(**defaults)


def _sent() -> tuple[list[dict[str, Any]], Any]:
    box: list[dict[str, Any]] = []

    def send(text: str, reply_markup: Any = None, **kw: Any) -> int:
        box.append({"text": text, "reply_markup": reply_markup, **kw})
        return 1

    return box, send


def _no_codes(text: str) -> None:
    for code in _RAW_CODES:
        assert code not in text, code
    assert "ลองใหม่" not in text


def _cards(conn: Any) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM assist_quick_card").fetchone()[0])


def _typed(conn: Any, text: str, fake: QuickFake) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sent, send = _sent()
    out = handle_quick_text(conn, text, futures=fake, send=send, now_ms=NOW)
    assert out is not None
    assert len(sent) == 1
    _no_codes(sent[0]["text"])
    return out, sent


def _open_eth(conn: Any, risk: float) -> None:
    tid = _insert_quick_trade(
        conn,
        symbol="ETHUSDT",
        side="short",
        now_ms=NOW,
        entry_avg=100.0,
        planned_stop=90.0,
        entry_qty=0.01,
    )
    conn.execute("UPDATE assist_quick_trade SET risk_usdt=? WHERE id=?", (risk, tid))
    conn.commit()


def _open_reg(conn: Any, *, symbol: str, expires_at_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, updated_at_ms
        ) VALUES ('reg-qc', ?, 'short', 59330.1, 59600, 0.001, ?, ?, 'open', ?)
        """,
        (symbol, NOW, expires_at_ms, NOW),
    )
    conn.commit()


def _draft(conn: Any, *, status: str, symbol: str | None, side: str = "short") -> None:
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, status, created_at_ms, updated_at_ms
        ) VALUES ('draft1', ?, ?, ?, ?, ?)
        """,
        (side, symbol, status, NOW, NOW),
    )
    conn.commit()


def test_g1_bug3_short_and_cap15(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = _conn(tmp_path, "g1.db")
    out, sent = _typed(conn, "q BTC short 62000", _fake())
    assert out["reason"] == "risk_over_1pct"
    assert sent[0]["text"] == BUG3_SHORT
    assert sent[0]["reply_markup"] is None
    assert _cards(conn) == 0
    write_quick_settings(conn, cap_pct=1.5)
    card = build_quick_card(
        conn,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62000"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert card["text"] == BUG3_CAP15
    conn.close()


def test_g2_family_leverage_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = _conn(tmp_path, "g2.db")
    out, sent = _typed(conn, "q BTC short 0.07%", _fake())
    assert out["reason"] == "leverage_cap"
    assert sent[0]["text"] == G2
    assert sent[0]["reply_markup"] is None
    assert _cards(conn) == 0

    out_l, sent_l = _typed(conn, "q BTC long 0.07%", _fake())
    assert out_l["reason"] == "leverage_cap"
    assert sent_l[0]["text"] == G2L

    ok = build_quick_card(
        conn,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.08"),
        now_ms=NOW,
        allow_offer=True,
        enforce_stop_far=True,
    )
    assert ok["ok"] is True
    assert ok["qty"] == Decimal("0.011")
    assert int(ok["leverage"]) == 5

    tight = build_quick_card(
        conn,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.07"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert tight["reason"] == "leverage_cap"
    assert tight["text"] == G2B_TIGHT

    edge = build_quick_card(
        conn,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("59371.7"),
        now_ms=NOW,
        allow_offer=True,
        enforce_stop_far=True,
    )
    assert edge["reason"] == "leverage_cap"
    assert edge["text"] == G2C

    write_quick_settings(conn, risk_pct=0.76)
    manual = build_quick_card(
        conn,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.03"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert manual["reason"] == "leverage_cap"
    assert manual["text"] == G2D
    conn.close()


def test_g3_g4_quick_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = _conn(tmp_path, "g3.db")
    _open_eth(conn, 0.70)
    out, sent = _typed(conn, "q BTC short 0.5%", _fake())
    assert out["reason"] == "quick_cap"
    assert sent[0]["text"] == G3
    assert sent[0]["reply_markup"] is None
    assert _cards(conn) == 0

    conn_b = _conn(tmp_path, "g3b.db")
    _open_eth(conn_b, 0.8266)
    card_b = build_quick_card(
        conn_b,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.5"),
        now_ms=NOW,
        allow_offer=True,
        enforce_stop_far=True,
    )
    assert card_b["reason"] == "quick_cap"
    assert card_b["text"] == G3B

    conn_c = _conn(tmp_path, "g3c.db")
    _open_eth(conn_c, 0.70)
    sol = _pos("SOLUSDT", amt="1.6", entry="100", mark="100")
    stop = _stop_order("SOLUSDT", stop="99", qty="1.6")
    card_c = build_quick_card(
        conn_c,
        _fake(positions=[sol], orders=[stop]),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.5"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert card_c["reason"] == "quick_cap"
    assert card_c["text"] == G3C
    assert "น้อยกว่า" not in card_c["text"]

    conn_4 = _conn(tmp_path, "g4.db")
    _open_eth(conn_4, 1.22)
    card_4 = build_quick_card(
        conn_4,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.5"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert card_4["text"] == G4
    assert "น้อยกว่า" not in card_4["text"]

    conn_w = _conn(tmp_path, "g4w.db")
    _open_eth(conn_w, 0.70)
    wide = build_quick_card(
        conn_w,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("1.5"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert wide["reason"] == "quick_cap"
    assert wide["text"] == G4_WIDE
    assert "(0 USDT)" not in wide["text"]
    assert "น้อยกว่า" not in wide["text"]

    conn_n = _conn(tmp_path, "g4b.db")
    _open_eth(conn_n, 1.50)
    neg = build_quick_card(
        conn_n,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.5"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert neg["text"] == G4B
    assert "(-" not in neg["text"]
    for conn_i in (conn, conn_b, conn_c, conn_4, conn_w, conn_n):
        conn_i.close()


def test_g5_g6_g7_legacy_and_wallet_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = _conn(tmp_path, "g5.db")
    legacy = build_quick_card(
        conn,
        _fake(
            balance=Decimal("120"),
            available=Decimal("43.32"),
            marks={"BTCUSDT": Decimal("62100")},
        ),
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62300"),
        now_ms=NOW,
        allow_offer=False,
    )
    assert legacy["reason"] == "margin_cap"
    assert legacy["text"] == G5

    # Fee-aware 0.25% floors to 0 (size_failed); use 1% + higher mins for below_min.
    write_quick_settings(conn, risk_pct=1.0)
    below = build_quick_card(
        conn,
        _fake(
            min_notional={"BTCUSDT": Decimal("400")},
            min_qty={"BTCUSDT": Decimal("0.002")},
        ),
        symbol="BTCUSDT",
        side="short",
        stop_pct=Decimal("0.5"),
        now_ms=NOW,
        allow_offer=False,
    )
    assert below["reason"] == "below_min_notional"
    assert below["text"] == G6

    write_quick_settings(conn, risk_pct=1.0)
    zero = build_quick_card(
        conn,
        _fake(),
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62000"),
        now_ms=NOW,
        allow_offer=False,
    )
    assert zero["reason"] == "size_failed"
    assert zero["text"] == G7

    out, sent = _typed(conn, "q BTC short 62300", _fake(balance=Decimal("0"), available=Decimal("0")))
    assert out["reason"] == "size_failed"
    assert sent[0]["text"] == G7B
    assert _cards(conn) == 0
    conn.close()


def test_g8_g9_side_and_stop_far(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = _conn(tmp_path, "g8.db")
    cases = (
        ("q BTC short 59100", "stop_wrong_side", G8),
        ("q BTC long 59500", "stop_wrong_side", G8B),
        ("q BTC short 40000", "stop_wrong_side", G8C),
        ("q BTC short 74000", "stop_far", G9),
        ("q BTC long 45000", "stop_far", G9L),
        ("q BTC short 71197", "stop_far", G9B),
    )
    for text, reason, want in cases:
        out, sent = _typed(conn, text, _fake())
        assert out["reason"] == reason
        assert sent[0]["text"] == want
        assert sent[0]["reply_markup"] is None
    assert _cards(conn) == 0
    conn.close()


def test_g10_quick_open_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    exp = _ms_bangkok(2026, 11, 23, 23, 51)
    conn = _conn(tmp_path, "g10.db")
    _open_reg(conn, symbol="BTCUSDT", expires_at_ms=exp)
    out, sent = _typed(conn, "q BTC short 0.5%", _fake())
    assert out["reason"] == "quick_open"
    assert sent[0]["text"] == G10

    conn_c = _conn(tmp_path, "g10c.db")
    _open_reg(conn_c, symbol="BTCUSDT", expires_at_ms=NOW - 60_000)
    out_c, sent_c = _typed(conn_c, "q BTC short 0.5%", _fake())
    assert out_c["reason"] == "quick_open"
    assert sent_c[0]["text"] == G10C

    conn_b = _conn(tmp_path, "g10b.db")
    _insert_quick_trade(conn_b, symbol="BTCUSDT", side="short", now_ms=NOW, entry_avg=59310.0)
    out_b, sent_b = _typed(conn_b, "q BTC short 0.5%", _fake())
    assert out_b["reason"] == "quick_open"
    assert sent_b[0]["text"] == G10B
    for item in (conn, conn_c, conn_b):
        item.close()


def test_g11_portfolio_template() -> None:
    assert (
        e_portfolio_cap(
            Decimal("3"),
            Decimal("142"),
            Decimal("3.10"),
            Decimal("0.50"),
            Decimal("1.40"),
        )
        == G11
    )


DRAFT_STOP_HUGE_FAR = "❌ stop ห่าง > 999999.99% เกิน 20% · ถ้าหมายถึงเปอร์เซ็นต์ให้ใส่ % (เช่น 0.5%)"
DRAFT_STOP_HUGE_PCT_PREFIX = "❌ stop > 999999.99% ต้องมากกว่า 0 และน้อยกว่า 20%"


def test_catalogue_display_rounding() -> None:
    assert e_quick_cap(
        cap_pct=Decimal("1"),
        balance=Decimal("142"),
        open_usdt=Decimal("0.2334"),
        new_usdt=Decimal("1.1868"),
        preset=None,
    ) == (
        "❌ เกินเพดาน Quick รวม 1% = 1.42 USDT · เปิดอยู่ 0.23 + ไม้นี้ 1.19 = 1.43 · "
        "เหลือ 1.18 USDT (0.83%) · 👉 ปิดไม้หรือรอทะเบียนหมดอายุ"
    )
    assert e_portfolio_cap(
        Decimal("2"),
        Decimal("142"),
        Decimal("1.0"),
        Decimal("0.65"),
        Decimal("1.1901"),
    ) == (
        "❌ เกินเพดานความเสี่ยงรวม (หลัก + quick ≤ 2%) = 2.84 USDT · "
        "หลัก 1 + quick 0.65 + ไม้นี้ 1.19 = 2.85 · "
        "👉 ลดความเสี่ยงในเมนู ⚡ Quick หรือปิดไม้"
    )
    assert e_stop_side("short", Decimal("59330.1"), Decimal("59330.14"), Decimal("0.1")) == (
        "❌ SHORT ต้องตั้ง stop เหนือราคา — stop 59330.1 ต่ำกว่า mark 59330.14 · "
        "👉 ใส่ราคา > 59330.14 หรือเป็น % เช่น 0.4%"
    )
    assert e_stop_side("long", Decimal("59330.2"), Decimal("59330.16"), Decimal("0.1")) == (
        "❌ LONG ต้องตั้ง stop ใต้ราคา — stop 59330.2 สูงกว่า mark 59330.16 · "
        "👉 ใส่ราคา < 59330.16 หรือเป็น % เช่น 0.4%"
    )
    assert e_stop_far(Decimal("1000000")) == DRAFT_STOP_HUGE_FAR


def test_g12_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    assert parse_quick_command("q BTC short") == {"error": "usage", "field": "count", "count": 2}
    assert parse_quick_command("q BTC short 62000 r=0.5") == {
        "error": "usage",
        "field": "count",
        "count": 4,
    }
    assert parse_quick_command("q BTC up 62000")["token"] == "up"
    assert parse_quick_command("q BTC short abc")["token"] == "abc"
    assert parse_quick_command("q BTC short 25%")["token"] == "25%"
    comma = parse_quick_command("q btc long 1,2,3x")
    assert comma is not None and comma["token"] == "1,2,3x"
    stripped = parse_quick_command("q btc long 1,2,3")
    assert stripped == {"symbol": "BTCUSDT", "side": "long", "stop": Decimal("123")}
    ok = parse_quick_command("q btc long 67000")
    assert ok == {"symbol": "BTCUSDT", "side": "long", "stop": Decimal("67000")}
    assert "field" not in (ok or {})

    conn = _conn(tmp_path, "g12.db")
    expects = {
        "q BTC short": e_parse_count(2) + "\n" + QUICK_USAGE,
        "q BTC short 62000 r=0.5": e_parse_count(4) + "\n" + QUICK_USAGE,
        "q BTC up 62000": e_parse_side("up") + "\n" + QUICK_USAGE,
        "q BTC short abc": e_parse_stop("abc") + "\n" + QUICK_USAGE,
        "q BTC short 25%": e_parse_stop("25%") + "\n" + QUICK_USAGE,
    }
    for text, want in expects.items():
        out, sent = _typed(conn, text, _fake())
        assert out["reason"] == "usage"
        assert sent[0]["text"] == want
    out_sym, sent_sym = _typed(conn, "q @@@ long 1", _fake())
    assert out_sym["reason"] == "usage"
    assert sent_sym[0]["text"] == e_parse_symbol("@@@") + "\n" + QUICK_USAGE
    conn.close()


def test_g13_registration_codes(caplog: pytest.LogCaptureFixture) -> None:
    qty = "❌ ขนาดไม้เป็น 0 — ยังไม่ได้ลงทะเบียน · 👉 กด 🔄 อัปเดตราคา"
    side = "❌ ฝั่งไม่ถูกต้อง — ยังไม่ได้ลงทะเบียน · 👉 เริ่มใหม่จากเมนู ⚡ Quick"
    generic = "❌ ลงทะเบียนไม่ได้ — ยังไม่ได้ลงทะเบียน · 👉 ดูสถานะในเมนู ⚡ Quick"
    assert e_reg("qty_non_positive") == qty
    assert e_reg("invalid_side") == side
    assert _registration_refusal_text("qty_non_positive") == qty
    assert _registration_refusal_text("invalid_side") == side
    with caplog.at_level(logging.WARNING):
        unknown = e_reg("zzz")
    assert unknown == generic
    assert "zzz" not in unknown
    assert any("zzz" in rec.message for rec in caplog.records)
    assert _registration_refusal_text("zzz") == generic
    assert _registration_refusal_text("open_registration") == REFUSAL["quick_open"]


def test_g14_draft_numeric_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = _conn(tmp_path, "g14.db")
    _draft(conn, status="pick_stop", symbol="BTCUSDT")

    def _once(text: str) -> tuple[Any, list[dict[str, Any]]]:
        sent, send = _sent()
        out = handle_quick_draft_text(
            conn,
            text,
            futures=_fake(),
            send=send,
            edit=lambda *_a, **_k: True,
            now_ms=NOW,
        )
        for msg in sent:
            _no_codes(msg["text"])
        return out, sent

    def _assert_stop_null() -> None:
        row = conn.execute("SELECT stop_kind, stop_value FROM assist_quick_card").fetchone()
        assert row[0] is None and row[1] is None

    out, sent = _once("1.2.3")
    assert out == {"handled": True, "rejected": True}
    assert sent[0]["text"] == e_draft_stop_token("1.2.3")
    assert sent[0]["text"].endswith(STOP_STEP_HINT)
    assert conn.execute("SELECT status FROM assist_quick_card").fetchone()[0] == "pick_stop"

    out0, sent0 = _once("0")
    assert out0 == {"handled": True, "rejected": True}
    assert sent0[0]["text"] == e_draft_stop_token("0")

    outp, sentp = _once("25%")
    assert outp == {"handled": True, "rejected": True}
    assert sentp[0]["text"].startswith(e_draft_stop_pct_range(fmt_pct(Decimal("25"))))
    assert STOP_STEP_HINT in sentp[0]["text"]
    assert sentp[0]["text"].index("❌") < sentp[0]["text"].index("⚡ Quick")
    assert conn.execute("SELECT status FROM assist_quick_card").fetchone()[0] == "pick_stop"

    before = conn.execute("SELECT status, updated_at_ms, symbol FROM assist_quick_card").fetchone()
    for text in ("abc", "ok thanks", "สวัสดี", "62000 now", "-5"):
        out_s, sent_s = _once(text)
        assert out_s is None
        assert sent_s == []
    after = conn.execute("SELECT status, updated_at_ms, symbol FROM assist_quick_card").fetchone()
    assert tuple(after) == tuple(before)

    conn.execute("DELETE FROM assist_quick_card")
    _draft(conn, status="pick_symbol", symbol=None)
    out_th, sent_th = _once("สวัสดี")
    assert out_th is None and sent_th == []
    assert conn.execute("SELECT status FROM assist_quick_card").fetchone()[0] == "pick_symbol"

    out_z, sent_z = _once("ZZZQ")
    assert out_z is not None and out_z.get("step") == "pick_stop"
    assert conn.execute("SELECT status, symbol FROM assist_quick_card").fetchone()["symbol"] == "ZZZQUSDT"
    assert sent_z and "ไม่ใช่ราคา" not in sent_z[0]["text"]

    conn.execute("DELETE FROM assist_quick_card")
    _draft(conn, status="pick_stop", symbol="BTCUSDT")
    huge = "9" * 30
    out_pct, sent_pct = _once(huge + "%")
    assert out_pct == {"handled": True, "rejected": True}
    assert sent_pct[0]["text"].startswith(DRAFT_STOP_HUGE_PCT_PREFIX)
    _assert_stop_null()

    out_short, sent_short = _once(huge)
    assert out_short == {"handled": True, "rejected": True}
    assert sent_short[0]["text"] == DRAFT_STOP_HUGE_FAR
    _assert_stop_null()

    conn.execute("DELETE FROM assist_quick_card")
    _draft(conn, status="pick_stop", symbol="BTCUSDT", side="long")
    out_long, sent_long = _once(huge)
    assert out_long == {"handled": True, "rejected": True}
    assert sent_long[0]["text"] == DRAFT_STOP_HUGE_FAR
    _assert_stop_null()

    conn.execute("DELETE FROM assist_quick_card")
    _draft(conn, status="pick_stop", symbol="BTCUSDT")
    out_sci, sent_sci = _once("1e30")
    assert out_sci == {"handled": True, "rejected": True}
    assert sent_sci[0]["text"] == DRAFT_STOP_HUGE_FAR
    _assert_stop_null()

    conn.close()


def test_draft_stop_far_overflow_and_send_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)

    def _reject_price(
        conn: Any,
        *,
        token: str,
        fake: QuickFake,
    ) -> None:
        sent, send = _sent()
        out = handle_quick_draft_text(
            conn,
            token,
            futures=fake,
            send=send,
            edit=lambda *_a, **_k: True,
            now_ms=NOW,
        )
        assert out == {"handled": True, "rejected": True}
        assert len(sent) == 1
        assert sent[0]["text"] == DRAFT_STOP_HUGE_FAR
        _no_codes(sent[0]["text"])
        row = conn.execute("SELECT stop_kind, stop_value FROM assist_quick_card").fetchone()
        assert row[0] is None and row[1] is None

    conn = _conn(tmp_path, "draft-ovf.db")
    _draft(conn, status="pick_stop", symbol="BTCUSDT")
    for token in ("1e1000000", "1E+9999999"):
        _reject_price(conn, token=token, fake=_fake())
        conn.execute("UPDATE assist_quick_card SET stop_kind=NULL, stop_value=NULL WHERE id='draft1'")
        conn.commit()

    for mark in (Decimal("1.5"), Decimal("0.001")):
        conn.execute("DELETE FROM assist_quick_card")
        _draft(conn, status="pick_stop", symbol="BTCUSDT")
        _reject_price(conn, token="1e999999", fake=_fake(marks={"BTCUSDT": mark}))
    conn.close()

    conn_fail = _conn(tmp_path, "draft-send.db")
    _draft(conn_fail, status="pick_stop", symbol="BTCUSDT")

    def boom(_t: str, **_kw: Any) -> int:
        raise RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        handle_quick_draft_text(
            conn_fail,
            "130",
            futures=_fake(
                marks={"BTCUSDT": Decimal("100")},
                ticks={"BTCUSDT": Decimal("0.01")},
                steps={"BTCUSDT": Decimal("0.001")},
                min_notional={"BTCUSDT": Decimal("1")},
            ),
            send=boom,
            edit=lambda *_a, **_k: True,
            now_ms=NOW,
        )
    row = conn_fail.execute("SELECT status, stop_kind, stop_value FROM assist_quick_card").fetchone()
    assert row["status"] == "pick_stop"
    assert row["stop_kind"] is None and row["stop_value"] is None
    conn_fail.close()


def test_g15_g16_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = _conn(tmp_path, "g15.db")
    write_quick_settings(conn, cap_pct=1.0, risk_pct=2.5)
    sent, send = _sent()
    parsed = parse_quick_setting_command("q risk 1.5")
    assert parsed is not None
    handle_quick_setting_text(conn, parsed, send=send, now_ms=NOW)
    assert get_quick_settings(conn)["risk_pct"] == 1.5
    warn = sent[-1]["text"]
    assert "cap จำกัด" in warn or e_setting_risk_over_cap("1.5", "1") in warn
    _no_codes(warn.split("\n")[1] if "\n" in warn else warn)

    write_quick_settings(conn, cap_pct=0.5, risk_pct=0.5)
    handle_quick_callback(
        conn,
        f"qs:ok:risk:0.75:{NOW // 1000}",
        send=send,
        now_ms=NOW,
    )
    confirmed = sent[-1]["text"]
    assert "cap จำกัด" in confirmed or e_setting_risk_over_cap("0.75", "0.5") in confirmed
    _no_codes(confirmed.split("\n")[1] if "\n" in confirmed else confirmed)

    sent_r, send_r = _sent()
    bad = parse_quick_setting_command("q risk abc")
    assert bad is not None
    handle_quick_setting_text(conn, bad, send=send_r, now_ms=NOW)
    assert sent_r[-1]["text"] == '❌ ค่า "abc" ใช้ไม่ได้ — ใส่ตัวเลขมากกว่า 0 และไม่เกิน 100 (เช่น 0.6 หรือ 1.2)'
    long_tok = "9" * 40
    assert e_setting_reject(long_tok) == e_setting_reject("9" * 32 + "…")
    assert e_setting_reject(long_tok).startswith('❌ ค่า "' + ("9" * 32) + "…")
    long_cmd = parse_quick_setting_command(f"q risk {long_tok}")
    assert long_cmd is not None
    handle_quick_setting_text(conn, long_cmd, send=send_r, now_ms=NOW)
    assert sent_r[-1]["text"] == e_setting_reject(long_tok)
    assert "ลองใหม่" not in sent_r[-1]["text"]
    conn.close()


def test_g17_stale_texts() -> None:
    assert e_confirm_stale_ttl() == "ปุ่มนี้หมดอายุแล้ว (เกิน 60 วิ) — ไม่ได้เปลี่ยนอะไร"
    assert e_confirm_stale() == "ปุ่มนี้หมดอายุแล้ว — ไม่ได้เปลี่ยนอะไร"
    assert "60" not in e_confirm_stale()
    assert MSG_QUICK_CONFIRM_STALE == e_confirm_stale()


def test_g18_replay_skips_stop_far(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _conn(tmp_path, "g18.db")
    fake = _fake()
    sent, send = _sent()
    out = handle_quick_text(conn, "q BTC short 60000", futures=fake, send=send, now_ms=NOW)
    assert out is not None and out.get("ok") is True
    row = conn.execute("SELECT id, quote_stop FROM assist_quick_card").fetchone()
    assert row is not None
    card_id = str(row["id"])
    fake.marks["BTCUSDT"] = Decimal("40000")

    sent_rf, answers_rf, out_rf, edits_rf = _run_qk(conn, f"qk:rf:{card_id}", fake, now_ms=NOW + 1000)
    assert out_rf.get("reason") != "stop_far"
    for msg in sent_rf:
        assert "stop_far" not in msg["text"]
        assert msg["text"] != G9
        _no_codes(msg["text"])
    for edit in edits_rf:
        _no_codes(edit["text"])
    for toast in answers_rf:
        if toast:
            assert len(toast) <= 200

    sent_rg, answers_rg, out_rg, edits_rg = _run_qk(conn, f"qk:rg:{card_id}", fake, now_ms=NOW + 2000)
    assert out_rg.get("reason") != "stop_far"
    for msg in sent_rg:
        assert "stop ห่าง" not in msg["text"] or "เกิน 20%" not in msg["text"]
        _no_codes(msg["text"])
    for edit in edits_rg:
        _no_codes(edit["text"])
    for toast in answers_rg:
        if toast:
            assert len(toast) <= 200

    conn_far = _conn(tmp_path, "g18far.db")
    far = _fake(marks={"BTCUSDT": Decimal("40000")})
    out_far, sent_far = _typed(conn_far, "q BTC short 60000", far)
    assert out_far["reason"] == "stop_far"
    assert "เกิน 20%" in sent_far[0]["text"]

    cb = f"{QUICK_REGISTER_PREFIX}BTCUSDT:s:60000.0:{NOW // 1000}"
    sent_qr, send_qr = _sent()
    qr = handle_quick_register_callback(conn_far, cb, futures=far, send=send_qr, now_ms=NOW + 1000)
    assert qr.get("reason") != "stop_far"
    for msg in sent_qr:
        assert "stop_far" not in msg["text"]
        _no_codes(msg["text"])
    conn.close()
    conn_far.close()


def test_fail_never_returns_raw_reason() -> None:
    leaked = _fail("zzz_not_a_code")
    assert leaked["reason"] == "zzz_not_a_code"
    assert "zzz" not in leaked["text"]
    assert leaked["text"] == "❌ ทำรายการไม่ได้ — ดูสถานะในเมนู ⚡ Quick"
    assert REFUSAL["size_failed"] == "❌ คำนวณขนาดไม่ได้ — ตรวจยอดใน Futures"
    assert "stop_far" not in REFUSAL
    assert stop_far_refusal_text().startswith("❌ stop ห่างเกิน 20%")
    for value in REFUSAL.values():
        assert "ลองใหม่" not in value


def test_toasts_and_no_retry_phrase() -> None:
    for name in dir(quick_m1a1):
        if not name.startswith("MSG_"):
            continue
        value = getattr(quick_m1a1, name)
        if isinstance(value, str):
            assert len(value) <= 200, name
    for path in SRC.glob("quick*.py"):
        assert "ลองใหม่" not in path.read_text(encoding="utf-8")


def test_quick_errors_ast_and_import() -> None:
    import importlib

    importlib.import_module("jarvise_assist.quick_errors")
    src = (SRC / "quick_errors.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    allowed = {
        "__future__",
        "decimal",
        "logging",
        "typing",
        "jarvise_assist.messages",
        "jarvise_assist.sizing",
        "jarvise_assist.quick_minlot",
    }
    forbidden = {
        "redis",
        "jarvise_trade",
        "jarvise_assist.execute",
        "jarvise_assist.futures",
        "jarvise_assist.quick",
        "jarvise_assist.quick_card",
        "jarvise_assist.quick_m1a1",
        "jarvise_notify.quiet",
        "jarvise_assist.partner_admin",
    }
    minlot_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"redis"}
                assert alias.name in allowed or alias.name.split(".")[0] in {"decimal", "logging"}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module in allowed, module
            assert module not in forbidden
            if module == "jarvise_assist.quick_minlot":
                minlot_names.update(alias.name for alias in node.names)
            if module == "jarvise_assist.messages":
                imported = {alias.name for alias in node.names}
                assert imported <= {"fmt_price", "_fmt_price_tick", "fmt_qty"}
            if module == "jarvise_assist.sizing":
                imported = {alias.name for alias in node.names}
                assert imported <= {"round_level_to_tick", "floor_to_step"}
    assert minlot_names <= {"fmt_pct", "minimum_contract_qty"}
    assert minlot_names
