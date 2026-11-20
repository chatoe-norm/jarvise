"""M1a-1 fix round 4 — mutation-backed tests (M8, R6, M16, R1, M14, …)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_listen import _send_capture
from tests.test_assist_quick import _fill
from tests.test_assist_quick_card import (
    _happy_fake,
    _msg,
    _seed_settings,
    build_quick_card,
)
from tests.test_assist_quick_m1a1_round1 import NOW, _flow_to_quoted, _run_qk
from tests.test_assist_real_trades import FakeFutures

import jarvise_assist.quick as quick_mod
from jarvise_assist.execute import _format_owner_order_line
from jarvise_assist.keyboard import BTN_QUICK
from jarvise_assist.listen import process_update
from jarvise_assist.menu import match_menu_label
from jarvise_assist.order_lines import (
    BOT_CLOSING_ORDER_NOTE,
    QUICK_LIST_CLOSING_NOTE,
    format_closing_order_line,
)
from jarvise_assist.quick import (
    REG_EXPIRE_GRACE_MS,
    REG_TTL_MS,
    create_quick_registration,
    render_quick_menu,
    sync_quick_trades,
)
from jarvise_assist.quick_card import (
    REFUSAL,
    handle_quick_register_callback,
    quick_mode_tag,
)
from jarvise_assist.quick_m1a1 import (
    MSG_BAD_BUTTON,
    MSG_REGISTER_FAILED,
    MSG_STOP_FAR,
    STOP_STEP_HINT,
    _quoted_markup,
    _stop_step_markup,
    handle_quick_draft_text,
    registration_success_message,
)
from jarvise_assist.store import write_settings
from jarvise_ingest.db import open_db

T0 = NOW
T1 = T0 + 1000
T2 = T0 + 2000
T3 = T0 + 3000


def _rows(conn: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(r) for r in conn.execute("SELECT * FROM assist_quick_card"))


# --- A: M8 two drafts -------------------------------------------------------


def test_two_drafts_rf_wrong_side_cancels_new(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant M8."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "2d.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"BTCUSDT": Decimal("100000")})
    old_id = _flow_to_quoted(conn, fake, now_ms=T0)
    conn.execute(
        """
        UPDATE assist_quick_card
        SET symbol='BTCUSDT', stop_kind='price', stop_value='120000'
        WHERE id=?
        """,
        (old_id,),
    )
    conn.commit()
    _run_qk(conn, "qk:sd:l", fake, now_ms=T1)
    new_id = conn.execute(
        "SELECT id FROM assist_quick_card WHERE status='pick_symbol' ORDER BY created_at_ms DESC"
    ).fetchone()["id"]
    _run_qk(conn, f"qk:rf:{old_id}", fake, now_ms=T2)
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (new_id,)).fetchone()["status"]
        == "cancelled"
    )
    sent, send = _send_capture()
    edits: list[dict[str, Any]] = []

    def edit_fn(mid: int, text: str, **kw: Any) -> bool:
        edits.append({"text": text, **kw})
        return True

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    out = process_update(
        conn,
        _msg("99000"),
        send=send,
        edit=edit_fn,
        futures=fake,
        now_ms_value=T3,
    )
    assert out.get("handled")
    row = conn.execute(
        "SELECT status, stop_kind, stop_value FROM assist_quick_card WHERE id=?", (old_id,)
    ).fetchone()
    assert row["status"] == "quoted"
    assert row["stop_kind"] == "price"
    assert row["stop_value"] == "99000"
    conn.close()


# --- B: R6 wrong-side when rowcount 0 ----------------------------------------


def test_rf_wrong_side_update_zero_leaves_new_draft(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant R6."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "r6.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"BTCUSDT": Decimal("100000")})
    old_id = _flow_to_quoted(conn, fake, now_ms=T0)
    conn.execute(
        "UPDATE assist_quick_card SET symbol='BTCUSDT', stop_kind='price', stop_value='120000' WHERE id=?",
        (old_id,),
    )
    conn.commit()
    _run_qk(conn, "qk:sd:l", fake, now_ms=T1)
    new_id = conn.execute(
        "SELECT id FROM assist_quick_card WHERE status='pick_symbol' ORDER BY created_at_ms DESC"
    ).fetchone()["id"]
    import jarvise_assist.quick_m1a1 as m1

    def fake_build(conn_a: Any, futures_a: Any, row: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        conn_a.execute("UPDATE assist_quick_card SET status='registered' WHERE id=?", (row["id"],))
        conn_a.commit()
        return {"ok": False, "reason": "stop_wrong_side", "text": "x"}

    monkeypatch.setattr(m1, "build_card_for_row", fake_build)
    _, answers, _, _ = _run_qk(conn, f"qk:rf:{old_id}", fake, now_ms=T2)
    assert answers == [MSG_BAD_BUTTON]
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (new_id,)).fetchone()["status"]
        == "pick_symbol"
    )
    conn.close()


# --- C: M16 persist guard ----------------------------------------------------


def test_rf_does_not_downgrade_registered(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant M16."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "m16.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    conn.execute("UPDATE assist_quick_card SET status='registered' WHERE id=?", (cid,))
    conn.commit()
    import jarvise_assist.quick_m1a1 as m1

    real = m1.build_card_for_row

    def wrap(conn_a: Any, futures_a: Any, row: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        return real(conn_a, futures_a, row, now_ms=now_ms)

    monkeypatch.setattr(m1, "build_card_for_row", wrap)
    _run_qk(conn, f"qk:rf:{cid}", fake)
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "registered"
    )
    conn.close()


# --- D: R1 rollback uncommitted insert ---------------------------------------


def test_qk_rg_uncommitted_insert_rolled_back(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant R1."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    db_path = tmp_path / "r1.db"
    conn = open_db(db_path)
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)

    import jarvise_assist.quick_m1a1 as m1

    def boom(conn_a: Any, **kwargs: Any) -> tuple[str | None, str | None]:
        conn_a.execute(
            """
            INSERT INTO assist_quick_registration (
                id, symbol, side, planned_entry, planned_stop, qty,
                status, created_at_ms, expires_at_ms, updated_at_ms
            ) VALUES ('orphan', 'BTCUSDT', 'long', 1, 0.9, 1, 'open', 1, 2, 1)
            """
        )
        raise RuntimeError("no commit")

    monkeypatch.setattr(m1, "create_quick_registration_from_card", boom)
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    conn.close()
    conn2 = open_db(db_path)
    assert conn2.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0
    assert (
        conn2.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "quoted"
    )
    assert answers == [MSG_REGISTER_FAILED]
    assert out.get("ok") is False
    conn2.close()


# --- E: except safety --------------------------------------------------------


class _RollbackBombConn:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def rollback(self) -> None:
        raise RuntimeError("rollback boom")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_qk_rg_rollback_raises_still_answers(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    inner = open_db(tmp_path / "erb.db")
    conn = _RollbackBombConn(inner)
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    monkeypatch.setattr(
        m1, "create_quick_registration_from_card", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert len(answers) == 1
    assert answers[0] == MSG_REGISTER_FAILED
    assert out.get("ok") is False


def test_qk_rg_recovery_raises_still_answers(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "errb.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    monkeypatch.setattr(
        m1,
        "create_quick_registration_from_card",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    monkeypatch.setattr(
        m1,
        "_rollback_registered_to_quoted",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("rb")),
    )
    _, answers, _, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert answers == [MSG_REGISTER_FAILED]


def test_qk_rg_refusal_answer_once(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvise_assist.quick_m1a1 as m1

    calls: list[Any] = []

    def count_answer(cq_id: str, *, text: str | None = None) -> bool:
        calls.append(text)
        if len(calls) == 1 and text is None:
            raise RuntimeError("answer boom")
        return True

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "ra.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    monkeypatch.setattr(
        m1,
        "create_quick_registration_from_card",
        lambda *_a, **_k: (None, "open_registration"),
    )
    upd = {
        "callback_query": {
            "id": "1",
            "data": f"qk:rg:{cid}",
            "message": {"chat": {"id": 1}, "message_id": 1},
        }
    }
    out = process_update(
        conn,
        upd,
        send=lambda t, **kw: 1,
        answer=count_answer,
        edit=lambda *_a, **_k: True,
        futures=fake,
        now_ms_value=NOW,
    )
    assert len(calls) == 1
    assert out.get("ok") is False


class _RowcountZeroConn:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def execute(self, sql: str, *a: Any) -> Any:
        if "UPDATE assist_quick_card" in sql and "pick_symbol" in sql:
            self._inner.execute("BEGIN")
            cur = type("C", (), {"rowcount": 0})()
            return cur
        return self._inner.execute(sql, *a)

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()

    @property
    def in_transaction(self) -> bool:
        return self._inner.in_transaction

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_typed_symbol_rollback_on_rowcount_zero(tmp_path: Any) -> None:
    """Mutant R825."""
    inner = open_db(tmp_path / "r825.db")
    conn = _RowcountZeroConn(inner)
    _seed_settings(conn)
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, status, created_at_ms, updated_at_ms, message_id
        ) VALUES ('c1', 'long', NULL, 'pick_symbol', ?, ?, 1)
        """,
        (NOW, NOW),
    )
    conn.commit()
    out = handle_quick_draft_text(
        conn,
        "BTC",
        futures=_happy_fake(),
        send=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=NOW,
    )
    assert out is None
    assert not conn.in_transaction
    inner.close()


