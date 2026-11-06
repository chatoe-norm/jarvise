"""Assist R journal: original 1R, one card, grades, weekly block."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures

from jarvise_assist.journal import (
    format_journal_card,
    format_journal_lines,
    journal_on_close,
    locked_r,
    net_r_of,
    stops_only_tightened,
    summarize_journal,
)
from jarvise_assist.listen import process_update
from jarvise_assist.store import insert_exec_intent, insert_watch_advice, upsert_watch_position
from jarvise_assist.watch import run_assist_watch
from jarvise_assist.weekly import format_weekly_summary
from jarvise_ingest.db import SCHEMA_VERSION, open_db, upsert_analysis_output

ENTRY = 1_700_000_000_000
EXIT = ENTRY + 3_600_000
NOW = EXIT + 60_000


def test_schema_is_migration_22(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "j.db")
    assert SCHEMA_VERSION == 22
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assist_journal'"
    ).fetchone()
    assert row is not None
    conn.close()


def test_net_r_stays_on_original_loss_at_stop() -> None:
    loss = 0.43
    qty = 1.0
    entry = 1.0
    net_usdt = 0.86
    stored = net_usdt / loss
    trade = {"net_r_real": stored, "loss_at_stop": loss}
    assert net_r_of(trade) == pytest.approx(stored)
    new_risk = abs(1.1 - entry) * qty
    assert stored != pytest.approx(net_usdt / new_risk)
    assert locked_r(side="long", entry=entry, stop=0.9, qty=qty, loss_at_stop=loss) != pytest.approx(
        locked_r(side="long", entry=entry, stop=1.1, qty=qty, loss_at_stop=loss)
    )


def test_locked_r_long_and_short() -> None:
    assert locked_r(side="long", entry=100, stop=110, qty=2, loss_at_stop=10) == pytest.approx(2.0)
    assert locked_r(side="short", entry=100, stop=90, qty=2, loss_at_stop=10) == pytest.approx(2.0)
    assert locked_r(side="short", entry=100, stop=110, qty=2, loss_at_stop=10) == pytest.approx(-2.0)
    assert locked_r(side="long", entry=100, stop=110, qty=2, loss_at_stop=None) is None
    assert stops_only_tightened("long", [1.0, 1.1]) is True
    assert stops_only_tightened("long", [1.0, 0.9]) is False
    assert stops_only_tightened("short", [1.0, 0.9]) is True
    assert stops_only_tightened("short", [1.0, 1.1]) is False


def _watch(
    conn: Any,
    *,
    closed_at_ms: int | None,
    entry_time_ms: int = ENTRY,
    symbol: str = "ADAUSDT",
    side: str = "short",
) -> None:
    upsert_watch_position(
        conn,
        {
            "symbol": symbol,
            "position_side": "BOTH",
            "side": side,
            "entry": 1.0,
            "qty": 2.0,
            "in_universe": 1,
            "signal_id": "sig1",
            "initial_stop": 2.0,
            "initial_stop_source": "signal",
            "entry_time_ms": entry_time_ms,
            "no_stop_latched": 0,
            "last_seen_ms": entry_time_ms,
            "closed_at_ms": closed_at_ms,
        },
    )


def _trade(
    conn: Any,
    *,
    entry_order_id: str,
    entry_time_ms: int = ENTRY,
    exit_time_ms: int | None = EXIT,
    loss_at_stop: float | None = 1.0,
    net_r_real: float | None = 1.0,
    realized: float | None = 1.0,
    status: str = "closed",
    exit_kind: str = "stop",
    signal_id: str | None = "sig1",
    intent_id: str | None = None,
    symbol: str = "ADAUSDT",
    side: str = "short",
    planned_stop: float | None = 2.0,
    entry_fill: float = 1.0,
    exit_fill: float | None = 0.5,
) -> None:
    conn.execute(
        """
        INSERT INTO assist_real_trade (
            symbol, side, entry_order_id, status, updated_at_ms, intent_id, signal_id,
            entry_time_ms, entry_qty, entry_fill_avg, planned_stop, loss_at_stop,
            exit_time_ms, exit_fill_avg, exit_kind, realized_pnl_usdt, commission_usdt,
            funding_usdt, net_r_real
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            side,
            entry_order_id,
            status,
            NOW,
            intent_id,
            signal_id,
            entry_time_ms,
            2.0,
            entry_fill,
            planned_stop,
            loss_at_stop,
            exit_time_ms,
            exit_fill,
            exit_kind,
            realized,
            0.0,
            0.0,
            net_r_real,
        ),
    )
    conn.commit()


