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
    MSG_ALREADY_REGISTERED,
    MSG_BAD_BUTTON,
    MSG_REGISTER_FAILED,
    MSG_STOP_FAR,
    STOP_STEP_HINT,
    _quoted_markup,
    _refresh_only_markup,
    _side_label,
    _stop_step_markup,
    build_card_for_row,
    handle_quick_draft_text,
    registration_success_message,
)
from jarvise_assist.store import write_settings
from jarvise_ingest.db import open_db

T0 = NOW
T1 = T0 + 1000
T2 = T0 + 2000
T3 = T0 + 3000


def _expected_tag_notice() -> str:
    return (
        "✅ จับไม้ quick ได้แล้ว — BTCUSDT LONG เข้า 100 · qty 1 · stop ที่ลงทะเบียน 90\n"
        "ความเสี่ยงจริง ≈ 10 USDT · บอทไม่ได้วาง stop — ตั้ง stop ในแอปเอง\n"
        f"{quick_mode_tag()}"
    )


def _expected_close_with_net_r() -> str:
    return (
        "🏁 ไม้ quick BTCUSDT LONG ปิดแล้ว (ปิดใน Binance) — สุทธิ +9.98 USDT · "
        "+1.00R (รวมค่าธรรมเนียมจริง + funding)\n"
        f"{quick_mode_tag()}"
    )


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


# --- C: S1 / M16 persist guard -----------------------------------------------


def test_rf_after_registration_answers_already(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S1: rf after real qk:rg must not reach _persist_quote; answers already registered."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "m16a.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    _run_qk(conn, f"qk:rg:{cid}", fake)
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "registered"
    )
    _, answers, _, _ = _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=T1)
    assert answers == [MSG_ALREADY_REGISTERED]
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "registered"
    )
    conn.close()


def test_rf_concurrent_register_not_downgraded(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S1: concurrent register during rf must not downgrade via _persist_quote."""
    import jarvise_assist.quick_m1a1 as m1

    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "m16b.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    real = m1.build_card_for_row

    def wrap(conn_a: Any, futures_a: Any, row: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        card = real(conn_a, futures_a, row, now_ms=now_ms)
        conn_a.execute("UPDATE assist_quick_card SET status='registered' WHERE id=?", (row["id"],))
        conn_a.commit()
        return card

    monkeypatch.setattr(m1, "build_card_for_row", wrap)
    _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=T1)
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


def test_qk_rg_txn_failure_leaves_quoted(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise inside qk:rg transaction → quoted card, no registration, one failure answer."""
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
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert answers == [MSG_REGISTER_FAILED]
    assert out == {"ok": False, "reason": "read_failed"}
    assert conn.execute("SELECT COUNT(*) c FROM assist_quick_registration").fetchone()["c"] == 0
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert row["registration_id"] is None


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
    """S5–S8 tag line exact copy (kills tag line-2 prefix mutants)."""
    conn, _fake, sent, _rid = _sync_buy_sell(tmp_path, with_close=False)
    assert sent == [_expected_tag_notice()]
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
    assert sent == [_expected_tag_notice(), _expected_close_with_net_r()]
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
    close = [m for m in msgs if "ปิดแล้ว" in m][0]
    net = float(conn2.execute("SELECT net_usdt FROM assist_quick_trade LIMIT 1").fetchone()[0])
    other = conn2.execute("SELECT commission_other_asset FROM assist_quick_trade LIMIT 1").fetchone()[0]
    assert close == (
        f"🏁 ไม้ quick BTCUSDT LONG ปิดแล้ว (ปิดใน Binance) — สุทธิ {net:+.2f} USDT · "
        f"R คำนวณไม่ได้ (ค่าธรรมเนียมเป็น {other})\n{quick_mode_tag()}"
    )
    conn2.close()


def test_sync_close_risk_zero_notice(tmp_path: Path) -> None:
    """S7: risk_usdt==0 → plain R line through sync."""
    conn = open_db(tmp_path / "r0sync.db")
    t0 = T0
    create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1.0, now_ms=t0
    )
    buy_t, sell_t = t0 + 60_000, t0 + 120_000
    fake = FakeFutures(
        trades=[
            _fill(tid=1, symbol="BTCUSDT", side="BUY", price=90, qty=1, time_ms=buy_t),
            _fill(tid=2, symbol="BTCUSDT", side="SELL", price=91, qty=1, time_ms=sell_t, realized=1),
        ]
    )
    sent: list[str] = []
    sync_quick_trades(conn, fake, now_ms=sell_t + 1000, send=lambda t, **_k: sent.append(t) or True)
    net = float(conn.execute("SELECT net_usdt FROM assist_quick_trade LIMIT 1").fetchone()[0])
    assert conn.execute("SELECT risk_usdt FROM assist_quick_trade LIMIT 1").fetchone()[0] == 0
    close = sent[-1]
    assert close == (
        f"🏁 ไม้ quick BTCUSDT LONG ปิดแล้ว (ปิดใน Binance) — สุทธิ {net:+.2f} USDT · "
        f"R คำนวณไม่ได้\n{quick_mode_tag()}"
    )
    conn.close()


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
    close_msg = [m for m in sent if "ปิดแล้ว" in m][-1]
    lines = close_msg.split("\n")
    assert "อ่านคำสั่งค้างไม่ได้ — ตรวจในแอป" in lines
    assert lines[-1] == quick_mode_tag()
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
    net = float(
        conn.execute("SELECT net_usdt FROM assist_quick_trade WHERE id=?", (trade["id"],)).fetchone()[0]
    )
    assert close_msg.split("\n") == [
        f"🏁 ไม้ quick BTCUSDT LONG ปิดแล้ว (ปิดใน Binance) — สุทธิ {net:+.2f} USDT · "
        "+1.00R (รวมค่าธรรมเนียมจริง + funding)",
        "คำสั่งที่ยังค้างบน BTCUSDT:",
        "• STOP_MARKET SELL 95 · 1 · 🤖 ของบอท",
        "• STOP_MARKET SELL 94 · 1 · ✋ คุณตั้งเอง",
        "✋ ยังค้างอยู่ — ลบในแอป",
        quick_mode_tag(),
    ]
    conn.close()


