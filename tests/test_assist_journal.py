"""Assist R journal: original 1R, one card, grades, weekly block."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures

from jarvise_assist.journal import (
    JOURNAL_SHIPPED_MS,
    collect_auto_checks,
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
BANGKOK = timezone(timedelta(hours=7))
# NOW is 06:14 Asia/Bangkok, inside the night hold. Sends use the first minute after it.
DAYTIME = int(datetime(2023, 11, 15, 7, 30, tzinfo=BANGKOK).timestamp() * 1000)
GAP_MS = 30 * 60 * 1000


def test_schema_is_migration_29(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "j.db")
    assert SCHEMA_VERSION == 29
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
    qty: float = 2.0,
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
            qty,
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
ผลเป็นเงิน +1.00 USDT
ปิด: stop · คำแนะนำล่าสุด: exit (flat)
เลื่อน stop:
· stop เริ่ม 2 ล็อก -2.00R
· 0.5 ล็อก +1.00R
เหตุเข้า: EMA stack short · ความมั่นใจ 0.72
ตรวจอัตโนมัติ:
· เปิดไม่เกิน 2 ไม้: ผ่าน
· เข้าจากสัญญาณบอท: ผ่าน
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

    first = journal_on_close(conn, [], futures=None, now_ms_value=DAYTIME, send=send, sync_fn=sync)
    assert first["inserted"] == ["42"]
    assert first["sent"] == []
    assert sent[0]["text"] == SAMPLE_CARD
    buttons = sent[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "jp:42"
    assert buttons[1]["callback_data"] == "jf:42"
    assert len(buttons[1]["callback_data"]) <= 64
    row = conn.execute("SELECT card_message_id FROM assist_journal WHERE entry_order_id='42'").fetchone()
    assert row["card_message_id"] is None

    second = journal_on_close(conn, [], futures=None, now_ms_value=DAYTIME, send=send, sync_fn=sync)
    assert second["sent"] == ["42"]
    assert second["synced"] is False
    row = conn.execute("SELECT card_message_id FROM assist_journal WHERE entry_order_id='42'").fetchone()
    assert row["card_message_id"] == 9

    journal_on_close(conn, [], futures=None, now_ms_value=DAYTIME, send=send, sync_fn=sync)
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
    failed_sync = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=DAYTIME, send=lambda _t: True)
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
    failed_send = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=DAYTIME, send=lambda _t: True)
    assert failed_send["ok"] is True
    row = conn.execute("SELECT card_message_id FROM assist_journal WHERE entry_order_id='42'").fetchone()
    assert row is not None and row["card_message_id"] is None
    conn.close()


def test_null_loss_is_excluded_from_r_and_weekly_numbers(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "week.db")
    base = JOURNAL_SHIPPED_MS + 3_600_000
    rows = [
        ("p", base + 100, 1.0, 2.0, "pass", None),
        ("f", base + 200, 1.0, -1.0, "fail", "early_exit"),
        ("u", base + 300, None, 99.0, None, None),
        ("g", base + 400, 1.0, 0.5, None, None),
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
    summary = summarize_journal(conn, since_ms=base, until_ms=base + 1_000)
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


def _bkk(year: int, month: int, day: int, hour: int, minute: int) -> int:
    dt = datetime(year, month, day, hour, minute, tzinfo=BANGKOK)
    return int(dt.timestamp() * 1000)


def _noop_sync(*_a: Any, **_k: Any) -> None:
    return None


DOGE_CARD = """\
📘 บันทึกไม้
🔻 DOGEUSDT · SHORT
เข้า 0.1 → ออก 0.09
1R = 1.00 USDT
ผล +1.00R
ผลเป็นเงิน +1.00 USDT
ปิด: stop
เลื่อน stop:
· stop เริ่ม 0.11 ล็อก -1.00R
เหตุเข้า: one-tap DOGE short · ความมั่นใจ 0.80
ตรวจอัตโนมัติ:
· เปิดไม่เกิน 2 ไม้: ผ่าน
· เข้าจากสัญญาณบอท: ผ่าน
· มี stop: ผ่าน
· เลื่อน stop เฉพาะทางแน่น: ผ่าน
· ปิดตามแผน: ผ่าน
· ไม่เข้าตอนพักแพ้ติด: ผ่าน"""


def test_one_tap_closed_trade_cards_once_and_skips_unmatched_buys(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "doge.db")
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "an-doge",
            "timestamp": ENTRY,
            "symbol": "DOGEUSDT",
            "timeframe": "4h",
            "regime_state": "trend_down",
            "confidence_score": 0.80,
            "action": "sell",
            "invalidation_price": 0.11,
            "size_pct_equity": 1.0,
            "thesis": "one-tap DOGE short",
        },
    )
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={"analysis_id": "an-doge", "stop": "0.11"},
        created_at_ms=ENTRY,
        expires_at_ms=ENTRY + 600_000,
    )
    _watch(conn, closed_at_ms=EXIT, symbol="DOGEUSDT", side="short")
    _trade(
        conn,
        entry_order_id="doge-1",
        symbol="DOGEUSDT",
        side="short",
        status="closed",
        signal_id=None,
        intent_id=intent_id,
        entry_fill=0.1,
        exit_fill=0.09,
        planned_stop=0.11,
        qty=100,
        loss_at_stop=1.0,
        net_r_real=1.0,
        realized=1.0,
        exit_kind="stop",
    )
    _trade(
        conn,
        entry_order_id="buy-old-1",
        symbol="DOGEUSDT",
        side="buy",
        status="unmatched",
        signal_id=None,
        entry_fill=0.0769,
        exit_fill=None,
        exit_time_ms=None,
        exit_kind="unknown",
        loss_at_stop=None,
        net_r_real=None,
        realized=None,
        planned_stop=None,
    )
    _trade(
        conn,
        entry_order_id="buy-old-2",
        symbol="DOGEUSDT",
        side="buy",
        status="unmatched",
        signal_id=None,
        entry_fill=0.081,
        exit_fill=None,
        exit_time_ms=None,
        exit_kind="unknown",
        loss_at_stop=None,
        net_r_real=None,
        realized=None,
        planned_stop=None,
    )
    sent: list[str] = []

    def send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int:
        sent.append(text)
        return 11

    result = journal_on_close(
        conn,
        [("DOGEUSDT", "BOTH")],
        futures=None,
        now_ms_value=DAYTIME,
        send=send,
        sync_fn=_noop_sync,
    )
    assert result["inserted"] == ["doge-1"]
    assert result["sent"] == ["doge-1"]
    assert sent == [DOGE_CARD]
    assert "0.0769" not in sent[0]
    stored = conn.execute(
        "SELECT auto_checks_json FROM assist_journal WHERE entry_order_id='doge-1'"
    ).fetchone()
    checks = json.loads(stored["auto_checks_json"])
    assert checks["signal_present"] is True
    ids = {
        row["entry_order_id"] for row in conn.execute("SELECT entry_order_id FROM assist_journal").fetchall()
    }
    assert ids == {"doge-1"}
    conn.close()


def test_closed_buy_is_never_carded_or_counted(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "buy.db")
    _watch(conn, closed_at_ms=EXIT, symbol="ADAUSDT", side="long")
    _trade(
        conn,
        entry_order_id="buy-closed",
        side="buy",
        status="closed",
        signal_id=None,
        entry_fill=0.0769,
        exit_kind="manual",
    )
    after_journal = JOURNAL_SHIPPED_MS + 60_000
    _trade(
        conn,
        entry_order_id="hand",
        side="long",
        status="closed",
        exit_kind="manual",
        signal_id="sig1",
        exit_time_ms=after_journal,
    )
    sent: list[str] = []

    def send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int:
        sent.append(text)
        return 4

    result = journal_on_close(
        conn,
        [("ADAUSDT", "BOTH")],
        futures=None,
        now_ms_value=DAYTIME,
        send=send,
        sync_fn=_noop_sync,
    )
    assert result["inserted"] == ["hand"]
    assert result["sent"] == ["hand"]
    assert "0.0769" not in sent[0]
    checks = json.loads(
        conn.execute("SELECT auto_checks_json FROM assist_journal WHERE entry_order_id='hand'").fetchone()[
            "auto_checks_json"
        ]
    )
    assert checks["exit_followed_plan"] is False
    conn.execute(
        """
        INSERT INTO assist_journal (
            entry_order_id, auto_checks_json, grade, mistake_tags, note,
            card_message_id, created_at_ms, graded_at_ms
        ) VALUES ('buy-closed', '{}', NULL, NULL, NULL, NULL, ?, NULL)
        """,
        (NOW,),
    )
    conn.commit()
    again = journal_on_close(
        conn,
        [],
        futures=None,
        now_ms_value=DAYTIME + GAP_MS,
        send=send,
        sync_fn=_noop_sync,
    )
    assert again["sent"] == []
    assert len(sent) == 1
    summary = summarize_journal(conn, since_ms=0, until_ms=after_journal + 1_000)
    assert summary["n"] == 1
    assert summary["ungraded"] == 1
    assert summary["pre_journal_n"] == 0
    conn.close()


def test_journal_runs_after_orphan_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "order.db")
    _watch(conn, closed_at_ms=None)
    order: list[str] = []

    def cancel(_trade: Any, _symbol: str) -> tuple[list[str], bool]:
        order.append("cancel")
        return (["algo-1"], True)

    def journal(_conn: Any, _closed: list[tuple[str, str]], **_k: Any) -> dict[str, Any]:
        order.append("journal")
        return {"ok": True}

    monkeypatch.setattr("jarvise_assist.watch.assist_execute_mode", lambda: "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (object(), None))
    monkeypatch.setattr("jarvise_assist.watch.cancel_jv_orders_for_symbol", cancel)
    monkeypatch.setattr("jarvise_assist.watch.journal_on_close", journal)
    result = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=NOW, send=lambda _t: True)
    assert result["ok"] is True
    assert order == ["cancel", "journal"]
    assert result["orphan_cancelled"] == [{"symbol": "ADAUSDT", "algo_id": "algo-1"}]
    conn.close()


def test_close_sync_throttles_then_gives_up(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn = open_db(tmp_path / "throttle.db")
    _watch(conn, closed_at_ms=NOW)
    calls: list[int] = []

    def sync(*_a: Any, **_k: Any) -> None:
        calls.append(1)

    def run(at: int) -> None:
        journal_on_close(
            conn,
            [("ADAUSDT", "BOTH")],
            futures=None,
            now_ms_value=at,
            send=lambda *_a, **_k: 1,
            sync_fn=sync,
        )

    caplog.set_level(logging.WARNING, logger="jarvise_assist.journal")
    run(NOW)
    run(NOW + GAP_MS - 1)
    assert calls == [1]
    for step in range(1, 4):
        run(NOW + step * GAP_MS)
    assert len(calls) == 4
    run(NOW + 4 * GAP_MS)
    run(NOW + 5 * GAP_MS)
    assert len(calls) == 4
    gave_up = [rec for rec in caplog.records if "gave up" in rec.message]
    assert len(gave_up) == 1
    assert conn.execute("SELECT COUNT(*) FROM assist_journal").fetchone()[0] == 0
    summary = summarize_journal(conn, since_ms=0, until_ms=NOW + 6 * GAP_MS)
    assert summary["n"] == 0
    assert summary["ungraded"] == 0
    conn.close()


def test_no_stop_advice_inside_window_fails_stop_existed(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "stop.db")
    _trade(conn, entry_order_id="ada", symbol="ADAUSDT", side="long")
    ada = dict(conn.execute("SELECT * FROM assist_real_trade WHERE entry_order_id='ada'").fetchone())
    insert_watch_advice(
        conn,
        symbol="ADAUSDT",
        position_side="BOTH",
        kind="no_stop",
        reason="missing",
        level=None,
        approx_r=None,
        status="sent",
        created_at_ms=ENTRY - 1,
    )
    assert collect_auto_checks(conn, ada)["stop_existed"] is True
    insert_watch_advice(
        conn,
        symbol="ADAUSDT",
        position_side="BOTH",
        kind="no_stop",
        reason="missing",
        level=None,
        approx_r=None,
        status="sent",
        created_at_ms=ENTRY,
    )
    assert collect_auto_checks(conn, ada)["stop_existed"] is False

    _trade(conn, entry_order_id="eth", symbol="ETHUSDT", side="short")
    eth = dict(conn.execute("SELECT * FROM assist_real_trade WHERE entry_order_id='eth'").fetchone())
    insert_watch_advice(
        conn,
        symbol="ETHUSDT",
        position_side="BOTH",
        kind="no_stop",
        reason="missing",
        level=None,
        approx_r=None,
        status="held",
        created_at_ms=EXIT + 1,
    )
    assert collect_auto_checks(conn, eth)["stop_existed"] is True
    insert_watch_advice(
        conn,
        symbol="ETHUSDT",
        position_side="BOTH",
        kind="no_stop",
        reason="partial",
        level=None,
        approx_r=None,
        status="sent",
        created_at_ms=EXIT,
    )
    assert collect_auto_checks(conn, eth)["stop_existed"] is False
    conn.close()


def test_night_hold_holds_cards_until_morning(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "night.db")
    night = _bkk(2026, 11, 6, 23, 30)
    still_night = _bkk(2026, 11, 7, 7, 29)
    morning = _bkk(2026, 11, 7, 7, 30)
    entry = night - 60_000
    _watch(conn, closed_at_ms=night, entry_time_ms=entry, symbol="ADAUSDT", side="long")
    _trade(
        conn,
        entry_order_id="ada-night",
        symbol="ADAUSDT",
        side="long",
        entry_time_ms=entry,
        exit_time_ms=night,
        signal_id="sig1",
    )
    _trade(
        conn,
        entry_order_id="eth-night",
        symbol="ETHUSDT",
        side="short",
        entry_time_ms=entry,
        exit_time_ms=night,
        signal_id="sig2",
    )
    sent: list[str] = []

    def send(text: str, *, reply_markup: dict[str, Any] | None = None) -> int:
        sent.append(text)
        return 20 + len(sent)

    held = journal_on_close(
        conn,
        [("ADAUSDT", "BOTH")],
        futures=None,
        now_ms_value=night,
        send=send,
        sync_fn=_noop_sync,
    )
    assert set(held["inserted"]) == {"ada-night", "eth-night"}
    assert held["sent"] == []
    assert sent == []
    still = journal_on_close(
        conn,
        [],
        futures=None,
        now_ms_value=still_night,
        send=send,
        sync_fn=_noop_sync,
    )
    assert still["sent"] == []
    assert sent == []
    morning_run = journal_on_close(
        conn,
        [],
        futures=None,
        now_ms_value=morning,
        send=send,
        sync_fn=_noop_sync,
    )
    assert set(morning_run["sent"]) == {"ada-night", "eth-night"}
    assert len(sent) == 2
    conn.close()