# --- F: M14b / M14c ----------------------------------------------------------


class _MarkFailConn:
    def __init__(self, inner: Any, *, needle: str) -> None:
        self._inner = inner
        self._needle = needle

    def execute(self, sql: str, *a: Any) -> Any:
        if self._needle in sql:
            raise RuntimeError("mark fail")
        return self._inner.execute(sql, *a)

    def commit(self) -> None:
        self._inner.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_sync_tag_mark_fail_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant M14b."""
    conn = open_db(tmp_path / "m14b.db")
    t0 = T0
    rid, _ = create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1.0, now_ms=t0
    )
    buy_t = t0 + 60_000
    fake = FakeFutures(trades=[_fill(tid=1, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t)])
    wrap = _MarkFailConn(conn, needle="notice_sent_at_ms")
    sent: list[str] = []
    info = sync_quick_trades(wrap, fake, now_ms=buy_t + 1000, send=lambda t, **_k: sent.append(t) or True)
    assert info.get("tagged") == 1
    assert not sent


def test_sync_close_mark_fail_still_sends_tag(tmp_path: Path) -> None:
    """Mutant M14c."""
    conn = open_db(tmp_path / "m14c.db")
    t0 = T0
    rid, _ = create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1.0, now_ms=t0
    )
    buy_t = t0 + 60_000
    sell_t = buy_t + 60_000
    fake = FakeFutures(
        trades=[
            _fill(tid=1, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t),
            _fill(tid=2, symbol="BTCUSDT", side="SELL", price=110, qty=1, time_ms=sell_t, realized=10),
        ]
    )
    sent: list[str] = []
    sync_quick_trades(conn, fake, now_ms=buy_t + 1000, send=lambda t, **_k: sent.append(t) or True)
    wrap = _MarkFailConn(conn, needle="close_notice_at_ms")
    sync_quick_trades(wrap, fake, now_ms=sell_t + 1000, send=lambda t, **_k: sent.append(t) or True)
    assert len(sent) == 1
    assert "จับไม้ quick" in sent[0]


# --- G: sync notices (owner copy) --------------------------------------------


class _OrdersFake(FakeFutures):
    def __init__(
        self, *a: Any, orders: list[dict[str, Any]] | None = None, boom_orders: bool = False, **kw: Any
    ) -> None:
        super().__init__(*a, **kw)
        self._leftover_orders = list(orders or [])
        self._boom_orders = boom_orders

    def all_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        if self._boom_orders:
            raise RuntimeError("orders boom")
        return [o for o in self._leftover_orders if str(o.get("symbol", "")).upper() == symbol.upper()]


def _sync_buy_sell(
    tmp_path: Path,
    *,
    with_close: bool = True,
    income: list[dict[str, Any]] | None = None,
) -> tuple[Any, FakeFutures, list[str], str]:
    conn = open_db(tmp_path / "sync.db")
    t0 = T0
    rid, _ = create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1.0, now_ms=t0
    )
    buy_t = t0 + 60_000
    trades = [_fill(tid=1, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t)]
    if with_close:
        sell_t = buy_t + 60_000
        trades.append(
            _fill(tid=2, symbol="BTCUSDT", side="SELL", price=110, qty=1, time_ms=sell_t, realized=10)
        )
        end_ms = sell_t + 1000
    else:
        end_ms = buy_t + 1000
    fake = FakeFutures(trades=trades, income=income or [])
    sent: list[str] = []

    def send(msg: str, **_kw: Any) -> bool:
        sent.append(msg)
        return True

    sync_quick_trades(conn, fake, now_ms=end_ms, send=send)
    return conn, fake, sent, rid


def test_sync_tag_notice_three_lines(tmp_path: Path) -> None:
    conn, _fake, sent, _rid = _sync_buy_sell(tmp_path, with_close=False)
    assert len(sent) == 1
    lines = sent[0].split("\n")
    assert len(lines) == 3
    label = quick_mod._side_menu_label("long")
    assert lines[0].startswith(f"✅ จับไม้ quick ได้แล้ว — BTCUSDT {label}")
    assert lines[1].startswith("ความเสี่ยงจริง ≈")
    assert lines[2] == quick_mode_tag()
    conn.close()


def test_sync_expire_notices_exact(tmp_path: Path) -> None:
    tag = quick_mode_tag()
    # no_fill
    conn = open_db(tmp_path / "enf.db")
    t0 = T0
    create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1, now_ms=t0
    )
    sent: list[str] = []
    sync_quick_trades(
        conn,
        FakeFutures(trades=[]),
        now_ms=t0 + REG_TTL_MS + REG_EXPIRE_GRACE_MS + 1,
        send=lambda t, **_k: sent.append(t) or True,
    )
    assert sent == [
        "\n".join(
            [
                "⌛ ลงทะเบียน quick BTCUSDT LONG หมดเวลา — ไม่พบไม้ภายใน 10 นาที (ไม่นับเป็น quick)",
                tag,
            ]
        )
    ]
    conn.close()

    # wrong_side
    conn2 = open_db(tmp_path / "ews.db")
    create_quick_registration(
        conn2, symbol="ETHUSDT", side="long", planned_entry=100, planned_stop=90, qty=1, now_ms=t0
    )
    sent2: list[str] = []
    sync_quick_trades(
        conn2,
        FakeFutures(
            trades=[_fill(tid=1, symbol="ETHUSDT", side="SELL", price=100, qty=1, time_ms=t0 + 1000)]
        ),
        now_ms=t0 + 5000,
        send=lambda t, **_k: sent2.append(t) or True,
    )
    assert sent2 == [
        "\n".join(
            [
                "⌛ ลงทะเบียน quick ETHUSDT LONG ถูกยกเลิก — ไม้แรกเป็นฝั่ง SHORT จึงนับเป็นไม้ปกติ (ระบบหลักเฝ้าให้)",
                tag,
            ]
        )
    ]
    conn2.close()

    # late_fill
    conn3 = open_db(tmp_path / "elf.db")
    create_quick_registration(
        conn3, symbol="SOLUSDT", side="long", planned_entry=100, planned_stop=90, qty=1, now_ms=t0
    )
    fill_late = t0 + REG_TTL_MS + REG_EXPIRE_GRACE_MS + 1
    sent3: list[str] = []
    sync_quick_trades(
        conn3,
        FakeFutures(trades=[_fill(tid=1, symbol="SOLUSDT", side="BUY", price=100, qty=1, time_ms=fill_late)]),
        now_ms=fill_late + 1000,
        send=lambda t, **_k: sent3.append(t) or True,
    )
    assert sent3 == [
        "\n".join(
            [
                "⌛ ลงทะเบียน quick SOLUSDT LONG หมดเวลา — ไม้ที่เปิดหลังหมดเวลาไม่นับเป็น quick",
                tag,
            ]
        )
    ]
    conn3.close()


def test_sync_close_notice_variants(tmp_path: Path) -> None:
    conn, _f, sent, _ = _sync_buy_sell(tmp_path)
    assert len(sent) == 2
    assert "+1.00R" in sent[1] or "+1.0" in sent[1]
    assert sent[1].strip().split("\n")[-1] == quick_mode_tag()
    conn.close()

    conn2 = open_db(tmp_path / "cnb.db")
    t0 = T0
    create_quick_registration(
        conn2, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1, now_ms=t0
    )
    buy_t, sell_t = t0 + 1000, t0 + 2000
    fake = FakeFutures(
        trades=[
            _fill(
                tid=1,
                symbol="BTCUSDT",
                side="BUY",
                price=100,
                qty=1,
                time_ms=buy_t,
                commission=0.001,
                commission_asset="BNB",
            ),
            _fill(
                tid=2,
                symbol="BTCUSDT",
                side="SELL",
                price=110,
                qty=1,
                time_ms=sell_t,
                realized=10,
                commission=0.001,
                commission_asset="BNB",
            ),
        ]
    )
    msgs: list[str] = []
    sync_quick_trades(conn2, fake, now_ms=sell_t + 1, send=lambda t, **_k: msgs.append(t) or True)
    assert any("R คำนวณไม่ได้ (ค่าธรรมเนียมเป็น BNB" in m for m in msgs)
    conn2.close()

    from jarvise_assist.quick import _send_close_notice

    conn3 = open_db(tmp_path / "c0r.db")
    conn3.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, net_usdt, net_r,
            close_notice_at_ms, updated_at_ms
        ) VALUES ('t0', 'r0', 'BTCUSDT', 'long', 'closed', 0.1, NULL, NULL, 1)
        """
    )
    conn3.commit()
    plain: list[str] = []
    _send_close_notice(
        conn3,
        lambda t, **_k: plain.append(t),
        trade_id="t0",
        client=None,
        payload={
            "symbol": "BTCUSDT",
            "side": "long",
            "net_usdt": 0.1,
            "net_r": None,
            "risk_usdt": 0.0,
            "commission_other_asset": None,
        },
        now_ms=T0,
    )
    assert plain[0].count("R คำนวณไม่ได้") == 1
    assert "ค่าธรรมเนียมเป็น" not in plain[0]
    conn3.close()