def test_sync_tagged_same_run_one_user_trades_call(tmp_path: Path) -> None:
    """S2: tagged trade must not fall through to second user_trades fetch."""
    conn, fake, _sent, _ = _sync_buy_sell(tmp_path / "utc", with_close=False)
    assert len(fake.user_trade_calls) == 1
    conn.close()


def test_sync_tag_and_close_one_run_one_user_trades_call(tmp_path: Path) -> None:
    """Companion S2: tag + close in one sync still fetches user_trades once."""
    conn, fake, _sent, _ = _sync_buy_sell(tmp_path / "utc2", with_close=True)
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
    want = "\n".join([f"⚡ Quick · BTCUSDT · {_side_label('long')} — {STOP_STEP_HINT}", quick_mode_tag()])
    assert edits[-1]["text"] == want
    assert edits[-1]["reply_markup"] == _stop_step_markup(cid)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent2, send2 = _send_capture()
    out2 = process_update(conn, _msg("0.5"), send=send2, edit=edit_fn, futures=fake, now_ms_value=T2)
    assert out2.get("handled")
    assert [m["text"] for m in sent2] == [MSG_STOP_FAR]
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
    """S3/S4: explore and awaiting_risk must not route symbol text into draft handler."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def stub_lookup(*_a: Any, **_k: Any) -> None:
        return None

    def stub_fetch(*_a: Any, **_k: Any) -> list[Any]:
        return []

    for key, kwargs in (("explore", {"awaiting_explore": True}), ("risk", {"awaiting_risk": "0.5"})):
        conn = open_db(tmp_path / f"await-{key}.db")
        _seed_settings(conn)
        _run_qk(conn, "qk:sd:l", _happy_fake())
        write_settings(conn, **kwargs)
        before = _rows(conn)
        process_update(
            conn,
            _msg("BTC"),
            send=lambda *_a, **_k: 1,
            edit=lambda *_a, **_k: True,
            futures=_happy_fake(),
            now_ms_value=T1,
            explore_lookup=stub_lookup,
            explore_fetch=stub_fetch,
        )
        assert _rows(conn) == before
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
    """S14."""
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
    assert lines[1] == quick_mode_tag()
    assert lines[3] == (f"⏳ เหลือ 10 นาที (ถึง {_hhmm(T0 + REG_TTL_MS)} น.) · ดูสถานะใน ⚡ Quick")


def test_format_owner_order_line_literals() -> None:
    tick = Decimal("0.1")
    stop_cp = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "closePosition": True,
        "stopPrice": "95000",
    }
    assert _format_owner_order_line(stop_cp, tick=tick) == (
        "• STOP_MARKET SELL 95000.0 · closePosition · ✋ คุณตั้งเอง — จะยังค้างอยู่ ลบในแอป"
    )
    limit = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "LIMIT",
        "reduceOnly": True,
        "price": "110000",
        "origQty": "0.01",
    }
    assert _format_owner_order_line(limit, tick=tick) == (
        "• LIMIT SELL 110000.0 · 0.01 · ✋ คุณตั้งเอง — จะยังค้างอยู่ ลบในแอป"
    )
    stop_qty = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "stopPrice": "99",
        "origQty": "1",
    }
    assert _format_owner_order_line(stop_qty, tick=tick) == (
        "• STOP_MARKET SELL 99.0 · 1 · ✋ คุณตั้งเอง — จะยังค้างอยู่ ลบในแอป"
    )


def test_menu_open_registration_countdown_line(tmp_path: Path) -> None:
    """S15."""
    conn = open_db(tmp_path / "menu-ct.db")
    create_quick_registration(
        conn, symbol="BTCUSDT", side="long", planned_entry=100, planned_stop=90, qty=1, now_ms=T0
    )
    conn.execute(
        "UPDATE assist_quick_registration SET expires_at_ms=? WHERE status='open'",
        (T0 + 600_000,),
    )
    conn.commit()
    text, _ = render_quick_menu(conn, now_ms=T0 + 60_000)
    want = f"⏳ ลงทะเบียน BTCUSDT LONG — เหลือ 9 นาที (ถึง {quick_mod._menu_hhmm(T0 + 600_000)} น.)"
    assert want in text.split("\n")
    conn.close()


def test_qs_cap_confirm_button_callbacks(tmp_path: Path) -> None:
    """S16 companion: cap confirm row callbacks."""
    conn = open_db(tmp_path / "qscap.db")
    _seed_settings(conn)
    captured: list[dict[str, Any]] = []

    def capture_send(_t: str, **kw: Any) -> bool:
        captured.append(kw)
        return True

    out = quick_mod.handle_quick_callback(conn, "qs:cap:1.5", send=capture_send, now_ms=T0)
    assert out.get("confirm")
    row = captured[-1]["reply_markup"]["inline_keyboard"][0]
    assert row[0] == {"text": "✅ ยืนยัน", "callback_data": "qs:ok:cap:1.5"}
    assert row[1] == {"text": "↩️ ไม่เปลี่ยน", "callback_data": "qs:no"}
    conn.close()


def test_quoted_card_leftovers_and_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S17/S18."""
    from jarvise_assist.quick_card import CARD_TTL_PREFIX, _hhmmss

    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "card-lo.db")
    _seed_settings(conn)
    bot = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "clientOrderId": "jv-x",
        "stopPrice": "95000",
        "origQty": "0.01",
    }
    owner = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "clientOrderId": "man-x",
        "stopPrice": "94000",
        "origQty": "0.01",
    }
    fake = _happy_fake(
        marks={"BTCUSDT": Decimal("100000")},
        orders=[bot, owner],
        ticks={"BTCUSDT": Decimal("0.1")},
    )
    card = build_quick_card(conn, fake, symbol="BTCUSDT", side="long", stop_pct=Decimal("0.5"), now_ms=T0)
    assert card.get("ok")
    lines = card["text"].split("\n")
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("⚠ มีคำสั่งป้องกันค้าง"))
    assert lines[idx] == "⚠ มีคำสั่งป้องกันค้างบนสัญลักษณ์นี้ 2 อัน — ตรวจก่อนเข้า:"
    assert lines[idx + 1] == format_closing_order_line(bot, tick=Decimal("0.1"))
    assert lines[idx + 2] == format_closing_order_line(owner, tick=Decimal("0.1"))
    ttl = (
        f"{CARD_TTL_PREFIX} {_hhmmss(T0 + 120_000)} (หมดแล้วกด 🔄 อัปเดตราคา) · "
        "บอทไม่ส่งคำสั่ง — กดลงทะเบียน แล้วเปิดเองใน Binance ภายใน 10 นาที"
    )
    assert ttl in lines
    conn.close()


