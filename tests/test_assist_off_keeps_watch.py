"""Assistant off stops new signals/entry buttons; protective watch stays on."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis, _seed_candle
from tests.test_assist_execute import (
    RecordingTransport,
    _client,
    _move_stop_handlers,
    _open_then_flat,
)
from tests.test_assist_progress import _capture, _progress

from jarvise_assist.execute import (
    MOVE_STOP_TTL_MS,
    MSG_OFF,
    client_order_id,
    handle_exec_callback,
    protective_buttons_enabled,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.listen import process_update
from jarvise_assist.menu import render_anything_now, toggle_enabled
from jarvise_assist.signals import run_assist_signals
from jarvise_assist.sizing import size_signal
from jarvise_assist.store import (
    ensure_settings,
    get_exec_intent,
    insert_exec_intent,
    insert_signal,
    insert_watch_advice,
    upsert_watch_position,
    write_settings,
)
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db

ONE_H_MS = 3_600_000


def _btc_move_setup(conn: Any, *, day: int) -> FakeFutures:
    entry_ts = day - 2 * ONE_H_MS
    _seed_candle(
        conn,
        symbol="BTCUSDT",
        ts=entry_ts,
        open_=100,
        high=106,
        low=99,
        close=105,
        atr=6.67,
        timeframe="1h",
    )
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("105"),
        update_time_ms=entry_ts,
    )
    return FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "90",
            }
        ],
        marks={"BTCUSDT": Decimal("105")},
    )


def test_off_live_daytime_watch_emits_protective_advice_with_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    conn = open_db(tmp_path / "watch.db")
    write_settings(conn, enabled=False)
    day = _ms_bangkok(2026, 10, 27, 14, 0)

    # Move-stop + progress (+0.5R) on BTC long.
    sent, send = _capture()
    out = run_assist_watch(conn, futures=_btc_move_setup(conn, day=day), now_ms_value=day, send=send)
    assert out.get("ok") is True
    assert out.get("assist_enabled") is False
    assert "skipped" not in out or out.get("skipped") is None
    assert any(a.get("action") == "move_stop" for a in out["actions"])
    assert any(a.get("action") == "progress" for a in out["actions"])
    move_msgs = [m for m in sent if "→" in m["text"] and "stop" in m["text"]]
    assert move_msgs
    assert any(
        "ms:" in json.dumps(m.get("reply_markup") or {}) and "mn:" in json.dumps(m.get("reply_markup") or {})
        for m in move_msgs
    )
    prog = _progress(sent)
    assert prog
    assert any("xx:" in json.dumps(m.get("reply_markup") or {}) for m in prog)

    # Exit advice with xx: (FLAT analysis).
    conn2 = open_db(tmp_path / "exit.db")
    write_settings(conn2, enabled=False)
    entry_ts = day - 4 * ONE_H_MS
    _seed_candle(
        conn2,
        symbol="ADAUSDT",
        ts=entry_ts - 4 * ONE_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    sized = size_signal(
        symbol="ADAUSDT",
        side="short",
        entry=Decimal("0.40"),
        stop=Decimal("0.43"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    insert_signal(
        conn2,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    _seed_analysis(conn2, symbol="ADAUSDT", action="flat", confidence=0.9, invalidation=None, ts=day - 60_000)
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.39"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "ADAUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.43",
            }
        ],
        marks={"ADAUSDT": Decimal("0.39")},
    )
    sent2, send2 = _capture()
    out2 = run_assist_watch(conn2, futures=fake, now_ms_value=day, send=send2)
    assert any(a.get("action") == "exit" for a in out2["actions"])
    exit_msgs = [m for m in sent2 if "FLAT" in m["text"]]
    assert exit_msgs
    assert any("xx:" in json.dumps(m.get("reply_markup") or {}) for m in exit_msgs)

    # Missing-stop warning.
    conn3 = open_db(tmp_path / "nostop.db")
    write_settings(conn3, enabled=False)
    _seed_candle(
        conn3,
        symbol="ADAUSDT",
        ts=entry_ts - 4 * ONE_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    pos3 = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.40"),
        update_time_ms=entry_ts,
    )
    sent3, send3 = _capture()
    out3 = run_assist_watch(
        conn3,
        futures=FakeFutures(positions=[pos3], marks={"ADAUSDT": Decimal("0.40")}),
        now_ms_value=day,
        send=send3,
    )
    assert any(a.get("action") == "no_stop" for a in out3["actions"])
    assert any("ไม่มี stop order" in m["text"] for m in sent3)

    # Close journaling + orphan jv- cancel still run while off.
    order: list[str] = []

    def cancel(_trade: Any, _symbol: str) -> tuple[list[str], bool]:
        order.append("cancel")
        return (["algo-1"], True)

    def journal(_conn: Any, _closed: list[tuple[str, str]], **_k: Any) -> dict[str, Any]:
        order.append("journal")
        return {"ok": True}

    conn4 = open_db(tmp_path / "orphan.db")
    write_settings(conn4, enabled=False)
    upsert_watch_position(
        conn4,
        {
            "symbol": "ADAUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.4,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.43,
            "initial_stop_source": "signal",
            "entry_time_ms": entry_ts,
            "no_stop_latched": 0,
            "last_seen_ms": entry_ts,
            "closed_at_ms": None,
        },
    )
    monkeypatch.setattr("jarvise_assist.watch.assist_execute_mode", lambda: "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (object(), None))
    monkeypatch.setattr("jarvise_assist.watch.cancel_jv_orders_for_symbol", cancel)
    monkeypatch.setattr("jarvise_assist.watch.journal_on_close", journal)
    result = run_assist_watch(
        conn4,
        futures=FakeFutures(positions=[]),
        now_ms_value=day,
        send=lambda *_a, **_k: True,
    )
    assert result["ok"] is True
    assert order == ["cancel", "journal"]
    assert result["orphan_cancelled"] == [{"symbol": "ADAUSDT", "algo_id": "algo-1"}]
    conn.close()
    conn2.close()
    conn3.close()
    conn4.close()


def test_off_protective_taps_work_entry_taps_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # testnet skips sapi key checks; protective gate is mode != "off".
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "taps.db")
    write_settings(conn, enabled=False)
    ts = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ts = ts - 2 * ONE_H_MS
    upsert_watch_position(
        conn,
        {
            "symbol": "DOGEUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.08,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.083,
            "initial_stop_source": "signal",
            "entry_time_ms": entry_ts,
            "no_stop_latched": 0,
            "last_seen_ms": entry_ts,
            "closed_at_ms": None,
        },
    )
    from jarvise_ingest.db import upsert_market_technicals

    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "DOGEUSDT",
                "timestamp": entry_ts,
                "timeframe": "1h",
                "open": 0.08,
                "high": 0.08,
                "low": 0.076,
                "close": 0.077,
                "volume": 1.0,
            }
        ],
    )
    ms_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice", "tick": "0.00001"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    handlers = _move_stop_handlers(ms_client_id=client_order_id(ms_id, "ms"), mark="0.078")
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (trade, None))
    sent: list[str] = []
    answers: list[str | None] = []
    out_ms = handle_exec_callback(
        conn,
        f"ms:{ms_id}",
        send=lambda t, **_k: sent.append(t) or True,
        answer=lambda *_a, **_k: answers.append(None),
        edit=lambda *_a, **_k: True,
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert out_ms.get("ok") is True or any("เลื่อน" in t or "stop" in t.lower() for t in sent)
    assert MSG_OFF not in sent
    assert "ปิดอยู่" not in answers
    row = get_exec_intent(conn, ms_id)
    assert row is not None
    assert row["status"] != "preview"

    xx_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    live = {
        "symbol": "DOGEUSDT",
        "positionSide": "BOTH",
        "positionAmt": "-100",
        "entryPrice": "0.08",
        "markPrice": "0.07",
    }
    xx_handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([live]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "1"}]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    xx_transport = RecordingTransport(xx_handlers)
    xx_trade = _client(xx_transport)
    monkeypatch.setattr("jarvise_assist.execute.build_trade_client", lambda: (xx_trade, None))
    sent_xx: list[str] = []
    out_xx = handle_exec_callback(
        conn,
        f"xx:{xx_id}",
        send=lambda t, **_k: sent_xx.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        trade=xx_trade,
        now_ms_value=ts,
        message={"message_id": 2},
    )
    assert out_xx.get("ok") is True or any("ปิด" in t for t in sent_xx)
    assert MSG_OFF not in sent_xx

    # Entry taps still refuse while assistant is off.
    for data in ("ec:DOGEUSDT:s", "ey:aei_deadbeefdeadbeef"):
        answers_e: list[str | None] = []
        sent_e: list[str] = []

        def _send_e(t: str, *, _s: list[str] = sent_e, **_k: Any) -> bool:
            _s.append(t)
            return True

        def _answer_e(
            _cq: str, text: str | None = None, *, _a: list[str | None] = answers_e, **_k: Any
        ) -> None:
            _a.append(text)

        process_update(
            conn,
            {
                "callback_query": {
                    "id": f"cq-{data[:2]}",
                    "data": data,
                    "message": {"chat": {"id": 111}, "message_id": 9, "text": "x"},
                }
            },
            send=_send_e,
            answer=_answer_e,
            now_ms_value=ts,
        )
        assert "ปิดอยู่" in answers_e or MSG_OFF in sent_e
    conn.close()


def test_off_signals_still_skipped(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sig.db")
    write_settings(conn, enabled=False)
    out = run_assist_signals(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=_ms_bangkok(2026, 10, 27, 14, 0),
        send=lambda *_a, **_k: True,
    )
    assert out.get("skipped") == "assist_disabled"
    conn.close()


def test_off_skips_pause_evaluators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "pause.db")
    write_settings(conn, enabled=False)
    calls = {"auto": 0, "pause": 0, "day": 0}

    def _auto(*_a: Any, **_k: Any) -> bool:
        calls["auto"] += 1
        return False

    def _pause(*_a: Any, **_k: Any) -> dict[str, Any]:
        calls["pause"] += 1
        return {"paused": False}

    def _day(*_a: Any, **_k: Any) -> dict[str, Any]:
        calls["day"] += 1
        return {"paused": False}

    monkeypatch.setattr("jarvise_assist.watch.maybe_auto_resume", _auto)
    monkeypatch.setattr("jarvise_assist.watch.evaluate_pause", _pause)
    monkeypatch.setattr("jarvise_assist.watch.evaluate_day_loss_pause", _day)
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=_ms_bangkok(2026, 10, 27, 14, 0),
        send=lambda *_a, **_k: True,
    )
    assert calls == {"auto": 0, "pause": 0, "day": 0}

    write_settings(conn, enabled=True)
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms_value=_ms_bangkok(2026, 10, 27, 14, 0),
        send=lambda *_a, **_k: True,
    )
    assert calls["auto"] == 1
    assert calls["pause"] == 1
    assert calls["day"] == 1
    conn.close()


def test_execute_mode_off_still_refuses_protective_taps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    conn = open_db(tmp_path / "mode-off.db")
    write_settings(conn, enabled=True)
    assert protective_buttons_enabled(conn) is False
    ts = _ms_bangkok(2026, 11, 14, 12, 0)
    for kind, prefix in (("move_stop", "ms"), ("exit", "xx"), ("move_stop", "mn")):
        intent_id = insert_exec_intent(
            conn,
            kind=kind,
            symbol="DOGEUSDT",
            side="short",
            preview={"stop": "0.079", "qty": "100", "advice_text": "x"},
            created_at_ms=ts,
            expires_at_ms=ts + 120_000,
        )
        answers: list[str | None] = []
        sent: list[str] = []

        def _send(t: str, *, _s: list[str] = sent, **_k: Any) -> bool:
            _s.append(t)
            return True

        def _answer(_cq: str, text: str | None = None, *, _a: list[str | None] = answers, **_k: Any) -> None:
            _a.append(text)

        process_update(
            conn,
            {
                "callback_query": {
                    "id": f"cq-{prefix}",
                    "data": f"{prefix}:{intent_id}",
                    "message": {"chat": {"id": 111}, "message_id": 1, "text": "x"},
                }
            },
            send=_send,
            answer=_answer,
            now_ms_value=ts,
        )
        assert "ปิดอยู่" in answers or MSG_OFF in sent
    conn.close()


def test_off_to_on_keeps_watch_rows_advice_and_intents(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "keep.db")
    day = _ms_bangkok(2026, 10, 27, 14, 0)
    write_settings(conn, enabled=True)
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 100.0,
            "qty": 0.1,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 90.0,
            "initial_stop_source": "signal",
            "entry_time_ms": day - ONE_H_MS,
            "no_stop_latched": 0,
            "last_seen_ms": day,
            "closed_at_ms": None,
        },
    )
    insert_watch_advice(
        conn,
        symbol="BTCUSDT",
        position_side="BOTH",
        kind="move_stop",
        reason="breakeven",
        level=100.2,
        approx_r=0.5,
        status="sent",
        created_at_ms=day,
        sent_at_ms=day,
    )
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="BTCUSDT",
        side="long",
        preview={"stop": "100.2"},
        created_at_ms=day,
        expires_at_ms=day + 600_000,
    )
    assert "ปิด" in toggle_enabled(conn, now_ms_value=day)
    assert ensure_settings(conn)["enabled"] is False
    watch = conn.execute("SELECT COUNT(*) FROM assist_watch_position WHERE closed_at_ms IS NULL").fetchone()[
        0
    ]
    advice = conn.execute("SELECT COUNT(*) FROM assist_watch_advice").fetchone()[0]
    intent = get_exec_intent(conn, intent_id)
    assert watch == 1
    assert advice == 1
    assert intent is not None and intent["status"] == "preview"
    assert "เปิด" in toggle_enabled(conn, now_ms_value=day)
    assert ensure_settings(conn)["enabled"] is True
    assert (
        conn.execute("SELECT COUNT(*) FROM assist_watch_position WHERE closed_at_ms IS NULL").fetchone()[0]
        == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM assist_watch_advice").fetchone()[0] == 1
    assert get_exec_intent(conn, intent_id)["status"] == "preview"
    conn.close()


def test_toggle_off_copy_mentions_watch(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "copy.db")
    write_settings(conn, enabled=True)
    msg = toggle_enabled(conn)
    assert "หยุดส่ง signal ใหม่" in msg
    assert "ยังเฝ้าไม้ที่เปิดอยู่" in msg
    status, _ = render_anything_now(
        conn,
        futures=FakeFutures(positions=[]),
        now_ms=_ms_bangkok(2026, 10, 27, 14, 0),
    )
    assert status == "ผู้ช่วยปิดอยู่ · ยังเฝ้าไม้ที่เปิดอยู่"
    conn.close()