def _seed_card(conn: Any) -> None:
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "an1",
            "timestamp": ENTRY,
            "symbol": "ADAUSDT",
            "timeframe": "4h",
            "regime_state": "trend_down",
            "confidence_score": 0.72,
            "action": "sell",
            "invalidation_price": 2.0,
            "size_pct_equity": 1.0,
            "thesis": "EMA stack short",
        },
    )
    entry_intent = insert_exec_intent(
        conn,
        kind="entry",
        symbol="ADAUSDT",
        side="short",
        preview={"analysis_id": "an1", "loss_at_stop": "1", "stop": "2"},
        created_at_ms=ENTRY,
        expires_at_ms=ENTRY + 600_000,
    )
    move_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="ADAUSDT",
        side="short",
        preview={"stop": "0.5"},
        created_at_ms=ENTRY + 1_000,
        expires_at_ms=ENTRY + 600_000,
    )
    conn.execute("UPDATE assist_exec_intent SET status='done' WHERE id=?", (move_id,))
    conn.commit()
    _watch(conn, closed_at_ms=EXIT)
    insert_watch_advice(
        conn,
        symbol="ADAUSDT",
        position_side="BOTH",
        kind="exit",
        reason="flat",
        level=None,
        approx_r=1.0,
        status="sent",
        created_at_ms=ENTRY + 2_000,
    )
    _trade(conn, entry_order_id="42", intent_id=entry_intent)


SAMPLE_CARD = """\
📘 บันทึกไม้
🔻 ADAUSDT · SHORT
เข้า 1 → ออก 0.5
1R = 1.00 USDT
ผล +1.00R
กำไร +1.00 USDT
ปิด: stop · คำแนะนำล่าสุด: exit (flat)
เลื่อน stop:
· 2 ล็อก -2.00R
· 0.5 ล็อก +1.00R
เหตุเข้า: EMA stack short · ความมั่นใจ 0.72
ตรวจอัตโนมัติ:
· เปิดไม่เกิน 2 ไม้: ผ่าน
· สัญญาณผ่านเกณฑ์: ผ่าน
· มี stop: ผ่าน
· เลื่อน stop เฉพาะทางแน่น: ผ่าน
· ปิดตามแผน: ผ่าน
· ไม่เข้าตอนพักแพ้ติด: ผ่าน"""


def test_card_sent_once_and_retries_until_message_id(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "card.db")
    _seed_card(conn)
    sent: list[dict[str, Any]] = []

    def send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int | None:
        sent.append({"text": text, "reply_markup": reply_markup})
        if len(sent) == 1:
            return None
        return 9

    def sync(*_a: Any, **_k: Any) -> None:
        return None

    first = journal_on_close(conn, [], futures=None, now_ms_value=NOW, send=send, sync_fn=sync)
    assert first["inserted"] == ["42"]
    assert first["sent"] == []
    assert sent[0]["text"] == SAMPLE_CARD
    buttons = sent[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "jp:42"
    assert buttons[1]["callback_data"] == "jf:42"
    assert len(buttons[1]["callback_data"]) <= 64
    row = conn.execute("SELECT card_message_id FROM assist_journal WHERE entry_order_id='42'").fetchone()
    assert row["card_message_id"] is None

    second = journal_on_close(conn, [], futures=None, now_ms_value=NOW, send=send, sync_fn=sync)
    assert second["sent"] == ["42"]
    assert second["synced"] is False
    row = conn.execute("SELECT card_message_id FROM assist_journal WHERE entry_order_id='42'").fetchone()
    assert row["card_message_id"] == 9

    journal_on_close(conn, [], futures=None, now_ms_value=NOW, send=send, sync_fn=sync)
    assert len(sent) == 2
    conn.close()


def test_rendered_card_matches_fixture(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "render.db")
    _seed_card(conn)
    trade = dict(conn.execute("SELECT * FROM assist_real_trade WHERE entry_order_id='42'").fetchone())
    text = format_journal_card(
        conn,
        trade,
        {
            "open_positions_ok": True,
            "signal_present": True,
            "stop_existed": True,
            "moves_tightened": True,
            "exit_followed_plan": True,
            "not_in_streak_pause": True,
        },
    )
    assert text == SAMPLE_CARD
    conn.close()


def test_sync_or_send_failure_does_not_break_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "watch.db")
    _watch(conn, closed_at_ms=None)
    _trade(conn, entry_order_id="42")

    def bad_sync(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("binance")

    monkeypatch.setattr("jarvise_assist.journal.sync_real_trades", bad_sync)
    failed_sync = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=NOW, send=lambda _t: True)
    assert failed_sync["ok"] is True
    assert failed_sync["closed"] == [{"symbol": "ADAUSDT", "position_side": "BOTH"}]
    missing = conn.execute("SELECT 1 FROM assist_journal").fetchone()
    assert missing is None

    _watch(conn, closed_at_ms=EXIT)

    def noop_sync(*_a: Any, **_k: Any) -> None:
        return None

    def bad_send(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("telegram")

    monkeypatch.setattr("jarvise_assist.journal.sync_real_trades", noop_sync)
    monkeypatch.setattr("jarvise_assist.journal.send_message", bad_send)
    failed_send = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=NOW, send=lambda _t: True)
    assert failed_send["ok"] is True
    row = conn.execute("SELECT card_message_id FROM assist_journal WHERE entry_order_id='42'").fetchone()
    assert row is not None and row["card_message_id"] is None
    conn.close()


