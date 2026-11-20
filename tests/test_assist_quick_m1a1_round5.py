"""M1a-1 fix round 5 — mutation-backed tests S19–S36."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_listen import _send_capture, process_update
from tests.test_assist_quick import _fill
from tests.test_assist_quick_card import (
    REFUSAL,
    QuickFake,
    _happy_fake,
    _msg,
    _seed_settings,
)
from tests.test_assist_quick_m1a1_round1 import NOW, _answer_capture, _flow_to_quoted, _run_qk
from tests.test_assist_quick_m1a1_round4 import (
    T0,
    T1,
    T2,
    T3,
    _OrdersFake,
    _rows,
)
from tests.test_assist_real_trades import FakeFutures

from jarvise_assist.messages import fmt_price, fmt_qty
from jarvise_assist.pause import next_0820_bangkok_ms
from jarvise_assist.quick import (
    REG_TTL_MS,
    _send_close_notice,
    _send_tag_notice,
    create_quick_registration,
    sync_quick_trades,
)
from jarvise_assist.quick_card import (
    _refusal_week_loss,
    quick_mode_tag,
)
from jarvise_assist.quick_m1a1 import (
    MSG_ALREADY_REGISTERED,
    MSG_BAD_BUTTON,
    MSG_CARD_QUOTE_EXPIRED,
    MSG_CARD_TOO_OLD,
    QUICK_CARD_MAX_AGE_MS,
    QUICK_CARD_TTL_MS,
    QUOTE_CLOCK_SKEW_MS,
    _decorate_card_text,
    _edit_draft_message,
    _persist_quote,
    _quote_is_expired,
    _quoted_markup,
    _recent_quick_symbols,
    _refresh_only_markup,
    _side_label,
    _stop_step_markup,
    build_card_for_row,
    handle_quick_trade_callback,
)
from jarvise_ingest.db import open_db


def _insert_draft_card(
    conn: Any,
    *,
    cid: str,
    status: str,
    side: str = "long",
    symbol: str | None = None,
    stop_kind: str | None = None,
    stop_value: str | None = None,
    created_at_ms: int = T0,
    updated_at_ms: int | None = None,
) -> None:
    upd = updated_at_ms if updated_at_ms is not None else created_at_ms
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, stop_kind, stop_value, status,
            created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (cid, side, symbol, stop_kind, stop_value, status, created_at_ms, upd),
    )
    conn.commit()


def _card_quote_payload(
    conn: Any, fake: QuickFake, *, symbol: str = "SUIUSDT", side: str = "long"
) -> dict[str, Any]:
    from jarvise_assist.quick_card import build_quick_card

    row = build_quick_card(
        conn=conn,
        futures=fake,
        symbol=symbol,
        side=side,
        stop_pct=Decimal("0.5"),
        now_ms=T0,
    )
    assert row.get("ok")
    return row


# --- S19: _persist_quote status guard (round4 6a / P3) -----------------------


def test_s19_persist_quote_ignores_terminal_statuses(tmp_path: Any) -> None:
    """S19: _persist_quote must not touch registered/cancelled/expired rows (kills P3; see round4 6a)."""
    conn = open_db(tmp_path / "s19.db")
    _seed_settings(conn)
    fake = _happy_fake()
    card = _card_quote_payload(conn, fake)
    for cid, status in (("r1", "registered"), ("c1", "cancelled"), ("e1", "expired")):
        conn.execute(
            """
            INSERT INTO assist_quick_card (
                id, side, symbol, stop_kind, stop_value, status,
                quote_entry, quote_stop, quote_qty, quoted_at_ms,
                created_at_ms, updated_at_ms
            ) VALUES (?, 'long', 'SUIUSDT', 'pct', '0.5', ?, 1, 0.9, 0.01, ?, ?, ?)
            """,
            (cid, status, T0, T0, T0),
        )
    conn.commit()
    before = _rows(conn)
    for cid in ("r1", "c1", "e1"):
        _persist_quote(
            conn,
            cid,
            card,
            stop_kind="pct",
            stop_value="1",
            now_ms=T1,
        )
    after = _rows(conn)
    assert before == after
    conn.close()


def test_s19_rf_after_registration_doc_p3(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S19 companion: rf on registered never persists quotes (round4 P3; does not kill S19)."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s19b.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    q0 = conn.execute(
        "SELECT quote_entry, quote_stop, quote_qty FROM assist_quick_card WHERE id=?", (cid,)
    ).fetchone()
    _run_qk(conn, f"qk:rg:{cid}", fake)
    _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=T2)
    q1 = conn.execute(
        "SELECT quote_entry, quote_stop, quote_qty FROM assist_quick_card WHERE id=?", (cid,)
    ).fetchone()
    assert q1 == q0
    conn.close()


# --- S20: _cancel_other_drafts cards A–E ------------------------------------


def test_s20_cancel_other_drafts_cards_a_through_e(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S20: wrong-side rf cancels pick_symbol/pick_stop only; quoted and registered rows untouched."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s20.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"BTCUSDT": Decimal("100000")})
    keep = "aaaa0001"
    _insert_draft_card(
        conn,
        cid=keep,
        status="quoted",
        symbol="BTCUSDT",
        stop_kind="price",
        stop_value="120000",
        created_at_ms=T0,
    )
    _insert_draft_card(conn, cid="bbbb0002", status="pick_symbol", created_at_ms=T0)
    _insert_draft_card(
        conn,
        cid="cccc0003",
        status="pick_stop",
        symbol="ETHUSDT",
        side="short",
        created_at_ms=T0,
    )
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, stop_kind, stop_value, status,
            quote_entry, quote_stop, quote_qty, quoted_at_ms,
            created_at_ms, updated_at_ms
        ) VALUES ('dddd0004', 'long', 'SOLUSDT', 'pct', '0.5', 'quoted',
            180.0, 179.1, 2.5, ?, ?, ?)
        """,
        (T0, T0, T0),
    )
    rid_f = "aqr00000000001"
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            status, created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES (?, 'LINKUSDT', 'long', 10, 9, 1, 'open', ?, ?, ?)
        """,
        (rid_f, T0, T0 + 600_000, T0),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, stop_kind, stop_value, status, registration_id,
            quote_entry, quote_stop, quote_qty, quoted_at_ms,
            created_at_ms, updated_at_ms
        ) VALUES ('ffff0006', 'long', 'LINKUSDT', 'pct', '0.5', 'registered', ?,
            10.0, 9.5, 1.0, ?, ?, ?)
        """,
        (rid_f, T0, T0, T0),
    )
    conn.commit()
    rows_before = {r["id"]: tuple(r) for r in conn.execute("SELECT * FROM assist_quick_card ORDER BY id")}
    _, answers, out, _ = _run_qk(conn, f"qk:rf:{keep}", fake, now_ms=T1)
    assert out == {"ok": False, "stop_wrong_side": True}
    assert answers == [None]
    st = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM assist_quick_card").fetchall()}
    assert st[keep] == "pick_stop"
    assert st["bbbb0002"] == "cancelled"
    assert st["cccc0003"] == "cancelled"
    rows_after = {r["id"]: tuple(r) for r in conn.execute("SELECT * FROM assist_quick_card ORDER BY id")}
    assert rows_after["dddd0004"] == rows_before["dddd0004"]
    assert rows_after["ffff0006"] == rows_before["ffff0006"]
    conn.close()