def test_rf_pct_card_refreshes_stop_and_time(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 6a / S23."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "rfpct.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"SUIUSDT": Decimal("2.0")})
    cid = _flow_to_quoted(conn, fake)
    row = dict(conn.execute("SELECT * FROM assist_quick_card WHERE id=?", (cid,)).fetchone())
    old_stop = float(row["quote_stop"])
    fake.marks["SUIUSDT"] = Decimal("2.1")
    now_ms = T0 + 60_000
    row_now = dict(conn.execute("SELECT * FROM assist_quick_card WHERE id=?", (cid,)).fetchone())
    expect = build_card_for_row(conn, fake, row_now, now_ms=now_ms)
    assert expect.get("ok")
    _, answers, out, _ = _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=now_ms)
    assert out == {"ok": True, "refreshed": True}
    assert answers == [None]
    after = conn.execute(
        "SELECT quote_stop, quoted_at_ms FROM assist_quick_card WHERE id=?", (cid,)
    ).fetchone()
    assert float(after["quote_stop"]) == float(expect["stop"])
    assert float(after["quote_stop"]) != old_stop
    assert int(after["quoted_at_ms"]) == now_ms
    conn.close()


def test_rf_price_card_refreshes_qty_only(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "rfpr.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"SUIUSDT": Decimal("2.0")})
    cid = _flow_to_quoted(conn, fake)
    row = conn.execute("SELECT quote_stop FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    old_stop = float(row["quote_stop"])
    conn.execute(
        "UPDATE assist_quick_card SET stop_kind='price', stop_value=? WHERE id=?",
        (str(old_stop), cid),
    )
    conn.commit()
    old_qty = float(conn.execute("SELECT quote_qty FROM assist_quick_card WHERE id=?", (cid,)).fetchone()[0])
    fake.marks["SUIUSDT"] = Decimal("2.05")
    _run_qk(conn, f"qk:rf:{cid}", fake, now_ms=T0 + 60_000)
    after = conn.execute("SELECT quote_stop, quote_qty FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert float(after["quote_stop"]) == old_stop
    assert float(after["quote_qty"]) != old_qty
    conn.close()


def test_rf_low_balance_refusal_refresh_markup(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S22."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "rflb.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake)
    poor = _happy_fake(balance=Decimal("1"), available=Decimal("1"))
    _, _, _, edits = _run_qk(conn, f"qk:rf:{cid}", poor, now_ms=T1)
    assert edits[-1]["reply_markup"] == _refresh_only_markup(cid)
    assert (
        conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()["status"]
        == "quoted"
    )
    conn.close()


def test_draft_pick_symbol_edit_fail_uses_send(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S12/S13 via draft path."""
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "edfail.db")
    _seed_settings(conn)
    _run_qk(conn, "qk:sd:l", _happy_fake())
    cid = conn.execute("SELECT id FROM assist_quick_card LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE assist_quick_card SET message_id=7 WHERE id=?", (cid,))
    conn.commit()
    sent, send = _send_capture()

    def edit_boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("edit boom")

    process_update(
        conn,
        _msg("BTC"),
        send=send,
        edit=edit_boom,
        futures=_happy_fake(),
        now_ms_value=T1,
    )
    assert len(sent) == 1
    assert (
        int(conn.execute("SELECT message_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()[0])
        == sent[0]["message_id"]
    )
    conn.close()


def _hhmm(ms: int) -> str:
    from jarvise_assist.quick_m1a1 import _hhmm as hhmm

    return hhmm(ms)
