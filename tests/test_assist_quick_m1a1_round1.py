"""M1a-1 fix round 1 — status guards, drafts, notices, migration."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_listen import _send_capture
from tests.test_assist_quick_card import (
    QuickFake,
    _happy_fake,
    _msg,
    _seed_settings,
)

from jarvise_assist.listen import process_update
from jarvise_assist.order_lines import reduce_only_flag
from jarvise_assist.quick import (
    _send_close_notice,
    _send_tag_notice,
    render_quick_menu,
)
from jarvise_assist.quick_card import quick_mode_tag
from jarvise_assist.quick_m1a1 import (
    MSG_ALREADY_REGISTERED,
    MSG_BAD_BUTTON,
    MSG_CARD_QUOTE_EXPIRED,
    MSG_DRAFT_EXPIRED,
    QUICK_CARD_TTL_MS,
    QUICK_DRAFT_TTL_MS,
    _quoted_markup,
    _refresh_only_markup,
    _stop_step_markup,
    _symbol_buttons,
    create_card_from_typed_command,
)
from jarvise_ingest.db import SCHEMA_VERSION, open_db, schema_version

NOW = _ms_bangkok(2026, 11, 20, 12, 0)


def _answer_capture() -> tuple[list[str | None], Any]:
    texts: list[str | None] = []

    def answer(cq_id: str, *, text: str | None = None) -> bool:
        del cq_id
        texts.append(text)
        return True

    return texts, answer


def _run_qk(
    conn: Any,
    data: str,
    fake: QuickFake,
    *,
    now_ms: int = NOW,
    edit_returns: bool | None = True,
) -> tuple[list[dict[str, Any]], list[str | None], dict[str, Any], list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    edits: list[dict[str, Any]] = []

    def send(t: str, **kw: Any) -> int:
        mid = 100 + len(sent)
        sent.append({"text": t, **kw})
        return mid

    def edit(mid: int, text: str, **kw: Any) -> bool:
        edits.append({"mid": mid, "text": text, **kw})
        return True if edit_returns is None else bool(edit_returns)

    answer_texts, answer = _answer_capture()
    chat_id = int((__import__("os").environ.get("TELEGRAM_CHAT_ID") or "42").strip())
    upd = {
        "callback_query": {
            "id": f"cq-{data}",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": 1},
        }
    }
    out = process_update(
        conn,
        upd,
        send=send,
        answer=answer,
        edit=edit,
        futures=fake,
        now_ms_value=now_ms,
    )
    return sent, answer_texts, out, edits


def _flow_to_quoted(conn: Any, fake: QuickFake, *, now_ms: int = NOW) -> str:
    """qk:sd:l → sy SUI → sl 0.5%; returns card id."""
    _run_qk(conn, "qk:sd:l", fake, now_ms=now_ms)
    row = conn.execute(
        "SELECT id FROM assist_quick_card WHERE status='pick_symbol' ORDER BY created_at_ms DESC"
    ).fetchone()
    cid = str(row["id"])
    _run_qk(conn, f"qk:sy:{cid}:SUIUSDT", fake, now_ms=now_ms)
    _run_qk(conn, f"qk:sl:{cid}:0.5", fake, now_ms=now_ms)
    return cid


def test_mode_tag_live_testnet_off(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    assert quick_mode_tag() == "💰 เงินจริง"
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    assert quick_mode_tag() == "🧪 เทสเน็ต · ราคา/ยอดอ่านจากบัญชีจริง"
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    assert quick_mode_tag() == "💰 เงินจริง · บอทไม่ส่งคำสั่ง"


def test_draft_expires_at_5min_plus_1ms(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "exp.db")
    _seed_settings(conn)
    fake = _happy_fake()
    t0 = NOW
    _run_qk(conn, "qk:sd:l", fake, now_ms=t0)
    row = conn.execute("SELECT id, updated_at_ms FROM assist_quick_card LIMIT 1").fetchone()
    cid = str(row["id"])
    late = int(row["updated_at_ms"]) + QUICK_DRAFT_TTL_MS + 1
    sent, seen, out, _ed = _run_qk(conn, f"qk:sy:{cid}:SUIUSDT", fake, now_ms=late)
    assert seen == [MSG_DRAFT_EXPIRED]
    st = conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
    assert st == "expired"
    conn.close()


def test_qk_sl_non_preset_refused(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "sl.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    conn.execute("UPDATE assist_quick_card SET status='pick_stop' WHERE id=?", (cid,))
    conn.commit()
    _, seen, _, _ = _run_qk(conn, f"qk:sl:{cid}:0.4", fake)
    assert [t for t in seen if t is not None][-1] == MSG_BAD_BUTTON
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "pick_stop"
    )
    conn.close()


def test_cancel_then_sy_no_resurrect(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "x.db")
    _seed_settings(conn)
    fake = _happy_fake()
    _run_qk(conn, "qk:sd:l", fake)
    cid = conn.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
    _run_qk(conn, f"qk:x:{cid}", fake)
    _, seen, _, _ = _run_qk(conn, f"qk:sy:{cid}:BTCUSDT", fake)
    assert [t for t in seen if t is not None][-1] == MSG_BAD_BUTTON
    conn.close()


def test_registered_sl_and_x_idempotent(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "reg.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    _run_qk(conn, f"qk:rg:{cid}", fake)
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "registered"
    )
    _, seen, _, _ = _run_qk(conn, f"qk:sl:{cid}:0.5", fake)
    assert [t for t in seen if t is not None][-1] == MSG_ALREADY_REGISTERED
    _, seen2, _, _ = _run_qk(conn, f"qk:x:{cid}", fake)
    assert [t for t in seen2 if t is not None][-1] == MSG_ALREADY_REGISTERED
    conn.close()


def test_double_register_idempotent(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "dr.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    _run_qk(conn, f"qk:rg:{cid}", fake)
    n_reg = conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"]
    _, seen, _, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert [t for t in seen if t is not None][-1] == MSG_ALREADY_REGISTERED
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == n_reg
    conn.close()


def test_refresh_refusal_markup_no_register_button() -> None:
    cid = "a1b2c3d4"
    kb = _refresh_only_markup(cid)["inline_keyboard"]
    flat = [b["callback_data"] for row in kb for b in row]
    assert any(d.startswith("qk:rf:") for d in flat)
    assert any(d.startswith("qk:x:") for d in flat)
    assert not any(d.startswith("qk:rg:") for d in flat)


def test_quote_register_expired_markup_refresh_only(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "qexp.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    row = conn.execute("SELECT quoted_at_ms FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    late = int(row["quoted_at_ms"]) + QUICK_CARD_TTL_MS + 1
    _, answers, _, edits = _run_qk(conn, f"qk:rg:{cid}", fake, now_ms=late)
    assert answers == [MSG_CARD_QUOTE_EXPIRED]
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0
    assert edits, "quote-expired rg must edit the card"
    kb = edits[-1]["reply_markup"]
    flat = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert any(d.startswith("qk:rf:") for d in flat)
    assert any(d.startswith("qk:x:") for d in flat)
    assert not any(d.startswith("qk:rg:") for d in flat)
    conn.close()


def test_wrong_side_refresh_includes_mark(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "ws.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"SUIUSDT": Decimal("2.0")})
    cid = _flow_to_quoted(conn, fake)
    conn.execute(
        "UPDATE assist_quick_card SET stop_kind='price', stop_value='2.5' WHERE id=?",
        (cid,),
    )
    conn.commit()
    sent, _, _, edits = _run_qk(conn, f"qk:rf:{cid}", fake)
    texts = " ".join(s.get("text", "") for s in sent + edits)
    assert "อยู่ผิดฝั่งของราคาแล้ว (ตอนนี้ 2" in texts
    conn.close()


def test_edit_false_sends_new_message(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "edf.db")
    _seed_settings(conn)
    fake = _happy_fake()
    sent: list[dict[str, Any]] = []
    next_mid = 99

    def send(t: str, **kw: Any) -> int:
        nonlocal next_mid
        next_mid += 1
        sent.append({"text": t, **kw})
        return next_mid

    def edit(_mid: int, _text: str, **kw: Any) -> bool:
        return False

    answer_texts, answer = _answer_capture()
    chat_id = 1
    for data in ("qk:sd:l",):
        upd = {
            "callback_query": {
                "id": f"cq-{data}",
                "data": data,
                "message": {"chat": {"id": chat_id}, "message_id": 1},
            }
        }
        process_update(
            conn,
            upd,
            send=send,
            answer=answer,
            edit=edit,
            futures=fake,
            now_ms_value=NOW,
        )
    cid = conn.execute("SELECT id, message_id FROM assist_quick_card LIMIT 1").fetchone()
    mid_before = int(cid["message_id"])
    process_update(
        conn,
        {
            "callback_query": {
                "id": "cq-sy",
                "data": f"qk:sy:{cid['id']}:SUIUSDT",
                "message": {"chat": {"id": chat_id}, "message_id": mid_before},
            }
        },
        send=send,
        answer=answer,
        edit=edit,
        futures=fake,
        now_ms_value=NOW,
    )
    assert len(sent) == 2
    row = conn.execute("SELECT message_id FROM assist_quick_card WHERE id=?", (cid["id"],)).fetchone()
    assert int(row["message_id"]) == 101
    assert mid_before == 100
    conn.close()


def test_hello_world_not_consumed_during_draft(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "hw.db")
    _seed_settings(conn)
    fake = _happy_fake()
    _run_qk(conn, "qk:sd:l", fake)
    st_before = conn.execute("SELECT status FROM assist_quick_card LIMIT 1").fetchone()["status"]
    sent, send = _send_capture()
    out = process_update(
        conn,
        _msg("hello world"),
        send=send,
        futures=fake,
        now_ms_value=NOW,
    )
    assert out.get("handled") is not True
    assert conn.execute("SELECT status FROM assist_quick_card LIMIT 1").fetchone()["status"] == st_before
    conn.close()


def test_listen_no_futures_without_draft(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def _boom() -> Any:
        raise AssertionError("build_futures_client must not run")

    monkeypatch.setattr("jarvise_assist.listen.build_futures_client", _boom)
    conn = open_db(tmp_path / "nf.db")
    _seed_settings(conn)
    sent, send = _send_capture()
    _, answer = _answer_capture()
    out = process_update(
        conn,
        {
            "callback_query": {
                "id": "1",
                "data": "qk:sd:l",
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        },
        send=send,
        answer=answer,
        futures=None,
        now_ms_value=NOW,
    )
    assert out.get("step") == "pick_symbol"
    assert len(sent) == 1
    conn.close()


def test_typed_q_refusal_no_card_row(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "tq.db")
    _seed_settings(conn)
    fake = QuickFake(balance=Decimal("1"), available=Decimal("1"))
    parsed = {"symbol": "BTCUSDT", "side": "long", "stop_pct": Decimal("0.4")}
    cid, card = create_card_from_typed_command(conn, fake, parsed, now_ms=NOW)
    assert cid is None
    assert not card.get("ok")
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_card").fetchone()["c"] == 0
    conn.close()


def test_reduce_only_false_string_not_protective() -> None:
    assert reduce_only_flag({"reduceOnly": "false"}) is False
    assert reduce_only_flag({"reduceOnly": "true"}) is True


def test_tag_notice_mark_then_send_once(tmp_path: Any) -> None:
    conn = open_db(tmp_path / "tag.db")
    t0 = NOW
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            status, created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('r1', 'BTCUSDT', 'long', 1, 0.9, 0.01, 'tagged', ?, ?, ?)
        """,
        (t0, t0 + 600_000, t0),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, entry_avg, entry_qty,
            risk_usdt, entry_time_ms, updated_at_ms
        ) VALUES ('t1', 'r1', 'BTCUSDT', 'long', 'open', 100, 0.01, 5, ?, ?)
        """,
        (t0, t0),
    )
    conn.commit()
    sends: list[str] = []

    def send(msg: str, **_kw: Any) -> None:
        sends.append(msg)

    _send_tag_notice(conn, send, registration_id="r1", client=None, now_ms=t0)
    _send_tag_notice(conn, send, registration_id="r1", client=None, now_ms=t0 + 1)
    assert len(sends) == 1
    assert (
        conn.execute("SELECT notice_sent_at_ms FROM assist_quick_registration WHERE id='r1'").fetchone()[0]
        == t0
    )
    conn.close()


def test_close_notice_net_r_none_text(tmp_path: Any) -> None:
    conn = open_db(tmp_path / "cls.db")
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            commission_other_asset, updated_at_ms
        ) VALUES ('t1', 'r1', 'BTCUSDT', 'long', 'closed', 1.0, NULL, 'BNB', 1)
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
            "risk_usdt": 5.0,
            "commission_other_asset": "BNB",
        },
        now_ms=NOW,
    )
    assert sent
    assert "R คำนวณไม่ได้ (ค่าธรรมเนียมเป็น BNB)" in sent[0]
    conn.close()


def test_migration_34_adds_quick_card(tmp_path: Any) -> None:
    path = tmp_path / "m34.db"
    conn = open_db(path)
    conn.execute("DROP TABLE IF EXISTS assist_quick_card")
    conn.execute("ALTER TABLE assist_quick_registration DROP COLUMN notice_sent_at_ms")
    conn.execute("ALTER TABLE assist_quick_trade DROP COLUMN close_notice_at_ms")
    reg_cols = {r[1] for r in conn.execute("PRAGMA table_info(assist_quick_registration)").fetchall()}
    tr_cols = {r[1] for r in conn.execute("PRAGMA table_info(assist_quick_trade)").fetchall()}
    assert "notice_sent_at_ms" not in reg_cols
    assert "close_notice_at_ms" not in tr_cols
    conn.execute("PRAGMA user_version = 34")
    conn.commit()
    conn.close()
    conn = open_db(path)
    assert schema_version(conn) == SCHEMA_VERSION == 38
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assist_quick_card'"
    ).fetchone()
    reg_cols = {r[1] for r in conn.execute("PRAGMA table_info(assist_quick_registration)").fetchall()}
    tr_cols = {r[1] for r in conn.execute("PRAGMA table_info(assist_quick_trade)").fetchall()}
    assert "notice_sent_at_ms" in reg_cols
    assert "close_notice_at_ms" in tr_cols
    ver1 = schema_version(conn)
    conn.close()
    conn2 = open_db(path)
    assert schema_version(conn2) == ver1 == 38
    conn2.close()


def test_callback_data_within_64_bytes() -> None:
    cid = "deadbeef"
    too_long = "A" * 46 + "USDT"
    buttons = [b for row in _symbol_buttons(cid, [too_long, "1000PEPEUSDT"]) for b in row]
    cbs = {b["callback_data"] for b in buttons}
    assert not any(too_long in cb for cb in cbs)
    assert any(b["text"] == "1000PEPE" for b in buttons)
    assert any(b["callback_data"] == f"qk:sy:{cid}:1000PEPEUSDT" for b in buttons)
    assert any(b["callback_data"] == f"qk:x:{cid}" for b in buttons)
    for sym in ("1000PEPEUSDT",):
        for btn in [b for row in _symbol_buttons(cid, [sym]) for b in row]:
            assert len(btn["callback_data"].encode("utf-8")) <= 64
    for m in (
        _stop_step_markup(cid),
        _quoted_markup(cid),
        _refresh_only_markup(cid),
    ):
        for row in m["inline_keyboard"]:
            for btn in row:
                assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_full_flow_sd_sy_sl_rg_one_answer_per_step(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "flow.db")
    _seed_settings(conn)
    fake = _happy_fake()
    steps = [
        "qk:sd:l",
    ]
    cid = None
    answers_per_step: list[int] = []
    for _ in range(1):
        _, seen, out, _ = _run_qk(conn, steps[0], fake)
        answers_per_step.append(len(seen))
        row = conn.execute("SELECT id FROM assist_quick_card ORDER BY created_at_ms DESC").fetchone()
        cid = str(row["id"])
    for data in (f"qk:sy:{cid}:SUIUSDT", f"qk:sl:{cid}:0.5", f"qk:rg:{cid}"):
        _, seen, _, _ = _run_qk(conn, data, fake)
        answers_per_step.append(len(seen))
    assert all(n <= 1 for n in answers_per_step)
    assert sum(answers_per_step) >= len(answers_per_step)
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 1
    reg_stop = conn.execute("SELECT planned_stop FROM assist_quick_registration LIMIT 1").fetchone()[0]
    quote_stop = conn.execute("SELECT quote_stop FROM assist_quick_card WHERE id=?", (cid,)).fetchone()[0]
    assert float(reg_stop) == float(quote_stop)
    conn.close()


def test_menu_open_trade_line(tmp_path: Any) -> None:
    conn = open_db(tmp_path / "menu.db")
    t0 = NOW
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            status, created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('r1', 'BTCUSDT', 'long', 1, 0.9, 0.01, 'tagged', ?, ?, ?)
        """,
        (t0, t0 + 600_000, t0),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, planned_stop,
            entry_avg, entry_qty, entry_time_ms, updated_at_ms
        ) VALUES ('t1', 'r1', 'BTCUSDT', 'long', 'open', 0.9, 100, 0.01, ?, ?)
        """,
        (t0, t0),
    )
    conn.commit()
    text, _ = render_quick_menu(conn, now_ms=t0 + 60_000)
    assert "ดูกำไร/ขาดทุนใน 📋 ไม้ที่ถืออยู่" in text
    assert "ข้อมูลอัปเดตทุก 5 นาที" in text
    conn.close()


def test_stop_presets_build_pct_cards(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "pct.db")
    _seed_settings(conn)
    mark = Decimal("100000")
    fake = _happy_fake(marks={"BTCUSDT": mark})
    for preset in ("0.3", "0.5", "1"):
        _run_qk(conn, "qk:sd:l", fake, now_ms=NOW)
        row = conn.execute(
            "SELECT id FROM assist_quick_card WHERE status='pick_symbol' ORDER BY created_at_ms DESC LIMIT 1"
        ).fetchone()
        cid = str(row["id"])
        _run_qk(conn, f"qk:sy:{cid}:BTCUSDT", fake, now_ms=NOW)
        _run_qk(conn, f"qk:sl:{cid}:{preset}", fake, now_ms=NOW)
        card_row = conn.execute(
            "SELECT status, stop_value, quote_stop FROM assist_quick_card WHERE id=?",
            (cid,),
        ).fetchone()
        assert card_row["status"] == "quoted"
        assert card_row["stop_value"] == preset
        pct = Decimal(preset)
        expected_stop = float(mark * (Decimal("1") - pct / Decimal("100")))
        assert float(card_row["quote_stop"]) == pytest.approx(expected_stop, rel=1e-4)
    conn.close()