def test_null_loss_is_excluded_from_r_and_weekly_numbers(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "week.db")
    rows = [
        ("p", 100, 1.0, 2.0, "pass", None),
        ("f", 200, 1.0, -1.0, "fail", "early_exit"),
        ("u", 300, None, 99.0, None, None),
        ("g", 400, 1.0, 0.5, None, None),
    ]
    for oid, exit_ms, loss, net, grade, tags in rows:
        _trade(
            conn,
            entry_order_id=oid,
            entry_time_ms=exit_ms - 10,
            exit_time_ms=exit_ms,
            loss_at_stop=loss,
            net_r_real=net,
            realized=net,
        )
        conn.execute(
            """
            INSERT INTO assist_journal (
                entry_order_id, auto_checks_json, grade, mistake_tags, note,
                card_message_id, created_at_ms, graded_at_ms
            ) VALUES (?, '{}', ?, ?, NULL, 1, ?, NULL)
            """,
            (oid, grade, tags, exit_ms),
        )
    conn.commit()
    summary = summarize_journal(conn, since_ms=0, until_ms=1000)
    assert summary["n"] == 4
    assert summary["known_n"] == 3
    assert summary["sum_r"] == pytest.approx(1.5)
    assert summary["avg_r"] == pytest.approx(0.5)
    assert summary["pass_rate"] == pytest.approx(0.5)
    assert summary["avg_r_pass"] == pytest.approx(2.0)
    assert summary["avg_r_fail"] == pytest.approx(-1.0)
    assert summary["top_tag"] == "early_exit"
    assert summary["ungraded"] == 2
    lines = format_journal_lines(summary)
    text = format_weekly_summary(
        counts={"sent": 0, "skipped": 0, "taken": 0},
        virtual={"n": 0},
        real={"universe_usdt": 0.0},
        closed_all=0,
        journal=summary,
    )
    assert lines[0] == "บันทึกไม้: n=4 · รวม +1.50R · เฉลี่ย +0.50R"
    assert "ตามแผน 50% · ตามแผนเฉลี่ย +2.00R · ผิดแผนเฉลี่ย -1.00R" in text
    assert "ผิดบ่อยสุด: early_exit · ยังไม่ให้เกรด 2" in text
    assert "99" not in lines[0]
    conn.close()


def test_callback_grade_tag_and_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    conn = open_db(tmp_path / "cb.db")
    _trade(conn, entry_order_id="42")
    conn.execute(
        """
        INSERT INTO assist_journal (
            entry_order_id, auto_checks_json, grade, mistake_tags, note,
            card_message_id, created_at_ms, graded_at_ms
        ) VALUES ('42', '{}', NULL, NULL, NULL, 5, ?, NULL)
        """,
        (NOW,),
    )
    conn.commit()
    edits: list[dict[str, Any]] = []
    sent: list[str] = []

    def edit(mid: int, text: str, *, reply_markup: dict[str, Any] | None = None) -> bool:
        edits.append({"mid": mid, "text": text, "reply_markup": reply_markup})
        return True

    def send(text: str, **_k: Any) -> int:
        sent.append(text)
        return 1

    def tap(data: str) -> dict[str, Any]:
        return process_update(
            conn,
            {
                "callback_query": {
                    "id": "cq",
                    "data": data,
                    "message": {
                        "message_id": 5,
                        "chat": {"id": 111},
                        "text": "📘 บันทึกไม้",
                    },
                }
            },
            send=send,
            answer=lambda *_a, **_k: None,
            edit=edit,
            now_ms_value=NOW,
        )

    fail = tap("jf:42")
    assert fail["grade"] == "fail"
    tap("jt:42:early_exit")
    tap("jt:42:chase")
    tap("jd:42")
    row = conn.execute("SELECT grade, mistake_tags FROM assist_journal WHERE entry_order_id='42'").fetchone()
    assert row["grade"] == "fail"
    assert row["mistake_tags"] == "chase,early_exit"
    tag_board = edits[-2]["reply_markup"]["inline_keyboard"]
    labels = [btn[0]["text"] for btn in tag_board]
    assert "✅ ไล่ราคา" in labels
    assert "✅ ออกก่อน" in labels
    assert tag_board[-1][0]["callback_data"] == "jd:42"

    noted = process_update(
        conn,
        {
            "message": {
                "chat": {"id": 111},
                "text": "ออกเร็วไป",
                "reply_to_message": {"message_id": 5},
            }
        },
        send=send,
        now_ms_value=NOW,
    )
    assert noted["journal_note"] is True
    assert sent == ["บันทึกโน้ตแล้ว"]
    note = conn.execute("SELECT note, grade FROM assist_journal WHERE entry_order_id='42'").fetchone()
    assert note["note"] == "ออกเร็วไป"
    assert note["grade"] == "fail"

    passed = tap("jp:42")
    assert passed["grade"] == "pass"
    cleared = conn.execute(
        "SELECT grade, mistake_tags, note FROM assist_journal WHERE entry_order_id='42'"
    ).fetchone()
    assert cleared["grade"] == "pass"
    assert cleared["mistake_tags"] is None
    assert cleared["note"] == "ออกเร็วไป"
    conn.close()