# --- S21: rf boundaries, pick_stop refresh, races, wrong_side dict ---------


def test_s21_rf_age_boundary_pick_stop_and_wrong_side_dict(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S21: max age inclusive; pick_stop rf succeeds; wrong_side return dict."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s21.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake, now_ms=T0)
    row = conn.execute("SELECT created_at_ms FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    created = int(row["created_at_ms"])
    at_edge = created + QUICK_CARD_MAX_AGE_MS
    _, answers_edge, out_edge, _ = _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=at_edge)
    assert answers_edge == [None]
    assert out_edge.get("refreshed") is True
    _, answers_old, _, _ = _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=at_edge + 1)
    assert answers_old == [MSG_CARD_TOO_OLD]

    conn2 = open_db(tmp_path / "s21b.db")
    _seed_settings(conn2)
    _run_qk(conn2, "qk:sd:l", fake, now_ms=T0)
    cid2 = conn2.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
    _run_qk(conn2, f"qk:sy:{cid2}:SUIUSDT", fake, now_ms=T0)
    conn2.execute(
        "UPDATE assist_quick_card SET stop_kind='pct', stop_value='0.5' WHERE id=?",
        (cid2,),
    )
    conn2.commit()
    _, answers_ps, out_ps, edits = _run_qk(conn2, f"qk:rf:{cid2}", fake, now_ms=T1)
    assert answers_ps == [None]
    assert out_ps.get("refreshed") is True
    assert (
        conn2.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid2,)).fetchone()["status"]
        == "quoted"
    )
    assert edits[-1]["reply_markup"] == _quoted_markup(cid2)

    conn3 = open_db(tmp_path / "s21c.db")
    _seed_settings(conn3)
    fake3 = _happy_fake(marks={"BTCUSDT": Decimal("100000")})
    kid = "feedbeef"
    _insert_draft_card(
        conn3,
        cid=kid,
        status="quoted",
        symbol="BTCUSDT",
        stop_kind="price",
        stop_value="120000",
        created_at_ms=T0,
    )
    import jarvise_assist.quick_m1a1 as m1

    def race_build(conn_a: Any, futures_a: Any, row: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        conn_a.execute(
            "UPDATE assist_quick_card SET status='cancelled', updated_at_ms=? WHERE id=?",
            (int(now_ms), row["id"]),
        )
        conn_a.commit()
        return {"ok": False, "reason": "stop_wrong_side", "text": "x"}

    monkeypatch.setattr(m1, "build_card_for_row", race_build)
    _, answers_r, out_r, _ = _run_qk(conn3, f"qk:rf:{kid}", fake3, now_ms=T2)
    assert answers_r == [MSG_BAD_BUTTON]
    assert out_r.get("ok") is False
    assert out_r.get("stop_wrong_side") is not True
    conn.close()
    conn2.close()
    conn3.close()


# --- S22: rf refusal (non wrong-side), low balance --------------------------


def test_s22_rf_low_balance_refusal_markup_and_answer_none(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S22: rf rebuild failure (margin) → refresh-only markup, answer None."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s22.db")
    _seed_settings(conn)
    cid = _flow_to_quoted(conn, _happy_fake())
    low = QuickFake(balance=Decimal("1"), available=Decimal("1"))
    _, answers, out, edits = _run_qk(conn, f"qk:rf:{cid}", low, now_ms=T1)
    assert answers == [None]
    assert out.get("ok") is False
    assert out.get("reason") in {"margin_cap", "below_min_notional", "size_failed", "read_failed"}
    assert edits[-1]["reply_markup"] == _refresh_only_markup(cid)
    reason = str(out.get("reason"))
    assert edits[-1]["text"] == _decorate_card_text(REFUSAL.get(reason, reason))
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "quoted"
    )
    conn.close()