def test_sync_tag_then_close_order_and_at_most_once(tmp_path: Path) -> None:
    conn, fake, sent, _ = _sync_buy_sell(tmp_path)
    assert len(sent) == 2
    assert "จับไม้ quick" in sent[0]
    assert "ปิดแล้ว" in sent[1]
    n0 = len(sent)
    sync_quick_trades(conn, fake, now_ms=T0 + 500_000, send=lambda t, **_k: sent.append(t) or True)
    assert len(sent) == n0
    conn.close()


def test_sync_send_raise_not_retried(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "boom.db")
    t0 = T0
    create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1, now_ms=t0
    )
    buy_t = t0 + 1000
    fake = FakeFutures(trades=[_fill(tid=1, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t)])
    n_calls = 0

    def boom_send(_t: str, **_kw: Any) -> bool:
        nonlocal n_calls
        n_calls += 1
        raise RuntimeError("send boom")

    sync_quick_trades(conn, fake, now_ms=buy_t + 1, send=boom_send)
    sent2: list[str] = []
    sync_quick_trades(conn, fake, now_ms=buy_t + 2, send=lambda t, **_k: sent2.append(t) or True)
    assert n_calls == 1
    assert sent2 == []
    conn.close()


def test_sync_tag_select_fail_through_sync(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sel2.db")
    t0 = T0
    create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1, now_ms=t0
    )
    buy_t = t0 + 1000
    fake = FakeFutures(trades=[_fill(tid=1, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t)])

    class Wrap:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def execute(self, sql: str, *a: Any) -> Any:
            if sql.strip().startswith("SELECT * FROM assist_quick_trade WHERE registration_id"):
                raise RuntimeError("select boom")
            return self._inner.execute(sql, *a)

        def commit(self) -> None:
            self._inner.commit()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    now_ms = buy_t + 1
    wrap = Wrap(conn)
    info = sync_quick_trades(wrap, fake, now_ms=now_ms, send=lambda *_a, **_k: True)
    assert info.get("tagged") == 1
    assert (
        conn.execute("SELECT notice_sent_at_ms FROM assist_quick_registration LIMIT 1").fetchone()[0]
        == now_ms
    )
    conn.close()


