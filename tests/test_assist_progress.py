"""Live-mark progress alerts. Close / move-stop buttons reuse existing xx:/ms: paths."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_candle
from tests.test_assist_execute import (
    RecordingTransport,
    _client,
    _open_then_flat,
    _verified_stop,
)

from jarvise_assist.execute import (
    MOVE_STOP_TTL_MS,
    MSG_ALREADY_CLOSED,
    MSG_EXIT_POSITION_CHANGED,
    MSG_MOVE_STOP_EXPIRED,
    client_order_id,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.journal import _exit_line, _last_advice
from jarvise_assist.keyboard import progress_keyboard
from jarvise_assist.listen import process_update
from jarvise_assist.messages import _fmt_price_tick, _lock_at_stop, format_progress_alert
from jarvise_assist.store import insert_exec_intent, insert_watch_advice, write_settings
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db

ENTRY = Decimal("0.1673")
INITIAL_STOP = Decimal("0.1748")
TICK = Decimal("0.0001")
QTY = Decimal("-100")
SUGGESTED_BE = Decimal("0.1670")
FOOTER = "แจ้งเพื่อทราบ · กติกาเดิม: เลื่อน BE เมื่อแท่ง 1h ปิดเกิน +0.5R · trail เมื่อเกิน +1R"


def _r_mark(r: str) -> Decimal:
    """Short mark at exactly this many R under the frozen initial stop."""
    return ENTRY - Decimal(r) * abs(ENTRY - INITIAL_STOP)


def _stop_order(price: str = "0.1748") -> dict[str, Any]:
    return {
        "symbol": "ADAUSDT",
        "side": "BUY",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "closePosition": True,
        "positionSide": "BOTH",
        "stopPrice": price,
    }


def _position(mark: Decimal, *, entry_ms: int, qty: Decimal = QTY) -> FuturesPosition:
    return FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=qty,
        entry_price=ENTRY,
        mark_price=mark,
        update_time_ms=entry_ms,
    )


def _seed_stop(conn: Any, now: int) -> None:
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=now - 8 * 3_600_000,
        open_=0.17,
        high=0.18,
        low=0.16,
        close=0.167,
        atr=0.005,
    )


def _capture() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    def send(text: str, reply_markup: dict[str, Any] | None = None, **kwargs: Any) -> bool:
        markup = reply_markup if reply_markup is not None else kwargs.get("reply_markup")
        sent.append({"text": text, "reply_markup": markup})
        return True

    return sent, send


def _progress(sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in sent if "แจ้งเพื่อทราบ" in row["text"]]


def _advice_rows(conn: Any) -> list[tuple[Any, ...]]:
    return list(
        conn.execute(
            """
            SELECT reason, level, status FROM assist_watch_advice
            WHERE kind='progress' ORDER BY created_at_ms, level
            """
        ).fetchall()
    )


def _watch(
    conn: Any,
    *,
    mark: Decimal,
    now: int,
    entry_ms: int,
    send: Any,
    stop_price: str = "0.1748",
    positions: list[Any] | None = None,
) -> dict[str, Any]:
    pos = _position(mark, entry_ms=entry_ms)
    fake = FakeFutures(
        positions=[pos] if positions is None else positions,
        orders=[_stop_order(stop_price)],
        ticks={"ADAUSDT": TICK},
    )
    return run_assist_watch(conn, futures=fake, now_ms_value=now, send=send)


def _seed_be_candle(conn: Any, entry_ms: int) -> None:
    """1h bar that reaches short breakeven (~0.57R) but not the 1R trail."""
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ms,
        open_=0.1673,
        high=0.1673,
        low=0.1630,
        close=0.1635,
        atr=0.005,
        timeframe="1h",
    )


def _preseed_move_dedupe(conn: Any, *, entry_ms: int) -> None:
    insert_watch_advice(
        conn,
        symbol="ADAUSDT",
        position_side="BOTH",
        kind="move_stop",
        reason="breakeven",
        level=float(SUGGESTED_BE),
        approx_r=None,
        status="sent",
        created_at_ms=entry_ms + 1_000,
        sent_at_ms=entry_ms + 1_000,
    )


def _move_stop_advice_count(conn: Any) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM assist_watch_advice WHERE kind='move_stop'").fetchone()[0])


def _decision_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prefix = "assist_watch_decision "
    for rec in caplog.records:
        if rec.name != "jarvise_assist.watch":
            continue
        msg = rec.getMessage()
        if msg.startswith(prefix):
            out.append(json.loads(msg[len(prefix) :]))
    return out


def _ada_exchange_info() -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": "ADAUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                ],
            }
        ]
    }


def _progress_move_handlers(
    *,
    intent_id: str,
    mark: str = "0.1636",
    entry: str = "0.1673",
    amt: str = "-100",
    old_trigger: str = "0.1748",
    new_stop: str = "0.1670",
    position_rows: Any = None,
) -> dict[tuple[str, str], Any]:
    old_order = {
        "symbol": "ADAUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": old_trigger,
        "quantity": "100",
        "algoId": 99,
        "clientAlgoId": "jv-old-sl",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    ms_row = _verified_stop(
        client_id=client_order_id(intent_id, "ms"),
        algo_id=100,
        trigger=new_stop,
        qty=amt.lstrip("-"),
        symbol="ADAUSDT",
    )
    n = {"i": 0}

    def _open(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        n["i"] += 1
        if n["i"] == 1:
            return (200, [old_order])
        return (200, [old_order, ms_row])

    pos = position_rows
    if pos is None:
        pos = (
            200,
            [
                {
                    "symbol": "ADAUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": amt,
                    "entryPrice": entry,
                    "markPrice": mark,
                }
            ],
        )
    return {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): pos,
        ("GET", "/fapi/v1/exchangeInfo"): (200, _ada_exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": mark}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): _open,
        ("GET", "/fapi/v1/algoOrder"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("POST", "/fapi/v1/algoOrder"): (200, {"algoId": 100}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 99, "code": "200", "msg": "success"}),
    }


def test_progress_half_r_once_then_silent_below_on_a_fresh_trade(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "half.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    sent, send = _capture()
    first = _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    alerts = _progress(sent)
    assert len(alerts) == 1
    assert "ถึง +0.5R" in alerts[0]["text"]
    assert [a for a in first["actions"] if a.get("action") == "progress"] == [
        {"symbol": "ADAUSDT", "action": "progress", "level": 0.5, "status": "sent"}
    ]
    _watch(conn, mark=_r_mark("0.5"), now=day + 5 * 60_000, entry_ms=entry_ms, send=send)
    assert len(_progress(sent)) == 1

    fresh = open_db(tmp_path / "below.db")
    _seed_stop(fresh, day)
    quiet, quiet_send = _capture()
    _watch(fresh, mark=Decimal("0.16356"), now=day, entry_ms=entry_ms, send=quiet_send)
    assert _progress(quiet) == []
    assert _advice_rows(fresh) == []
    conn.close()
    fresh.close()


def test_progress_jump_sends_only_the_highest_new_level(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "jump.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.3"), now=day, entry_ms=entry_ms, send=send)
    assert _progress(sent) == []
    jumped = _watch(conn, mark=_r_mark("1.2"), now=day + 60_000, entry_ms=entry_ms, send=send)
    alerts = _progress(sent)
    assert len(alerts) == 1
    assert "ถึง +1R" in alerts[0]["text"]
    assert "ถึง +0.5R" not in alerts[0]["text"]
    assert "ตอนนี้ +1.20R" in alerts[0]["text"]
    progress_actions = [a for a in jumped["actions"] if a.get("action") == "progress"]
    assert progress_actions == [{"symbol": "ADAUSDT", "action": "progress", "level": 1.0, "status": "sent"}]
    assert {(row[0], row[2]) for row in _advice_rows(conn)} == {("r0.5", "sent"), ("r1", "sent")}

    _watch(conn, mark=_r_mark("0.6"), now=day + 120_000, entry_ms=entry_ms, send=send)
    assert len(_progress(sent)) == 1
    _watch(conn, mark=_r_mark("1.5"), now=day + 180_000, entry_ms=entry_ms, send=send)
    alerts = _progress(sent)
    assert len(alerts) == 2
    assert "ถึง +1.5R" in alerts[1]["text"]
    assert "ตอนนี้ +1.50R" in alerts[1]["text"]
    assert ("r1.5", "sent") in {(row[0], row[2]) for row in _advice_rows(conn)}
    conn.close()


def test_progress_silent_through_night_hold_then_sends_by_day(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "night.db")
    night = _ms_bangkok(2026, 11, 13, 23, 30)
    morning = _ms_bangkok(2026, 11, 14, 7, 30)
    entry_ms = night - 2 * 3_600_000
    _seed_stop(conn, night)
    sent, send = _capture()
    held = _watch(conn, mark=_r_mark("0.5"), now=night, entry_ms=entry_ms, send=send)
    assert held["night_hold"] is True
    assert _progress(sent) == []
    assert _advice_rows(conn) == []
    day = _watch(conn, mark=_r_mark("0.5"), now=morning, entry_ms=entry_ms, send=send)
    assert day["night_hold"] is False
    assert len(_progress(sent)) == 1
    assert "ถึง +0.5R" in _progress(sent)[0]["text"]
    conn.close()


def test_progress_new_trade_alerts_again_and_missing_entry_time_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(tmp_path / "reopen.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 2 * 3_600_000
    _seed_stop(conn, day)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    assert len(_progress(sent)) == 1
    _watch(conn, mark=_r_mark("0.5"), now=day + 60_000, entry_ms=entry_ms, send=send, positions=[])
    later = day + 10 * 60_000
    _watch(conn, mark=_r_mark("0.5"), now=later + 60_000, entry_ms=later, send=send)
    assert len(_progress(sent)) == 2

    bare = open_db(tmp_path / "nosince.db")
    _seed_stop(bare, day)
    import jarvise_assist.watch as watch_mod

    real = watch_mod._link_or_freeze_stop

    def _drop_entry_time(db: Any, pos: Any, stored: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        row = dict(real(db, pos, stored, now_ms=now_ms))
        row["entry_time_ms"] = None
        return row

    monkeypatch.setattr(watch_mod, "_link_or_freeze_stop", _drop_entry_time)
    quiet, quiet_send = _capture()
    _watch(bare, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=quiet_send)
    assert _progress(quiet) == []
    assert _advice_rows(bare) == []
    conn.close()
    bare.close()


def test_progress_send_failure_stores_no_row_and_retries(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "retry.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    sent: list[str] = []
    state = {"n": 0}

    def send(text: str, **_kwargs: Any) -> bool:
        state["n"] += 1
        if state["n"] == 1:
            return False
        sent.append(text)
        return True

    failed = _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    assert _advice_rows(conn) == []
    assert [a for a in failed["actions"] if a.get("action") == "progress"][0]["status"] == "failed"
    retried = _watch(conn, mark=_r_mark("0.5"), now=day + 60_000, entry_ms=entry_ms, send=send)
    assert len(sent) == 1
    assert "ถึง +0.5R" in sent[0]
    assert [a for a in retried["actions"] if a.get("action") == "progress"][0]["status"] == "sent"
    assert len(_advice_rows(conn)) == 1
    conn.close()


def test_progress_text_prices_at_tick_and_stop_in_r(tmp_path: Path) -> None:
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    conn = open_db(tmp_path / "text.db")
    _seed_stop(conn, day)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    text = _progress(sent)[0]["text"]
    assert text == (
        "📈 ADAUSDT SHORT ถึง +0.5R แล้ว (ตอนนี้ +0.50R)\n"
        "mark 0.1636 · entry 0.1673 · stop ปัจจุบัน 0.1748 (-1.00R)\n"
        f"{FOOTER}"
    )
    assert "กด ✅" not in text
    conn.close()

    tight = open_db(tmp_path / "exch.db")
    _seed_stop(tight, day)
    sent2, send2 = _capture()
    _watch(
        tight,
        mark=_r_mark("0.5"),
        now=day,
        entry_ms=entry_ms,
        send=send2,
        stop_price="0.1676",
    )
    assert _progress(sent2)[0]["text"] == (
        "📈 ADAUSDT SHORT ถึง +0.5R แล้ว (ตอนนี้ +0.50R)\n"
        "mark 0.1636 · entry 0.1673 · stop ปัจจุบัน 0.1676 (-0.04R)\n"
        f"{FOOTER}"
    )
    tight.close()


@pytest.mark.parametrize("mode", ["off", "testnet"])
def test_progress_outside_live_has_text_and_no_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", mode)
    conn = open_db(tmp_path / f"{mode}.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    _seed_stop(conn, day)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=day - 3_600_000, send=send)
    assert len(_progress(sent)) == 1
    assert _progress(sent)[0]["reply_markup"] is None
    assert "กด ✅" not in _progress(sent)[0]["text"]
    assert not any(line.startswith("กด ") for line in _progress(sent)[0]["text"].splitlines())
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == 0
    conn.close()


def test_progress_live_attaches_the_early_close_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "live.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    _seed_stop(conn, day)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=day - 3_600_000, send=send)
    alerts = _progress(sent)
    assert len(alerts) == 1
    assert alerts[0]["text"].endswith("กด ⚠️ ปิดก่อนแผน = ปิดทั้งไม้ที่ราคาตลาดตอนกด · ไม่ใช่ทางออกตามแผน")
    row = conn.execute("SELECT id, kind, preview_json, expires_at_ms FROM assist_exec_intent").fetchone()
    assert row is not None
    intent_id, kind, preview_json, expires_at = row
    assert kind == "exit"
    assert json.loads(preview_json) == {
        "qty": "100",
        "entry": "0.1673",
        "source": "progress",
        "level": "0.5",
    }
    assert expires_at == day + 120_000
    assert alerts[0]["reply_markup"] == progress_keyboard(str(intent_id), early=True)
    button = alerts[0]["reply_markup"]["inline_keyboard"][0][0]
    assert button == {"text": "⚠️ ปิดก่อนแผน", "callback_data": f"xx:{intent_id}"}
    conn.close()


def test_progress_exit_tap_reuses_run_exit_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    conn = open_db(tmp_path / "tap.db")
    ts = _ms_bangkok(2026, 11, 14, 12, 0)
    live = {
        "symbol": "ADAUSDT",
        "positionSide": "BOTH",
        "positionAmt": "-100",
        "entryPrice": "0.1673",
        "markPrice": "0.16355",
    }

    def _tap(preview: dict[str, str], position_rows: Any) -> tuple[list[str], list[str], RecordingTransport]:
        intent_id = insert_exec_intent(
            conn,
            kind="exit",
            symbol="ADAUSDT",
            side="short",
            preview=preview,
            created_at_ms=ts,
            expires_at_ms=ts + 120_000,
        )
        handlers: dict[tuple[str, str], Any] = {
            ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
            ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
            ("GET", "/fapi/v3/positionRisk"): position_rows,
            ("POST", "/fapi/v1/order"): (
                200,
                {"orderId": 9, "avgPrice": "0.1636", "executedQty": "100", "status": "FILLED"},
            ),
            ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.37"}]),
            ("GET", "/fapi/v1/openOrders"): (200, []),
            ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        }
        transport = RecordingTransport(handlers)
        trade = _client(transport)
        monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (trade, None))
        sent: list[str] = []
        answers: list[str | None] = []

        def send(text: str, **_kwargs: Any) -> bool:
            sent.append(text)
            return True

        def answer(_cq: str, text: str | None = None, **_kwargs: Any) -> None:
            answers.append(text)

        process_update(
            conn,
            {
                "callback_query": {
                    "id": "cq-progress",
                    "data": f"xx:{intent_id}",
                    "message": {"chat": {"id": 111}, "message_id": 4, "text": "progress"},
                }
            },
            send=send,
            answer=answer,
            edit=lambda *_a, **_k: True,
            now_ms_value=ts,
        )
        return sent, answers, transport

    changed, _answers, changed_transport = _tap(
        {"qty": "100", "entry": "0.1673", "source": "progress", "level": "0.5"},
        (
            200,
            [
                {
                    "symbol": "ADAUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "-80",
                    "entryPrice": "0.1600",
                    "markPrice": "0.1590",
                }
            ],
        ),
    )
    assert changed == [MSG_EXIT_POSITION_CHANGED]
    assert not any(call[0] == "POST" for call in changed_transport.calls)

    sent, _answers, transport = _tap(
        {"qty": "100", "entry": "0.1673", "source": "progress", "level": "0.5"},
        _open_then_flat([live]),
    )
    assert any(text.startswith("✅ ปิด ADAUSDT") for text in sent)
    posts = [call for call in transport.calls if call[0] == "POST" and call[1] == "/fapi/v1/order"]
    assert len(posts) == 1
    assert posts[0][2]["reduceOnly"] == "true"
    assert posts[0][2]["side"] == "BUY"
    assert Decimal(posts[0][2]["quantity"]) == Decimal("100")

    # Assistant off: progress xx: still works (protective gate; execute mode stays testnet).
    write_settings(conn, enabled=False)
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    off_sent, _off_answers, off_transport = _tap(
        {"qty": "100", "entry": "0.1673", "source": "progress", "level": "0.5"},
        _open_then_flat([live]),
    )
    assert any(text.startswith("✅ ปิด ADAUSDT") for text in off_sent)
    assert not any(text == "ปิดอยู่" for text in off_sent)
    off_posts = [call for call in off_transport.calls if call[0] == "POST" and call[1] == "/fapi/v1/order"]
    assert len(off_posts) == 1
    conn.close()


def test_progress_live_deduped_move_offers_move_and_early_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "dedupe-move.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    _seed_be_candle(conn, entry_ms)
    _preseed_move_dedupe(conn, entry_ms=entry_ms)
    before = _move_stop_advice_count(conn)
    sent, send = _capture()
    with caplog.at_level(logging.INFO, logger="jarvise_assist.watch"):
        _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    alerts = _progress(sent)
    assert len(alerts) == 1
    _, lock_r = _lock_at_stop(
        side="short",
        qty=Decimal("100"),
        entry=ENTRY,
        initial_stop=INITIAL_STOP,
        new_stop=SUGGESTED_BE,
    )
    expected = format_progress_alert(
        symbol="ADAUSDT",
        side="short",
        level=Decimal("0.5"),
        r_now=Decimal("0.5"),
        mark=_r_mark("0.5"),
        entry=ENTRY,
        stop=INITIAL_STOP,
        stop_r=Decimal("-1"),
        tick=TICK,
        with_button=True,
        pending_stop=SUGGESTED_BE,
        pending_source="breakeven",
        pending_lock_r=lock_r,
        move_button=True,
        move_expires_at_ms=day + MOVE_STOP_TTL_MS,
        early_close=True,
    )
    assert alerts[0]["text"] == expected
    assert "📌 ยังไม่ได้เลื่อน stop ตามคำแนะนำ: → 0.1670 (ทุน)" in alerts[0]["text"]
    rows = list(
        conn.execute(
            "SELECT id, kind, preview_json, expires_at_ms FROM assist_exec_intent ORDER BY kind, id"
        ).fetchall()
    )
    assert len(rows) == 2
    by_kind = {str(r[1]): r for r in rows}
    exit_id = str(by_kind["exit"][0])
    move_id = str(by_kind["move_stop"][0])
    assert json.loads(by_kind["exit"][2]) == {
        "qty": "100",
        "entry": "0.1673",
        "source": "progress",
        "level": "0.5",
    }
    move_preview = json.loads(by_kind["move_stop"][2])
    assert move_preview == {
        "stop": str(SUGGESTED_BE),
        "qty": "100",
        "advice_text": alerts[0]["text"],
        "source": "breakeven",
        "tick": str(TICK),
        "via": "progress",
        "level": "0.5",
    }
    assert by_kind["move_stop"][3] == day + MOVE_STOP_TTL_MS
    assert alerts[0]["reply_markup"] == progress_keyboard(
        exit_id,
        move_intent_id=move_id,
        new_stop=_fmt_price_tick(SUGGESTED_BE, TICK),
        early=True,
    )
    kb = alerts[0]["reply_markup"]["inline_keyboard"]
    assert len(kb) == 2
    assert kb[0][0]["callback_data"] == f"ms:{move_id}"
    assert kb[1][0]["callback_data"] == f"xx:{exit_id}"
    assert _move_stop_advice_count(conn) == before
    decisions = _decision_payloads(caplog)
    assert any(d.get("decision") == "skipped_dedupe" for d in decisions)
    conn.close()


def test_progress_live_without_pending_move_has_only_early_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "no-pending.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    _seed_stop(conn, day)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=day - 3_600_000, send=send)
    alert = _progress(sent)[0]
    assert "📌" not in alert["text"]
    assert "🛡" not in alert["text"]
    kb = alert["reply_markup"]["inline_keyboard"]
    assert len(kb) == 1
    assert kb[0][0]["text"] == "⚠️ ปิดก่อนแผน"
    assert kb[0][0]["callback_data"].startswith("xx:")
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent WHERE kind='move_stop'").fetchone()[0] == 0
    conn.close()


def test_progress_live_fresh_move_advice_keeps_its_own_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "fresh-move.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    _seed_be_candle(conn, entry_ms)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    progress = _progress(sent)[0]
    assert "ms:" not in json.dumps(progress["reply_markup"])
    assert "📌" not in progress["text"]
    move_msgs = [row for row in sent if "→" in row["text"] and "ปุ่มหมดอายุ" in row["text"]]
    assert len(move_msgs) == 1
    assert move_msgs[0]["reply_markup"] is not None
    assert move_msgs[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("ms:")
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent WHERE kind='move_stop'").fetchone()[0] == 1
    conn.close()


def test_progress_no_reminder_when_stop_already_at_or_beyond_advice(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "already.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    _seed_be_candle(conn, entry_ms)
    sent, send = _capture()
    _watch(
        conn,
        mark=_r_mark("0.5"),
        now=day,
        entry_ms=entry_ms,
        send=send,
        stop_price="0.1670",
    )
    assert "📌" not in _progress(sent)[0]["text"]
    conn.close()


@pytest.mark.parametrize("mode", ["off", "testnet"])
def test_progress_reminder_text_only_outside_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", mode)
    conn = open_db(tmp_path / f"reminder-{mode}.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    _seed_be_candle(conn, entry_ms)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    alert = _progress(sent)[0]
    assert alert["reply_markup"] is None
    assert "เลื่อนเองในแอป (Stop-Market, Reduce-Only, Mark Price)" in alert["text"]
    assert "📌 ยังไม่ได้เลื่อน stop ตามคำแนะนำ: → 0.1670 (ทุน)" in alert["text"]
    assert not any(line.startswith("กด ") for line in alert["text"].splitlines())
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == 0
    conn.close()


def test_progress_close_label_when_exit_advice_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "exit-active.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    insert_watch_advice(
        conn,
        symbol="ADAUSDT",
        position_side="BOTH",
        kind="exit",
        reason="flat",
        level=None,
        approx_r=0.5,
        status="sent",
        created_at_ms=entry_ms + 1_000,
        sent_at_ms=entry_ms + 1_000,
    )
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    alert = _progress(sent)[0]
    assert alert["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ ปิดไม้"
    assert alert["text"].endswith("กด ✅ ปิดไม้ = ปิดทั้งไม้ที่ราคาตลาดตอนกด")
    conn.close()


def test_progress_move_tap_reuses_run_move_stop_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "ms-tap.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    _seed_be_candle(conn, entry_ms)
    _preseed_move_dedupe(conn, entry_ms=entry_ms)
    sent, send = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send)
    move_id = str(conn.execute("SELECT id FROM assist_exec_intent WHERE kind='move_stop'").fetchone()[0])
    # Taps use testnet + demo-fapi mock (same as exit tap); live key-safety is not under test.
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")

    def _tap(intent: str, *, disabled: bool = False) -> tuple[list[str], RecordingTransport]:
        if disabled:
            write_settings(conn, enabled=False)
        handlers = _progress_move_handlers(intent_id=intent)
        transport = RecordingTransport(handlers)
        trade = _client(transport)
        monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (trade, None))
        out: list[str] = []

        def _send(text: str, **_kwargs: Any) -> bool:
            out.append(text)
            return True

        process_update(
            conn,
            {
                "callback_query": {
                    "id": "cq-ms",
                    "data": f"ms:{intent}",
                    "message": {"chat": {"id": 111}, "message_id": 4, "text": "progress"},
                }
            },
            send=_send,
            answer=lambda *_a, **_k: None,
            edit=lambda *_a, **_k: True,
            now_ms_value=day,
        )
        return out, transport

    out, transport = _tap(move_id)
    assert any(text.startswith("✅") for text in out)
    posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
    assert len(posts) == 1
    assert posts[0][2].get("reduceOnly") == "true"
    assert posts[0][2].get("quantity") == "100"
    assert "closePosition" not in posts[0][2]
    deletes = [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"]
    assert len(deletes) == 1

    move2 = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="ADAUSDT",
        side="short",
        preview={
            "stop": str(SUGGESTED_BE),
            "qty": "100",
            "advice_text": "progress body",
            "source": "breakeven",
            "tick": str(TICK),
            "via": "progress",
            "level": "0.5",
        },
        created_at_ms=day,
        expires_at_ms=day + MOVE_STOP_TTL_MS,
    )
    off_out, off_transport = _tap(move2, disabled=True)
    assert any(text.startswith("✅") for text in off_out)
    assert not any(text == "ปิดอยู่" for text in off_out)
    assert any(c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" for c in off_transport.calls)
    conn.close()


def test_progress_move_tap_expired_restores_notice_without_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    conn = open_db(tmp_path / "ms-exp.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="ADAUSDT",
        side="short",
        preview={
            "stop": str(SUGGESTED_BE),
            "qty": "100",
            "advice_text": "progress notice body",
            "source": "breakeven",
            "tick": str(TICK),
            "via": "progress",
            "level": "0.5",
        },
        created_at_ms=day,
        expires_at_ms=day + 1_000,
    )
    transport = RecordingTransport(_progress_move_handlers(intent_id=intent_id))
    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (_client(transport), None))
    sent: list[str] = []
    edits: list[tuple[Any, ...]] = []

    process_update(
        conn,
        {
            "callback_query": {
                "id": "cq-exp",
                "data": f"ms:{intent_id}",
                "message": {"chat": {"id": 111}, "message_id": 9, "text": "progress"},
            }
        },
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda mid, text, **kw: edits.append((mid, text, kw.get("reply_markup"))),
        now_ms_value=day + MOVE_STOP_TTL_MS + 1,
    )
    assert sent == [MSG_MOVE_STOP_EXPIRED]
    assert edits == [(9, "progress notice body", {"inline_keyboard": []})]
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_progress_move_tap_level_changed_sends_fresh_advice_no_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "ms-changed.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    _seed_stop(conn, day)
    _seed_be_candle(conn, entry_ms)
    _preseed_move_dedupe(conn, entry_ms=entry_ms)
    sent_watch, send_watch = _capture()
    _watch(conn, mark=_r_mark("0.5"), now=day, entry_ms=entry_ms, send=send_watch)
    move_id = str(conn.execute("SELECT id FROM assist_exec_intent WHERE kind='move_stop'").fetchone()[0])
    # Deepen the already-closed entry bar so tap-time trail leaves the button BE.
    # Keep now within MOVE_STOP_TTL_MS so expiry does not win first.
    conn.execute(
        "UPDATE market_technicals SET low=?, close=? WHERE symbol=? AND timeframe=? AND timestamp=?",
        (0.1580, 0.1590, "ADAUSDT", "1h", entry_ms),
    )
    conn.commit()
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    handlers = _progress_move_handlers(intent_id=move_id)
    transport = RecordingTransport(handlers)
    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (_client(transport), None))
    sent: list[str] = []
    process_update(
        conn,
        {
            "callback_query": {
                "id": "cq-chg",
                "data": f"ms:{move_id}",
                "message": {"chat": {"id": 111}, "message_id": 4, "text": "progress"},
            }
        },
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms_value=day + 60_000,
    )
    assert any("stop แนะนำเปลี่ยนเป็น" in t for t in sent)
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_progress_move_tap_after_close_no_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    conn = open_db(tmp_path / "ms-closed.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="ADAUSDT",
        side="short",
        preview={
            "stop": str(SUGGESTED_BE),
            "qty": "100",
            "advice_text": "progress",
            "source": "breakeven",
            "tick": str(TICK),
            "via": "progress",
            "level": "0.5",
        },
        created_at_ms=day,
        expires_at_ms=day + MOVE_STOP_TTL_MS,
    )
    handlers = _progress_move_handlers(intent_id=intent_id, position_rows=(200, []))
    transport = RecordingTransport(handlers)
    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (_client(transport), None))
    sent: list[str] = []
    process_update(
        conn,
        {
            "callback_query": {
                "id": "cq-closed",
                "data": f"ms:{intent_id}",
                "message": {"chat": {"id": 111}, "message_id": 4, "text": "progress"},
            }
        },
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms_value=day,
    )
    assert sent == [MSG_ALREADY_CLOSED]
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_last_advice_ignores_progress_rows(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "journal.db")
    entry_ms = 1_700_000_000_000
    exit_ms = entry_ms + 3_600_000
    trade = {
        "symbol": "ADAUSDT",
        "entry_time_ms": entry_ms,
        "exit_time_ms": exit_ms,
        "exit_kind": "stop",
    }
    insert_watch_advice(
        conn,
        symbol="ADAUSDT",
        position_side="BOTH",
        kind="exit",
        reason="flat",
        level=None,
        approx_r=1.0,
        status="sent",
        created_at_ms=entry_ms + 1_000,
        sent_at_ms=entry_ms + 1_000,
    )
    insert_watch_advice(
        conn,
        symbol="ADAUSDT",
        position_side="BOTH",
        kind="progress",
        reason="r0.5",
        level=0.5,
        approx_r=0.65,
        status="sent",
        created_at_ms=entry_ms + 2_000,
        sent_at_ms=entry_ms + 2_000,
    )
    advice = _last_advice(conn, trade)
    assert advice is not None
    assert advice["kind"] == "exit"
    assert advice["reason"] == "flat"
    line = _exit_line(conn, trade)
    assert line == "ปิด: stop · คำแนะนำล่าสุด: exit (flat)"
    assert "progress" not in line

    only = open_db(tmp_path / "only-progress.db")
    insert_watch_advice(
        only,
        symbol="ADAUSDT",
        position_side="BOTH",
        kind="progress",
        reason="r1",
        level=1.0,
        approx_r=1.2,
        status="sent",
        created_at_ms=entry_ms + 2_000,
        sent_at_ms=entry_ms + 2_000,
    )
    assert _last_advice(only, trade) is None
    assert _exit_line(only, trade) == "ปิด: stop"
    conn.close()
    only.close()


def test_progress_is_sent_before_move_stop_and_exit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "both.db")
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 2 * 3_600_000
    _seed_stop(conn, day)
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ms,
        open_=0.1673,
        high=0.1673,
        low=0.1598,
        close=0.1600,
        atr=0.005,
        timeframe="1h",
    )
    sent, send = _capture()
    out = _watch(conn, mark=_r_mark("2"), now=day, entry_ms=entry_ms, send=send)
    kinds: list[str] = []
    for row in sent:
        text = row["text"]
        if "แจ้งเพื่อทราบ" in text:
            kinds.append("progress")
        elif "ออกตอนนี้" in text:
            kinds.append("exit")
        elif "→" in text:
            kinds.append("move_stop")
    assert kinds == ["progress", "move_stop", "exit"]
    assert "ถึง +1.5R" in sent[0]["text"]
    actions = [a.get("action") for a in out["actions"]]
    assert actions.index("progress") < actions.index("move_stop") < actions.index("exit")
    conn.close()
