"""Card sizing metadata. Qty, leverage, and loss_at_stop stay on the old formula."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tests.test_assist import FakeFutures

from jarvise_assist.execute import _preview_from_sized
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.menu import render_open_positions
from jarvise_assist.messages import format_signal_message
from jarvise_assist.sizing import (
    SizedSignal,
    _shrink_for_max_leverage,
    cap_requote_qty,
    floor_to_step,
    size_signal,
)
from jarvise_assist.store import insert_signal, mark_signal_sent
from jarvise_ingest.db import SCHEMA_VERSION, open_db, schema_version

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
EXPIRES = int(datetime(2026, 11, 6, 12, 0, tzinfo=BANGKOK).timestamp() * 1000)


def _doge_short() -> SizedSignal:
    sized = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.0800"),
        stop=Decimal("0.0835"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    return sized


def test_doge_fixture_qty_leverage_and_loss_unchanged() -> None:
    sized = _doge_short()
    assert sized.qty == Decimal("5714")
    assert sized.notional == Decimal("457.12")
    assert sized.leverage == 1
    assert sized.loss_at_stop == Decimal("19.999")
    assert sized.balance_usdt == Decimal("1000")
    assert sized.budget_usdt == Decimal("20")
    assert sized.reduce_reasons == ("step",)


def test_step_reason_when_floor_lowers_qty() -> None:
    sized = size_signal(
        symbol="ADAUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.3"),
    )
    assert sized is not None
    raw = Decimal("10") / Decimal("10")
    assert sized.qty == floor_to_step(raw, Decimal("0.3"))
    assert sized.qty == Decimal("0.9")
    assert sized.loss_at_stop == Decimal("9")
    assert sized.reduce_reasons == ("step",)


def test_leverage_cap_reason_when_max_leverage_lowers_qty() -> None:
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99.9"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    assert sized.qty == Decimal("50")
    assert sized.notional == Decimal("5000")
    assert sized.leverage == 5
    assert sized.loss_at_stop == Decimal("5")
    assert sized.leverage_capped is True
    assert sized.reduce_reasons == ("leverage_cap",)


def test_margin_cap_reason_only_when_shrink_lowers_qty() -> None:
    sized = size_signal(
        symbol="ETHUSDT",
        side="long",
        entry=Decimal("10"),
        stop=Decimal("9"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    assert sized.qty == Decimal("10")
    same = _shrink_for_max_leverage(sized, available_balance=Decimal("1000"), uncapped=2)
    assert same is not None
    assert same.qty == sized.qty
    assert "margin_cap" not in same.reduce_reasons
    shrunk = _shrink_for_max_leverage(sized, available_balance=Decimal("10"), uncapped=8)
    assert shrunk is not None
    assert shrunk.qty == Decimal("5")
    assert shrunk.notional == Decimal("50")
    assert shrunk.leverage == 5
    assert shrunk.loss_at_stop == Decimal("5")
    assert shrunk.reduce_reasons == ("margin_cap",)


def test_requote_cap_stores_anchor_id_time_and_first_loss() -> None:
    fresh = size_signal(
        symbol="ADAUSDT",
        side="long",
        entry=Decimal("1"),
        stop=Decimal("0.9"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert fresh is not None
    assert fresh.qty == Decimal("100")
    sent_at = int(datetime(2026, 11, 6, 11, 55, tzinfo=BANGKOK).timestamp() * 1000)
    capped, reason = cap_requote_qty(
        fresh,
        first_qty=Decimal("95"),
        first_loss=Decimal("2"),
        balance=Decimal("1000"),
        anchor_id="as_ada_first",
        anchor_sent_at_ms=sent_at,
    )
    assert reason is None and capped is not None
    assert capped.qty == Decimal("20")
    assert capped.loss_at_stop == Decimal("2")
    assert capped.notional == Decimal("20")
    assert "requote_cap" in capped.reduce_reasons
    assert capped.requote_anchor_id == "as_ada_first"
    assert capped.requote_anchor_sent_at_ms == sent_at
    assert capped.requote_first_loss == Decimal("2")


def test_normal_card_shows_budget_and_hides_tiny_step() -> None:
    sized = _doge_short()
    text = format_signal_message(sized, expires_at_ms=EXPIRES, balance=Decimal("1000"))
    assert "ขนาด: 5714 DOGE ≈ 457.12 USDT" in text
    assert "ฐาน: Futures 1000 USDT × 2% = งบ 20 USDT" in text
    assert "ถ้าชน stop เสีย: 20 USDT (2%)" in text
    assert "ลดขนาดเพราะ" not in text
    assert text.index("ขนาด:") < text.index("ฐาน:") < text.index("ถ้าชน stop เสีย:")


def test_reduction_lines_only_below_95pct_and_leverage_line_once() -> None:
    tight = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99.9"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert tight is not None
    lev = format_signal_message(tight, expires_at_ms=EXPIRES, balance=Decimal("1000"))
    assert "ฐาน: Futures 1000 USDT × 1% = งบ 10 USDT" in lev
    assert lev.count("ขนาดเต็มต้องใช้ 10x — ลดเป็น 5x แล้ว") == 1
    assert "ลดขนาดเพราะ" not in lev

    stepped = size_signal(
        symbol="ADAUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.3"),
    )
    assert stepped is not None
    step_text = format_signal_message(stepped, expires_at_ms=EXPIRES, balance=Decimal("1000"))
    assert "ลดขนาดเพราะ: ปัดจำนวนตาม lot step" in step_text

    fresh = size_signal(
        symbol="ADAUSDT",
        side="long",
        entry=Decimal("1"),
        stop=Decimal("0.9"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert fresh is not None
    sent_at = int(datetime(2026, 11, 6, 11, 55, tzinfo=BANGKOK).timestamp() * 1000)
    capped, _reason = cap_requote_qty(
        fresh,
        first_qty=Decimal("95"),
        first_loss=Decimal("2"),
        balance=Decimal("1000"),
        anchor_id="as_ada_first",
        anchor_sent_at_ms=sent_at,
    )
    assert capped is not None
    requote = format_signal_message(capped, expires_at_ms=EXPIRES, balance=Decimal("1000"))
    assert "ฐาน: Futures 1000 USDT × 1% = งบ 10 USDT" in requote
    assert "ลดขนาดเพราะ: เพดาน re-quote จากไอเดีย ADAUSDT 11:55 (เสี่ยงครั้งแรก 2 USDT)" in requote
    assert "ขนาดเต็มต้องใช้" not in requote


def test_margin_line_does_not_repeat_the_leverage_line() -> None:
    sized = size_signal(
        symbol="ETHUSDT",
        side="long",
        entry=Decimal("10"),
        stop=Decimal("9"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    shrunk = _shrink_for_max_leverage(sized, available_balance=Decimal("10"), uncapped=8)
    assert shrunk is not None
    text = format_signal_message(shrunk, expires_at_ms=EXPIRES, balance=Decimal("1000"))
    assert "ลดขนาดเพราะ: margin ไม่พอสำหรับหลายไอเดียพร้อมกัน" in text
    assert text.count("ขนาดเต็มต้องใช้ 8x — ลดเป็น 5x แล้ว") == 1
    assert "ลดขนาดเพราะ: ปัดจำนวนตาม lot step" not in text


def test_preview_json_carries_sizing_keys() -> None:
    fresh = size_signal(
        symbol="ADAUSDT",
        side="long",
        entry=Decimal("1"),
        stop=Decimal("0.9"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert fresh is not None
    sent_at = 1_700_000_000_000
    capped, _reason = cap_requote_qty(
        fresh,
        first_qty=Decimal("10"),
        first_loss=Decimal("2"),
        balance=Decimal("1000"),
        anchor_id="as_anchor",
        anchor_sent_at_ms=sent_at,
    )
    assert capped is not None
    preview = _preview_from_sized(capped)
    assert preview["balance_usdt"] == "1000"
    assert preview["risk_pct"] == "1"
    assert preview["budget_usdt"] == "10.00"
    assert preview["reduce_reasons"] == ["requote_cap"]
    assert preview["requote_anchor_id"] == "as_anchor"
    assert preview["requote_anchor_sent_at_ms"] == sent_at
    assert preview["requote_first_loss"] == "2"
    assert Decimal(preview["qty"]) == Decimal("20")


def test_signal_row_stores_sizing_on_insert_and_requote(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "card.db")
    sized = _doge_short()
    sid = insert_signal(conn, sized, status="held", created_at_ms=1, expires_at_ms=2)
    row = conn.execute(
        """
        SELECT qty, leverage, loss_at_stop, balance_usdt, risk_pct, budget_usdt,
               reduce_reasons, requote_anchor_id
        FROM assist_signal WHERE id=?
        """,
        (sid,),
    ).fetchone()
    assert row["qty"] == 5714
    assert row["leverage"] == 1
    assert row["loss_at_stop"] == 19.999
    assert row["balance_usdt"] == 1000
    assert row["risk_pct"] == 2
    assert row["budget_usdt"] == 20
    assert json.loads(row["reduce_reasons"]) == ["step"]
    assert row["requote_anchor_id"] is None

    fresh = size_signal(
        symbol="ADAUSDT",
        side="long",
        entry=Decimal("1"),
        stop=Decimal("0.9"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert fresh is not None
    capped, _reason = cap_requote_qty(
        fresh,
        first_qty=Decimal("10"),
        first_loss=Decimal("2"),
        balance=Decimal("1000"),
        anchor_id=sid,
        anchor_sent_at_ms=11,
    )
    assert capped is not None
    mark_signal_sent(conn, sid, capped, sent_at_ms=20, expires_at_ms=30)
    updated = conn.execute(
        "SELECT qty, loss_at_stop, reduce_reasons, requote_anchor_id FROM assist_signal WHERE id=?",
        (sid,),
    ).fetchone()
    assert updated["qty"] == 20
    assert updated["loss_at_stop"] == 2
    assert json.loads(updated["reduce_reasons"]) == ["requote_cap"]
    assert updated["requote_anchor_id"] == sid
    conn.close()


def test_migration_26_is_additive(tmp_path: Path) -> None:
    path = tmp_path / "mig.db"
    conn = open_db(path)
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, symbol, side, entry, stop, target, qty, notional, leverage,
            loss_at_stop, created_at_ms, expires_at_ms, status
        ) VALUES ('old', 'ADAUSDT', 'long', 1, 0.9, 1.2, 95, 95, 1, 0.43, 1, 2, 'sent')
        """
    )
    for col in (
        "requote_anchor_id",
        "reduce_reasons",
        "budget_usdt",
        "risk_pct",
        "balance_usdt",
    ):
        conn.execute(f"ALTER TABLE assist_signal DROP COLUMN {col}")
    conn.execute("PRAGMA user_version = 25")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert SCHEMA_VERSION == 26
    assert schema_version(conn) == 26
    row = conn.execute(
        """
        SELECT qty, loss_at_stop, balance_usdt, risk_pct, budget_usdt,
               reduce_reasons, requote_anchor_id
        FROM assist_signal WHERE id='old'
        """
    ).fetchone()
    assert row["qty"] == 95
    assert row["loss_at_stop"] == 0.43
    assert row["balance_usdt"] is None
    assert row["risk_pct"] is None
    assert row["budget_usdt"] is None
    assert row["reduce_reasons"] is None
    assert row["requote_anchor_id"] is None
    conn.close()