def test_sync_no_close_notice_if_already_closed(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "precls.db")
    t0 = T0
    closed = t0 - 10_000
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            status, created_at_ms, expires_at_ms, updated_at_ms, trade_id
        ) VALUES ('r1', 'BTCUSDT', 'long', 100, 90, 1, 'tagged', ?, ?, ?, 't1')
        """,
        (t0 - 20_000, t0, t0),
    )
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, entry_avg, entry_qty,
            entry_time_ms, closed_at_ms, exit_avg, net_usdt, net_r, risk_usdt,
            close_notice_at_ms, updated_at_ms, planned_stop
        ) VALUES ('t1', 'r1', 'BTCUSDT', 'long', 'closed', 100, 1, ?, ?, 110, 5, 0.5, 10, NULL, ?, 90)
        """,
        (t0 - 15_000, closed, t0),
    )
    conn.commit()
    sent: list[str] = []
    sync_quick_trades(conn, FakeFutures(trades=[]), now_ms=t0, send=lambda t, **_k: sent.append(t) or True)
    assert sent == []
    conn.close()


def test_sync_close_orders_boom_line(tmp_path: Path) -> None:
    conn, _, sent, _ = _sync_buy_sell(
        tmp_path / "oboom",
        with_close=True,
    )
    # second pass with boom orders client on open rebuild — insert closed with notice not sent
    trade = conn.execute("SELECT * FROM assist_quick_trade LIMIT 1").fetchone()
    conn.execute("UPDATE assist_quick_trade SET close_notice_at_ms=NULL WHERE id=?", (trade["id"],))
    conn.commit()
    buy_t = int(trade["entry_time_ms"])
    sell_t = int(trade["closed_at_ms"] or buy_t + 1000)
    fake = _OrdersFake(
        trades=[
            _fill(tid=1, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t),
            _fill(tid=2, symbol="BTCUSDT", side="SELL", price=110, qty=1, time_ms=sell_t, realized=10),
        ],
        boom_orders=True,
    )
    sent.clear()
    conn.execute(
        "UPDATE assist_quick_trade SET status='open', close_notice_at_ms=NULL WHERE id=?",
        (trade["id"],),
    )
    conn.commit()
    sync_quick_trades(conn, fake, now_ms=sell_t + 10, send=lambda t, **_k: sent.append(t) or True)
    assert any("อ่านคำสั่งค้างไม่ได้ — ตรวจในแอป" in m for m in sent)
    conn.close()