# --- S23: rf success return + edit ------------------------------------------------


def test_s23_rf_success_dict_edit_and_answers_none(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S23: successful rf → refreshed dict, quoted markup, answer None."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s23.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    pre = conn.execute("SELECT stop_kind, stop_value FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    row = dict(conn.execute("SELECT * FROM assist_quick_card WHERE id=?", (cid,)).fetchone())
    fake.marks["SUIUSDT"] = Decimal("2.02")
    expect = build_card_for_row(conn, fake, dict(row), now_ms=T1)
    assert expect.get("ok")
    _, answers, out, edits = _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=T1)
    assert answers == [None]
    assert out == {"ok": True, "refreshed": True}
    assert edits[-1]["reply_markup"] == _quoted_markup(cid)
    assert edits[-1]["text"] == _decorate_card_text(str(expect["text"]))
    post = conn.execute("SELECT stop_kind, stop_value FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert (post["stop_kind"], post["stop_value"]) == (pre["stop_kind"], pre["stop_value"])
    conn.close()


# --- S24: qk:rg low balance + week_loss pause ---------------------------------


def test_s24_qk_rg_low_balance_rolls_back(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S24: qk:rg margin_cap refusal before txn — no registration row."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s24a.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    import jarvise_assist.quick_m1a1 as m1

    monkeypatch.setattr(
        m1,
        "build_card_at_quoted_stop",
        lambda *_a, **_k: {"ok": False, "reason": "margin_cap", "text": REFUSAL["margin_cap"]},
    )
    sent, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out == {"ok": False, "reason": "margin_cap"}
    assert answers == [None]
    assert sent == [{"text": REFUSAL["margin_cap"]}]
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert row["registration_id"] is None
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0
    conn.close()


def test_s24_qk_rg_week_loss_pause(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S24: qk:rg refuses under main week-loss pause before txn."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s24b.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    until = next_0820_bangkok_ms(NOW)
    monkeypatch.setattr(
        "jarvise_assist.quick_m1a1.main_week_loss_pause_until",
        lambda *_a, **_k: until,
    )
    sent, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out == {"ok": False, "reason": "week_loss_pause"}
    assert answers == [None]
    assert sent[0]["text"] == _refusal_week_loss(until, now_ms=NOW)
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert row["registration_id"] is None
    conn.close()


# --- S25: qk:rg except paths, success dict, qk:zz --------------------------------


def test_s25_qk_rg_txn_raise_leaves_quoted(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S25 A2: txn raise rolls back — quoted card, no registration row."""
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s25a.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    monkeypatch.setattr(
        m1,
        "create_quick_registration_from_card",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert answers == [m1.MSG_REGISTER_FAILED]
    assert out == {"ok": False, "reason": "read_failed"}
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert row["registration_id"] is None
    conn.close()


def test_s25_qk_rg_send_fail_still_registered(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S25 A2: post-commit send fail still returns registration_id; answer None."""
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s25b.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    monkeypatch.setattr(
        m1,
        "registration_success_message",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("send")),
    )
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    rid = conn.execute("SELECT registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()[
        "registration_id"
    ]
    assert rid
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 1
    assert answers == [None]
    assert out == {"ok": True, "registration_id": rid}
    conn.close()


def test_s25_qk_rg_success_dict_and_qk_zz_bad_op(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S25: success dict keys; unknown op qk:zz → bad button."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s25c.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    _, _, out_ok, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out_ok.get("ok") is True
    assert out_ok.get("registration_id")
    _run_qk(conn, "qk:sd:l", fake)
    cid_draft = conn.execute(
        "SELECT id FROM assist_quick_card WHERE status='pick_symbol' ORDER BY created_at_ms DESC LIMIT 1"
    ).fetchone()["id"]
    _, answers_zz, out_zz, _ = _run_qk(conn, f"qk:zz:{cid_draft}", fake)
    assert answers_zz == [MSG_BAD_BUTTON]
    assert out_zz.get("ok") is False
    conn.close()


# --- S26: qk:x quoted cancel + race registered ---------------------------------


def test_s26_qk_x_cancel_quoted_and_race_registered(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S26: qk:x cancels quoted; concurrent register → bad button."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s26a.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    _, answers, out, edits = _run_qk(conn, f"qk:x:{cid}", fake)
    assert answers == [None]
    assert out == {"ok": True, "cancelled": True}
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "cancelled"
    )
    assert edits[-1]["text"] == "ยกเลิกแล้ว"

    conn2 = open_db(tmp_path / "s26b.db")
    _seed_settings(conn2)
    cid2 = _flow_to_quoted(conn2, fake)

    class _RaceConn:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def execute(self, sql: str, *args: Any) -> Any:
            if "SET status='cancelled'" in sql and "quoted" in sql:
                self._inner.execute(
                    "UPDATE assist_quick_card SET status='registered', updated_at_ms=? WHERE id=?",
                    (T1, cid2),
                )
                self._inner.commit()
            return self._inner.execute(sql, *args)

        def commit(self) -> None:
            self._inner.commit()

        def rollback(self) -> None:
            self._inner.rollback()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    _, answers2, out2, _ = _run_qk(_RaceConn(conn2), f"qk:x:{cid2}", fake, now_ms=T1)
    assert answers2 == [MSG_BAD_BUTTON]
    assert out2.get("ok") is False
    conn.close()
    conn2.close()


# --- S27: inner _edit send fallback on qk:x -----------------------------------


def test_s27_qk_x_edit_fallback_without_chat_or_mid(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S27: missing chat/message_id or edit false → send fallback still cancels."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s27.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    sends: list[dict[str, Any]] = []

    def send(t: str, **kw: Any) -> int | None:
        mid = 200 + len(sends)
        sends.append({"text": t, **kw})
        return mid

    def edit_fail(*_a: Any, **_k: Any) -> bool:
        return False

    answers, answer = _answer_capture()
    out = handle_quick_trade_callback(
        conn,
        f"qk:x:{cid}",
        futures=fake,
        send=send,
        answer=lambda t: answer("cq", text=t),
        edit=edit_fail,
        message={"message_id": None, "chat": {}},
        now_ms=T1,
    )
    assert out.get("cancelled") is True
    assert sends and sends[-1]["text"] == "ยกเลิกแล้ว"
    row = conn.execute("SELECT message_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert int(row["message_id"]) == 200
    assert answers == [None]

    conn.execute("UPDATE assist_quick_card SET status='registered' WHERE id=?", (cid,))
    conn.commit()
    sends.clear()
    out2 = handle_quick_trade_callback(
        conn,
        f"qk:x:{cid}",
        futures=fake,
        send=lambda *_a, **_k: None,
        answer=lambda t: answer("cq2", text=t),
        edit=edit_fail,
        message={"message_id": 1, "chat": {"id": 1}},
        now_ms=T2,
    )
    assert out2.get("idempotent") is True
    assert answers == [None, MSG_ALREADY_REGISTERED]
    assert not sends

    conn2 = open_db(tmp_path / "s27c.db")
    _seed_settings(conn2)
    cid3 = _flow_to_quoted(conn2, fake)
    edits3: list[dict[str, Any]] = []
    sends3: list[dict[str, Any]] = []

    def send3(t: str, **kw: Any) -> int:
        mid = 400 + len(sends3)
        sends3.append({"text": t, **kw})
        return mid

    def edit3(*_a: Any, **_k: Any) -> bool:
        edits3.append(dict(_k, text=_a[1] if len(_a) > 1 else ""))
        return False

    answers3, answer3 = _answer_capture()
    out3 = handle_quick_trade_callback(
        conn2,
        f"qk:x:{cid3}",
        futures=fake,
        send=send3,
        answer=lambda t: answer3("cq3", text=t),
        edit=edit3,
        message={"message_id": 5, "chat": {}},
        now_ms=T3,
    )
    assert out3 == {"ok": True, "cancelled": True}
    assert edits3 == []
    assert sends3 == [{"text": "ยกเลิกแล้ว", "reply_markup": {"inline_keyboard": []}}]
    assert answers3 == [None]
    conn.close()
    conn2.close()


# --- S28: _edit_draft_message -------------------------------------------------


def test_s28_edit_draft_message_paths(tmp_path: Any) -> None:
    """S28: _edit_draft_message edit vs send branches."""
    conn = open_db(tmp_path / "s28.db")
    cid = "cafebabe"
    markup_b = _stop_step_markup(cid)
    edits: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []

    def edit_ok(mid: int, text: str, **kw: Any) -> bool:
        edits.append({"mid": mid, "text": text, **kw})
        return True

    def send(mid_text: str, **kw: Any) -> int:
        mid = 300 + len(sends)
        sends.append({"text": mid_text, **kw})
        return mid

    _edit_draft_message(
        conn,
        card_id=cid,
        message_id=10,
        text="a",
        markup=None,
        send=send,
        edit=edit_ok,
    )
    assert edits == [{"mid": 10, "text": "a", "reply_markup": None}]
    assert sends == []

    _edit_draft_message(
        conn,
        card_id=cid,
        message_id=10,
        text="b",
        markup=markup_b,
        send=send,
        edit=lambda *_a, **_k: False,
    )
    assert edits == [{"mid": 10, "text": "a", "reply_markup": None}]
    assert sends == [{"text": "b", "reply_markup": markup_b}]

    conn.execute(
        "INSERT INTO assist_quick_card (id, side, status, created_at_ms, updated_at_ms) VALUES (?, 'long', 'pick_symbol', ?, ?)",
        (cid, T0, T0),
    )
    conn.commit()
    _edit_draft_message(
        conn,
        card_id=cid,
        message_id=None,
        text="c",
        markup=None,
        send=lambda *_a, **_k: None,
        edit=edit_ok,
    )
    assert edits == [{"mid": 10, "text": "a", "reply_markup": None}]
    assert sends == [{"text": "b", "reply_markup": markup_b}]
    mid = conn.execute("SELECT message_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()[0]
    assert mid is None
    conn.close()


# --- S29: typed draft refusal % and price -------------------------------------


def test_s29_typed_draft_pct_and_price_paths(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S29: 0.5% and price 99000 with low balance → exact refusal card text."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    low = QuickFake(balance=Decimal("1"), available=Decimal("1"), marks={"BTCUSDT": Decimal("100000")})
    conn = open_db(tmp_path / "s29a.db")
    _seed_settings(conn)
    _run_qk(conn, "qk:sd:l", low)
    cid = conn.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
    _run_qk(conn, f"qk:sy:{cid}:BTCUSDT", low)
    row = dict(conn.execute("SELECT * FROM assist_quick_card WHERE id=?", (cid,)).fetchone())
    row["stop_kind"] = "pct"
    row["stop_value"] = "0.5"
    expect_pct = build_card_for_row(conn, low, row, now_ms=T1)
    edits: list[dict[str, Any]] = []

    def cap_edit(mid: int, text: str, **kw: Any) -> bool:
        edits.append({"text": text, **kw})
        return True

    sent, send = _send_capture()
    out = process_update(
        conn,
        _msg("0.5%"),
        send=send,
        edit=cap_edit,
        futures=low,
        now_ms_value=T1,
    )
    assert out.get("ok") is False
    assert edits[-1]["text"] == _decorate_card_text(str(expect_pct["text"]))
    assert edits[-1]["reply_markup"] == _stop_step_markup(cid)

    conn2 = open_db(tmp_path / "s29b.db")
    _seed_settings(conn2)
    _run_qk(conn2, "qk:sd:l", low)
    cid2 = conn2.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
    _run_qk(conn2, f"qk:sy:{cid2}:BTCUSDT", low)
    row2 = dict(conn2.execute("SELECT * FROM assist_quick_card WHERE id=?", (cid2,)).fetchone())
    row2["stop_kind"] = "price"
    row2["stop_value"] = "99000"
    expect_price = build_card_for_row(conn2, low, row2, now_ms=T2)
    sent2, send2 = _send_capture()
    out2 = process_update(
        conn2,
        _msg("99000"),
        send=send2,
        edit=cap_edit,
        futures=low,
        now_ms_value=T2,
    )
    assert out2.get("ok") is False
    assert edits[-1]["text"] == _decorate_card_text(str(expect_price["text"]))
    assert edits[-1]["reply_markup"] == _stop_step_markup(cid2)
    assert (
        conn2.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid2,)).fetchone()["status"]
        == "pick_stop"
    )
    conn.close()
    conn2.close()


# --- S30: tag notice null risk/qty lines --------------------------------------


def test_s30_tag_notice_null_risk_and_qty_lines(tmp_path: Path) -> None:
    """S30: tag notice still emits risk/qty lines when stored nulls become zero."""
    conn = open_db(tmp_path / "s30.db")
    t0 = T0
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            status, created_at_ms, expires_at_ms, updated_at_ms, notice_sent_at_ms
        ) VALUES ('r1', 'BTCUSDT', 'long', 100, 90, 0.01, 'tagged', ?, ?, ?, NULL)
        """,
        (t0, t0 + REG_TTL_MS, t0),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status,
            entry_avg, entry_qty, risk_usdt, entry_time_ms, updated_at_ms, planned_stop
        ) VALUES ('t1', 'r1', 'BTCUSDT', 'long', 'open', 100, NULL, NULL, ?, ?, 90)
        """,
        (t0, t0),
    )
    conn.commit()
    sent: list[str] = []
    expected = "\n".join(
        [
            (
                "✅ จับไม้ quick ได้แล้ว — BTCUSDT LONG "
                f"เข้า {fmt_price(Decimal('100'))} · qty {fmt_qty(Decimal('0'))} · "
                f"stop ที่ลงทะเบียน {fmt_price(Decimal('90'))}"
            ),
            f"ความเสี่ยงจริง ≈ {fmt_price(Decimal('0'))} USDT · บอทไม่ได้วาง stop — ตั้ง stop ในแอปเอง",
            quick_mode_tag(),
        ]
    )
    _send_tag_notice(conn, lambda t, **_k: sent.append(t), registration_id="r1", client=None, now_ms=t0)
    assert sent == [expected]
    conn.close()


# --- S33: first_fill fallback -------------------------------------------------


def test_s33_first_fill_fallback_two_cases(tmp_path: Path) -> None:
    """S33: first_fill fallback, fill id ordering, and no-candidate break."""
    conn = open_db(tmp_path / "s33.db")
    t0 = T0
    buy_t = t0 + 10_000
    rid, _ = create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1.0, now_ms=t0
    )
    fake = FakeFutures(trades=[_fill(tid=7, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t)])
    sync_quick_trades(conn, fake, now_ms=buy_t + 1000, send=lambda *_a, **_k: True)
    trade = conn.execute(
        "SELECT id, entry_trade_ids FROM assist_quick_trade WHERE registration_id=?", (rid,)
    ).fetchone()
    conn.execute(
        "UPDATE assist_quick_trade SET entry_trade_ids='[999]', status='open' WHERE id=?",
        (trade["id"],),
    )
    conn.commit()
    sell_t = buy_t + 50_000
    fake2 = FakeFutures(
        trades=[
            _fill(tid=7, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t),
            _fill(tid=8, symbol="BTCUSDT", side="SELL", price=110, qty=1, time_ms=sell_t, realized=10),
        ]
    )
    sync_quick_trades(conn, fake2, now_ms=sell_t + 1000, send=lambda *_a, **_k: True)
    row = conn.execute(
        "SELECT status, exit_trade_ids FROM assist_quick_trade WHERE id=?", (trade["id"],)
    ).fetchone()
    assert row["status"] == "closed"
    assert "8" in (row["exit_trade_ids"] or "")

    conn2 = open_db(tmp_path / "s33b.db")
    rid2, _ = create_quick_registration(
        conn2, symbol="ETHUSDT", side="short", planned_entry=100, planned_stop=110, qty=1.0, now_ms=t0
    )
    sell_open = t0 + 20_000
    fake3 = FakeFutures(
        trades=[_fill(tid=20, symbol="ETHUSDT", side="SELL", price=100, qty=1, time_ms=sell_open)]
    )
    sync_quick_trades(conn2, fake3, now_ms=sell_open + 1000, send=lambda *_a, **_k: True)
    trade2 = conn2.execute(
        "SELECT id, entry_trade_ids FROM assist_quick_trade WHERE registration_id=?", (rid2,)
    ).fetchone()
    conn2.execute(
        "UPDATE assist_quick_trade SET entry_trade_ids='[]', status='open' WHERE id=?",
        (trade2["id"],),
    )
    conn2.commit()
    buy_close = sell_open + 40_000
    fake4 = FakeFutures(
        trades=[
            _fill(tid=20, symbol="ETHUSDT", side="SELL", price=100, qty=1, time_ms=sell_open),
            _fill(tid=21, symbol="ETHUSDT", side="BUY", price=90, qty=1, time_ms=buy_close, realized=10),
        ]
    )
    sync_quick_trades(conn2, fake4, now_ms=buy_close + 1000, send=lambda *_a, **_k: True)
    row2 = conn2.execute(
        "SELECT status, entry_trade_ids FROM assist_quick_trade WHERE id=?", (trade2["id"],)
    ).fetchone()
    assert row2["status"] == "closed"
    assert "20" in (row2["entry_trade_ids"] or "")

    conn3 = open_db(tmp_path / "s33c.db")
    t_same = t0 + 30_000
    rid3, _ = create_quick_registration(
        conn3, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1.0, now_ms=t0
    )
    fake5 = FakeFutures(
        trades=[
            _fill(tid=50, symbol="BTCUSDT", side="BUY", price=100, qty=0.5, time_ms=t_same),
            _fill(tid=51, symbol="BTCUSDT", side="BUY", price=101, qty=0.5, time_ms=t_same),
        ]
    )
    sync_quick_trades(conn3, fake5, now_ms=t_same + 1000, send=lambda *_a, **_k: True)
    trade3 = conn3.execute(
        "SELECT entry_trade_ids, entry_avg FROM assist_quick_trade WHERE registration_id=?", (rid3,)
    ).fetchone()
    assert trade3["entry_trade_ids"] == "[50, 51]"
    assert float(trade3["entry_avg"]) == pytest.approx(100.5)

    conn4 = open_db(tmp_path / "s33d.db")
    rid4, _ = create_quick_registration(
        conn4, symbol="BNBUSDT", side="long", planned_entry=100, planned_stop=90, qty=1.0, now_ms=t0
    )
    t_open = t0 + 5_000
    fake6 = FakeFutures(trades=[_fill(tid=1, symbol="BNBUSDT", side="BUY", price=100, qty=1, time_ms=t_open)])
    sync_quick_trades(conn4, fake6, now_ms=t_open + 1000, send=lambda *_a, **_k: True)
    trade4 = conn4.execute("SELECT id FROM assist_quick_trade WHERE registration_id=?", (rid4,)).fetchone()
    conn4.execute(
        "UPDATE assist_quick_trade SET entry_trade_ids='[]', status='open' WHERE id=?",
        (trade4["id"],),
    )
    conn4.commit()
    fake7 = FakeFutures(
        trades=[_fill(tid=99, symbol="BNBUSDT", side="SELL", price=100, qty=1, time_ms=t_open + 10_000)]
    )
    info = sync_quick_trades(conn4, fake7, now_ms=t_open + 20_000, send=lambda *_a, **_k: True)
    assert info.get("updated", 0) == 0
    assert (
        conn4.execute("SELECT status FROM assist_quick_trade WHERE id=?", (trade4["id"],)).fetchone()[
            "status"
        ]
        == "open"
    )
    conn.close()
    conn2.close()
    conn3.close()
    conn4.close()


# --- S34: _send_close_notice direct -------------------------------------------


def test_s34_send_close_notice_direct_variants(tmp_path: Path) -> None:
    """S34: _send_close_notice whole messages — BNB, zero risk, idempotent mark."""
    tag = quick_mode_tag()

    def _close_msg(sym: str, side: str, net_usdt: float, r_line: str) -> str:
        label = _side_label(side)
        return f"🏁 ไม้ quick {sym} {label} ปิดแล้ว (ปิดใน Binance) — สุทธิ {net_usdt:+.2f} USDT · {r_line}\n{tag}"

    conn = open_db(tmp_path / "s34.db")
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            risk_usdt, close_notice_at_ms, updated_at_ms
        ) VALUES ('t1', 'r1', 'BTCUSDT', 'long', 'closed', 5, 0.5, 10, NULL, 1)
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
            "net_usdt": 5.0,
            "net_r": 0.5,
            "risk_usdt": 10.0,
            "commission_other_asset": None,
        },
        now_ms=T0,
    )
    assert sent == [
        _close_msg(
            "BTCUSDT",
            "long",
            5.0,
            "+0.50R (รวมค่าธรรมเนียมจริง + funding)",
        )
    ]
    _send_close_notice(
        conn,
        send,
        trade_id="t1",
        client=_OrdersFake(orders=[]),
        payload={"symbol": "BTCUSDT", "side": "long", "net_usdt": 5.0, "net_r": 0.5, "risk_usdt": 10.0},
        now_ms=T1,
    )
    assert len(sent) == 1

    _send_close_notice(
        conn,
        send,
        trade_id="t1",
        client=None,
        payload={"symbol": "BTCUSDT", "side": "long", "net_usdt": 1.0, "net_r": None, "risk_usdt": 0.0},
        now_ms=T2,
        skip_reason="bot",
    )
    assert len(sent) == 1

    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            risk_usdt, commission_other_asset, close_notice_at_ms, updated_at_ms
        ) VALUES ('t2', 'r2', 'ETHUSDT', 'short', 'closed', 1, NULL, 8, 'BNB', NULL, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            risk_usdt, commission_other_asset, close_notice_at_ms, updated_at_ms
        ) VALUES ('t3', 'r3', 'SOLUSDT', 'long', 'closed', 2, NULL, 0, NULL, NULL, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            risk_usdt, commission_other_asset, close_notice_at_ms, updated_at_ms
        ) VALUES ('t4', 'r4', 'XRPUSDT', 'long', 'closed', 3, NULL, 5, NULL, NULL, 1)
        """
    )
    conn.commit()
    _send_close_notice(
        conn,
        send,
        trade_id="t2",
        client=None,
        payload={
            "symbol": "ETHUSDT",
            "side": "short",
            "net_usdt": 1.0,
            "net_r": None,
            "risk_usdt": 8.0,
            "commission_other_asset": "BNB",
        },
        now_ms=T3,
    )
    _send_close_notice(
        conn,
        send,
        trade_id="t3",
        client=None,
        payload={
            "symbol": "SOLUSDT",
            "side": "long",
            "net_usdt": 2.0,
            "net_r": None,
            "risk_usdt": 0.0,
            "commission_other_asset": None,
        },
        now_ms=T3,
    )
    _send_close_notice(
        conn,
        send,
        trade_id="t4",
        client=None,
        payload={
            "symbol": "XRPUSDT",
            "side": "long",
            "net_usdt": 3.0,
            "net_r": None,
            "risk_usdt": 5.0,
            "commission_other_asset": None,
        },
        now_ms=T3,
    )
    assert sent[1:] == [
        _close_msg("ETHUSDT", "short", 1.0, "R คำนวณไม่ได้ (ค่าธรรมเนียมเป็น BNB)"),
        _close_msg("SOLUSDT", "long", 2.0, "R คำนวณไม่ได้"),
        _close_msg("XRPUSDT", "long", 3.0, "R คำนวณไม่ได้"),
    ]
    conn.close()


# --- Optional helpers: _recent_quick_symbols, _side_label ---------------------


def test_optional_recent_quick_symbols_and_side_label(tmp_path: Any) -> None:
    """Optional: _recent_quick_symbols order/fallback and _side_label."""
    conn = open_db(tmp_path / "sym.db")
    now = T0
    for sym, off in (("SOLUSDT", 0), ("ADAUSDT", 1000), ("XRPUSDT", 2000)):
        conn.execute(
            """
            INSERT INTO assist_quick_registration (
                id, symbol, side, planned_entry, planned_stop, qty,
                status, created_at_ms, expires_at_ms, updated_at_ms
            ) VALUES (?, ?, 'long', 1, 0.9, 1, 'open', ?, ?, ?)
            """,
            (f"r-{sym}", sym, now + off, now + off + 60_000, now + off),
        )
    conn.commit()
    assert _recent_quick_symbols(conn, limit=6) == [
        "XRPUSDT",
        "ADAUSDT",
        "SOLUSDT",
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert _recent_quick_symbols(conn, limit=2) == ["XRPUSDT", "ADAUSDT"]
    assert _side_label("long") == "LONG"
    assert _side_label("short") == "SHORT"
    conn.close()


# --- S35: quote expiry / clock skew -------------------------------------------


def test_s35a_quote_is_expired_skew_and_ttl() -> None:
    """S35a: _quote_is_expired respects TTL and future clock skew."""
    base = T0
    assert not _quote_is_expired(base, base + QUICK_CARD_TTL_MS)
    assert _quote_is_expired(base, base + QUICK_CARD_TTL_MS + 1)
    assert _quote_is_expired(0, base)
    assert _quote_is_expired(base, base - QUOTE_CLOCK_SKEW_MS - 1)


def test_s35b_qk_rg_quote_expired_exact_body(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S35b: expired quote on register → exact edit body and no registration."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s35b.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    row = conn.execute("SELECT quoted_at_ms FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    late = int(row["quoted_at_ms"]) + QUICK_CARD_TTL_MS + 1
    _, answers, out, edits = _run_qk(conn, f"qk:rg:{cid}", fake, now_ms=late)
    expected = _decorate_card_text("\n".join([MSG_CARD_QUOTE_EXPIRED, quick_mode_tag()]))
    assert out == {"ok": False, "quote_expired": True}
    assert answers == [MSG_CARD_QUOTE_EXPIRED]
    assert edits[-1]["text"] == expected
    assert edits[-1]["reply_markup"] == _refresh_only_markup(cid)
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0
    conn.close()


def test_s35c_registered_without_rid_recovers_then_rg(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S35c: registered row without registration_id reverts to quoted and can register."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "s35c.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    conn.execute(
        "UPDATE assist_quick_card SET status='registered', registration_id=NULL WHERE id=?",
        (cid,),
    )
    conn.commit()
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert answers == [None]
    assert out.get("ok") is True
    assert out.get("registration_id")
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "registered"
    assert row["registration_id"] == out["registration_id"]
    conn.close()


# --- S36: callback_data guard on quoted markup --------------------------------


def test_s36_quoted_markup_rejects_oversized_card_id() -> None:
    """S36: _quoted_markup raises when any callback_data would exceed 64 bytes."""
    long_id = "q" * 60
    with pytest.raises(ValueError, match="callback_data exceeds 64 bytes"):
        _quoted_markup(long_id)
