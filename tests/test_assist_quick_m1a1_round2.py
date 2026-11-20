"""M1a-1 fix round 2 — register at fresh mark, exceptions, stricter guards."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_listen import _send_capture
from tests.test_assist_quick_card import (
    QuickFake,
    _happy_fake,
    _msg,
    _seed_settings,
)
from tests.test_assist_quick_m1a1_round1 import NOW, _flow_to_quoted, _run_qk

from jarvise_assist.listen import process_update
from jarvise_assist.messages import fmt_qty
from jarvise_assist.quick_card import REFUSAL, quick_mode_tag
from jarvise_assist.quick_m1a1 import STOP_STEP_HINT
from jarvise_ingest.db import open_db


def test_qk_rg_uses_rebuilt_qty_after_mark_move(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails on f29bef8: registration kept stale qty when mark moved."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "move.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"SUIUSDT": Decimal("2.0000")})
    cid = _flow_to_quoted(conn, fake)
    stale_qty = float(
        conn.execute("SELECT quote_qty FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["quote_qty"]
    )
    fake.marks["SUIUSDT"] = Decimal("2.0500")
    sent, _, _, edits = _run_qk(conn, f"qk:rg:{cid}", fake)
    reg = conn.execute(
        "SELECT planned_entry, planned_stop, qty FROM assist_quick_registration LIMIT 1"
    ).fetchone()
    assert reg is not None
    assert float(reg["planned_stop"]) == float(
        conn.execute("SELECT quote_stop FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["quote_stop"]
    )
    assert float(reg["qty"]) != stale_qty
    reg_msg = sent[-1]["text"]
    qty_s = fmt_qty(Decimal(str(reg["qty"])))
    assert qty_s in reg_msg
    assert edits and qty_s in edits[-1]["text"]
    assert float(reg["qty"]) != stale_qty
    conn.close()


def test_qk_rg_create_raises_rolls_back_to_quoted(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "raise.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)

    with mock.patch(
        "jarvise_assist.quick_m1a1.create_quick_registration_from_card",
        side_effect=RuntimeError("boom"),
    ):
        _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("ok") is False
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "quoted"
    )
    assert answers == [REFUSAL["read_failed"]]
    _, answers2, out2, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out2.get("ok") is True
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 1
    assert len(answers2) == 1
    conn.close()


def test_pick_symbol_rejects_digits_only(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "num.db")
    _seed_settings(conn)
    fake = _happy_fake()
    _run_qk(conn, "qk:sd:l", fake)
    sent, send = _send_capture()
    out = process_update(
        conn,
        _msg("67000"),
        send=send,
        futures=fake,
        now_ms_value=NOW,
    )
    assert out.get("handled") is not True
    assert conn.execute("SELECT status FROM assist_quick_card LIMIT 1").fetchone()["status"] == "pick_symbol"
    conn.close()


def test_refused_sl_keeps_stop_buttons(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "rsl.db")
    _seed_settings(conn)
    fake = QuickFake(balance=Decimal("1"), available=Decimal("1"))
    _run_qk(conn, "qk:sd:l", fake)
    cid = conn.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
    _run_qk(conn, f"qk:sy:{cid}:SUIUSDT", fake)
    _, _, _, edits = _run_qk(conn, f"qk:sl:{cid}:0.5", fake)
    assert edits
    flat = [
        b["callback_data"]
        for row in edits[-1].get("reply_markup", {}).get("inline_keyboard", [])
        for b in row
    ]
    assert any(d.startswith("qk:sl:") for d in flat)
    conn.close()


def test_typed_pct_invalid_shows_stop_hint(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "pct0.db")
    _seed_settings(conn)
    fake = _happy_fake()
    _run_qk(conn, "qk:sd:l", fake)
    cid = conn.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
    _run_qk(conn, f"qk:sy:{cid}:BTCUSDT", fake)
    edits: list[dict[str, Any]] = []

    def capture_edit(mid: int, text: str, **kw: Any) -> bool:
        edits.append({"text": text, **kw})
        return True

    sent, send = _send_capture()
    process_update(
        conn,
        _msg("0%"),
        send=send,
        edit=capture_edit,
        futures=fake,
        now_ms_value=NOW,
    )
    assert edits
    assert STOP_STEP_HINT in edits[-1]["text"]
    conn.close()


def test_close_notice_net_r_none_zero_risk_plain(tmp_path: Any) -> None:
    from jarvise_assist.quick import _send_close_notice

    conn = open_db(tmp_path / "r0.db")
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            close_notice_at_ms, updated_at_ms
        ) VALUES ('t1', 'r1', 'BTCUSDT', 'long', 'closed', 1.0, NULL, NULL, 1)
        """
    )
    conn.commit()
    sent: list[str] = []

    def send(msg: str, **_kw: Any) -> None:
        sent.append(msg)

    _send_close_notice(
        conn,
        send,
        trade_id="t1",
        client=None,
        payload={
            "symbol": "BTCUSDT",
            "side": "long",
            "net_usdt": 1.0,
            "net_r": None,
            "risk_usdt": 0.0,
            "commission_other_asset": None,
        },
        now_ms=NOW,
    )
    assert "R คำนวณไม่ได้" in sent[0]
    assert "ค่าธรรมเนียมเป็น" not in sent[0]
    conn.close()


def test_full_flow_mode_tag_on_every_message(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for mode in ("off", "testnet", "live"):
        _forbid_trade_client(monkeypatch)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", mode)
        tag = quick_mode_tag()
        conn = open_db(tmp_path / f"flow-{mode}.db")
        _seed_settings(conn)
        fake = _happy_fake()
        sent, _, _, edits = _run_qk(conn, "qk:sd:l", fake)
        cid = conn.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
        for data in (f"qk:sy:{cid}:SUIUSDT", f"qk:sl:{cid}:0.5", f"qk:rg:{cid}"):
            s, _, _, e = _run_qk(conn, data, fake)
            sent.extend(s)
            edits.extend(e)
        bodies = [x["text"] for x in sent + edits]
        assert all(tag in t for t in bodies if t)
        assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 1
        conn.close()