def test_sync_close_leftovers_one_per_line(tmp_path: Path) -> None:
    conn, _, sent, _ = _sync_buy_sell(tmp_path / "lo", with_close=True)
    trade = conn.execute("SELECT id, symbol FROM assist_quick_trade LIMIT 1").fetchone()
    conn.execute(
        "UPDATE assist_quick_trade SET close_notice_at_ms=NULL, status='open' WHERE id=?", (trade["id"],)
    )
    conn.commit()
    bot = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "clientOrderId": "jv-sl-1",
        "stopPrice": "95",
        "origQty": "1",
    }
    owner = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "clientOrderId": "manual-1",
        "stopPrice": "94",
        "origQty": "1",
    }
    buy_t = int(
        conn.execute("SELECT entry_time_ms FROM assist_quick_trade WHERE id=?", (trade["id"],)).fetchone()[0]
    )
    sell_t = buy_t + 60_000
    fake = _OrdersFake(
        trades=[
            _fill(tid=1, symbol="BTCUSDT", side="BUY", price=100, qty=1, time_ms=buy_t),
            _fill(tid=2, symbol="BTCUSDT", side="SELL", price=110, qty=1, time_ms=sell_t, realized=10),
        ],
        orders=[bot, owner],
    )
    sent.clear()
    sync_quick_trades(conn, fake, now_ms=sell_t + 10, send=lambda t, **_k: sent.append(t) or True)
    close_msg = [m for m in sent if "ปิดแล้ว" in m][-1]
    assert QUICK_LIST_CLOSING_NOTE in close_msg
    assert BOT_CLOSING_ORDER_NOTE in close_msg
    assert close_msg.strip().split("\n")[-1] == quick_mode_tag()
    conn.close()