def test_held_list_numbers_bullets_and_blank_line(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "held.db")
    day = int(datetime(2026, 11, 6, 12, 0, tzinfo=BANGKOK).timestamp() * 1000)
    btc = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.01"),
        entry_price=Decimal("100"),
        mark_price=Decimal("101"),
        update_time_ms=day,
        liquidation_price=Decimal("80"),
    )
    eth = FuturesPosition(
        symbol="ETHUSDT",
        position_side="BOTH",
        position_amt=Decimal("-1"),
        entry_price=Decimal("200"),
        mark_price=Decimal("190"),
        update_time_ms=day,
    )
    fake = FakeFutures(
        positions=[btc, eth],
        ticks={"BTCUSDT": Decimal("1"), "ETHUSDT": Decimal("0.01")},
        steps={"BTCUSDT": Decimal("0.001"), "ETHUSDT": Decimal("0.001")},
        orders=[
            {
                "symbol": "BTCUSDT",
                "type": "STOP_MARKET",
                "side": "SELL",
                "stopPrice": "92",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "workingType": "MARK_PRICE",
            }
        ],
    )
    text = render_open_positions(conn, futures=fake, now_ms=day)
    assert text.startswith("📋 ไม้ที่ถืออยู่\n\n1. BTCUSDT\n")
    assert "• LONG · qty 0.01" in text
    assert "• เข้า 100 · mark 101" in text
    assert "• stop ที่ตั้งไว้ 92 · trigger Mark" in text
    assert "• ยังไม่ปิด:" in text
    assert "\n\n2. ETHUSDT\n" in text
    assert "• SHORT · qty 1" in text
    assert "• เข้า 200.00 · mark 190.00" in text
    conn.close()