def test_sync_tagged_same_run_one_user_trades_call(tmp_path: Path) -> None:
    conn, fake, _sent, _ = _sync_buy_sell(tmp_path / "utc")
    assert len(fake.user_trade_calls) == 1
    conn.close()


# --- H: spec completion ------------------------------------------------------


def test_stepper_btc_stop_markup_and_stop_far(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    conn = open_db(tmp_path / "h-e.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"BTCUSDT": Decimal("100000")})
    _run_qk(conn, "qk:sd:l", fake)
    edits: list[dict[str, Any]] = []
    sent, send = _send_capture()

    def edit_fn(_mid: int, text: str, **kw: Any) -> bool:
        edits.append({"text": text, **kw})
        return True

    process_update(conn, _msg("BTC"), send=send, edit=edit_fn, futures=fake, now_ms_value=T1)
    cid = conn.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
    assert edits[-1]["reply_markup"] == _stop_step_markup(cid)
    assert STOP_STEP_HINT in edits[-1]["text"]
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent2, send2 = _send_capture()
    out2 = process_update(conn, _msg("0.5"), send=send2, edit=edit_fn, futures=fake, now_ms_value=T2)
    assert out2.get("handled")
    assert sent2 and MSG_STOP_FAR in sent2[0]["text"]
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "pick_stop"
    )
    conn.close()


def test_menu_label_cancels_draft(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "menu.db")
    _seed_settings(conn)
    _run_qk(conn, "qk:sd:l", _happy_fake())
    label = match_menu_label(BTN_QUICK)
    assert label
    process_update(
        conn,
        _msg(label),
        send=lambda *_a, **_k: 1,
        edit=lambda *_a, **_k: True,
        futures=_happy_fake(),
        now_ms_value=T1,
    )
    assert (
        conn.execute("SELECT COUNT(*) c FROM assist_quick_card WHERE status='pick_symbol'").fetchone()["c"]
        == 0
    )
    conn.close()


def test_awaiting_flags_leave_draft(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    for key, kwargs in (("explore", {"awaiting_explore": True}), ("risk", {"awaiting_risk": "0.5"})):
        conn = open_db(tmp_path / f"await-{key}.db")
        _seed_settings(conn)
        write_settings(conn, **kwargs)
        _run_qk(conn, "qk:sd:l", _happy_fake())
        before = conn.execute("SELECT COUNT(*) c FROM assist_quick_card").fetchone()["c"]
        process_update(
            conn,
            _msg("BTC"),
            send=lambda *_a, **_k: 1,
            edit=lambda *_a, **_k: True,
            futures=_happy_fake(),
            now_ms_value=T1,
        )
        assert conn.execute("SELECT COUNT(*) c FROM assist_quick_card").fetchone()["c"] == before
        conn.close()


def test_typed_q_pct_markup(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "tq.db")
    _seed_settings(conn)
    sent, send = _send_capture()
    out = process_update(
        conn,
        _msg("q BTC long 0.4%"),
        send=send,
        edit=lambda *_a, **_k: True,
        futures=_happy_fake(marks={"BTCUSDT": Decimal("100000")}),
        now_ms_value=T0,
    )
    assert out.get("handled")
    row = conn.execute(
        "SELECT id, status, stop_kind, stop_value FROM assist_quick_card ORDER BY created_at_ms DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "quoted"
    assert row["stop_kind"] == "pct"
    assert row["stop_value"] == "0.4"
    assert sent[-1].get("reply_markup") == _quoted_markup(row["id"])
    conn.close()


def test_qr_refusal_texts(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    from jarvise_assist.futures import FuturesPosition

    conn = open_db(tmp_path / "qr.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"BTCUSDT": Decimal("100000")})
    card = build_quick_card(conn, fake, symbol="BTCUSDT", side="long", stop=Decimal("99000"), now_ms=T0)
    assert card.get("ok")
    cb = card["callback_data"]
    assert cb
    create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=1, planned_stop=0.9, qty=1, now_ms=T0
    )
    sent: list[str] = []
    handle_quick_register_callback(
        conn, str(cb), futures=fake, send=lambda t, **_k: sent.append(t), now_ms=T1
    )
    assert REFUSAL["quick_open"] in sent[0]
    conn2 = open_db(tmp_path / "qr2.db")
    _seed_settings(conn2)
    card2 = build_quick_card(conn2, fake, symbol="BTCUSDT", side="long", stop=Decimal("99000"), now_ms=T0)
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.01"),
        entry_price=Decimal("100000"),
        mark_price=Decimal("100000"),
        update_time_ms=T1,
    )
    sent2: list[str] = []
    handle_quick_register_callback(
        conn2,
        str(card2["callback_data"]),
        futures=_happy_fake(marks={"BTCUSDT": Decimal("100000")}, positions=[pos]),
        send=lambda t, **_k: sent2.append(t),
        now_ms=T1,
    )
    assert REFUSAL["main_position"] in sent2[0]
    conn.close()
    conn2.close()


def _risk_confirm_row(conn: Any, *, now_ms: int) -> list[dict[str, str]]:
    captured: list[dict[str, Any]] = []

    def capture_send(_t: str, **kw: Any) -> bool:
        captured.append(kw)
        return True

    out = quick_mod.handle_quick_callback(conn, "qs:risk:1", send=capture_send, now_ms=now_ms)
    assert out.get("confirm")
    return captured[-1]["reply_markup"]["inline_keyboard"][0]


def test_menu_line2_mode_tag_and_qs_labels(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for mode in ("off", "testnet", "live"):
        monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", mode)
        conn = open_db(tmp_path / f"menu-{mode}.db")
        _seed_settings(conn)
        text, _kb = render_quick_menu(conn, now_ms=T0)
        assert text.split("\n")[1] == quick_mode_tag()
        row = _risk_confirm_row(conn, now_ms=T0)
        assert row[0]["text"] == "✅ ยืนยัน"
        assert row[0]["callback_data"] == "qs:ok:risk:1"
        assert row[1]["text"] == "↩️ ไม่เปลี่ยน"
        assert row[1]["callback_data"] == "qs:no"
        conn.close()


def test_registration_success_countdown_line() -> None:
    msg = registration_success_message(
        sym="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        qty=Decimal("0.01"),
        tick=Decimal("0.1"),
        now_ms=T0,
    )
    lines = msg.split("\n")
    assert quick_mode_tag() == lines[1]
    assert lines[3].startswith("⏳ เหลือ 10 นาที")
    assert _hhmm(T0 + REG_TTL_MS) in lines[3]


def test_format_owner_order_line_matches_closing_helper() -> None:
    order = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "stopPrice": "99",
        "origQty": "1",
    }
    tick = Decimal("0.1")
    from jarvise_assist.execute import OWNER_CLOSING_ORDER_NOTE

    assert _format_owner_order_line(order, tick=tick) == format_closing_order_line(
        order, tick=tick, note=OWNER_CLOSING_ORDER_NOTE
    )


def _hhmm(ms: int) -> str:
    from jarvise_assist.quick_m1a1 import _hhmm as hhmm

    return hhmm(ms)
