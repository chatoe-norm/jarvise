"""Owner-confirm assist execution — mock HTTP only, no network."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis

from jarvise_assist.execute import (
    MOVE_STOP_TTL_MS,
    MSG_ALREADY_CLOSED,
    MSG_BINANCE_NOT_SENT,
    MSG_BINANCE_RETRY,
    MSG_EXIT_POSITION_CHANGED,
    MSG_EXPIRED,
    MSG_KEY_REFUSED,
    MSG_KILL,
    MSG_MODE_UNREADABLE,
    MSG_MOVE_STOP_BUFFER,
    MSG_MOVE_STOP_EXPIRED,
    MSG_MOVE_STOP_KEEP,
    MSG_MOVE_STOP_NO_ADVICE,
    MSG_MOVE_STOP_TWO,
    MSG_MOVE_STOP_UNKNOWN_OLD_OK,
    MSG_OFF,
    MSG_ONE_WAY,
    MSG_ORDER_NOT_FOUND,
    MSG_OUTCOME_UNKNOWN,
    MSG_RECOVER_SIDE_MISMATCH,
    MSG_RECOVER_STALE_UNREADABLE,
    MSG_STOP_CLOSED,
    RECOVER_LEASE_MS,
    RECOVER_MAX_AGE_MS,
    BinanceTradeClient,
    TradeClientError,
    _recovery_notify,
    assist_execute_mode,
    build_trade_client,
    cancel_jv_orders_for_symbol,
    client_order_id,
    create_entry_preview,
    handle_exec_callback,
    msg_recover_true_state,
    msg_stop_unverifiable,
    reset_trade_key_cache,
    run_entry_sequence,
    run_exec_recovery,
    run_exit_sequence,
    run_move_stop_sequence,
    size_for_execution,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.listen import process_update
from jarvise_assist.sizing import (
    MAX_OPEN_IDEAS,
    ExistingIdea,
    SignalCandidate,
    SizedSignal,
    admit_signals,
    expected_analysis_bar_ms,
    floor_to_step,
    round_level_to_tick,
    size_signal,
)
from jarvise_assist.store import (
    claim_exec_intent,
    claim_exec_intent_for_recovery,
    finish_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_signal,
    insert_watch_advice,
    now_ms,
    symbol_has_in_flight_exec,
    upsert_watch_position,
)
from jarvise_assist.watch import flush_held_watch_advice, run_assist_watch
from jarvise_exchange.binance_spot import BinanceAuth
from jarvise_ingest.db import SCHEMA_VERSION, open_db, upsert_market_technicals


class RecordingTransport(httpx.BaseTransport):
    """Records signed requests and returns scripted JSON by path+method."""

    def __init__(self, handlers: dict[tuple[str, str], Any]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        params = dict(request.url.params)
        # Drop signature/timestamp from assertions of sequence.
        clean = {k: v for k, v in params.items() if k not in {"signature", "timestamp"}}
        self.calls.append((method, path, clean))
        key = (method, path)
        script = self.handlers.get(key)
        if script is None:
            return httpx.Response(404, json={"code": -1, "msg": f"no handler {method} {path}"})
        if callable(script):
            out = script(clean)
            if isinstance(out, BaseException):
                raise out
            status, body = out
        elif isinstance(script, list):
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            status, body = item
        else:
            status, body = script
        return httpx.Response(status, json=body)


def _auth() -> BinanceAuth:
    # HMAC is enough for signing in unit tests.
    return BinanceAuth(api_key="TRADE_KEY_TEST", hmac_secret="secret")


def _client(
    transport: RecordingTransport, *, base: str = "https://demo-fapi.binance.com"
) -> BinanceTradeClient:
    http = httpx.Client(transport=transport, base_url=base)
    return BinanceTradeClient(_auth(), client=http, base_url=base)


def _verified_stop(
    *,
    client_id: str,
    algo_id: int = 11,
    trigger: str = "0.083",
    qty: str = "100",
    side: str = "BUY",
    symbol: str = "DOGEUSDT",
) -> dict[str, Any]:
    """Open-algo row that passes verify_our_stop field checks."""
    return {
        "symbol": symbol,
        "type": "STOP_MARKET",
        "side": side,
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": trigger,
        "quantity": qty,
        "algoId": algo_id,
        "clientAlgoId": client_id,
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }


def _open_pos(
    *,
    amt: str = "-100",
    entry: str = "0.08",
    mark: str = "0.08",
    symbol: str = "DOGEUSDT",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "positionSide": "BOTH",
        "positionAmt": amt,
        "entryPrice": entry,
        "markPrice": mark,
    }


def _algo_not_found() -> tuple[int, dict[str, Any]]:
    return (400, {"code": -2013, "msg": "Order does not exist."})


def _flat_then_open(open_rows: list[dict[str, Any]], *, flat_count: int = 1) -> Any:
    """positionRisk: empty for baseline, then the open position for later steps."""
    state = {"n": 0}

    def _fn(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        state["n"] += 1
        if state["n"] <= flat_count:
            return (200, [])
        return (200, open_rows)

    return _fn


def _open_then_flat(open_rows: list[dict[str, Any]], *, open_count: int = 1) -> Any:
    """positionRisk: open for pre-exit check, then flat after the exit fill."""
    state = {"n": 0}

    def _fn(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        state["n"] += 1
        if state["n"] <= open_count:
            return (200, open_rows)
        return (200, [])

    return _fn


def _exchange_info() -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": "DOGEUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.00001"},
                ],
            }
        ]
    }


def _seed_doge_short(conn: Any, *, ts: int) -> None:
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.8,
        invalidation=0.083,
        ts=ts,
    )


def test_schema_version_includes_assist_execute(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "mig.db")
    assert SCHEMA_VERSION >= 20
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'assist_exec%'"
        ).fetchall()
    }
    assert "assist_exec_intent" in tables
    assert "assist_exec_log" in tables
    conn.close()


def test_assist_execute_mode_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_ASSIST_EXECUTE", raising=False)
    assert assist_execute_mode() == "off"
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    assert assist_execute_mode() == "live"
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "TESTNET")
    assert assist_execute_mode() == "testnet"


@pytest.mark.parametrize("same", ["key", "pem"])
def test_trade_key_that_reuses_read_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same: str
) -> None:
    pem = tmp_path / "k.pem"
    pem.write_text("not a real key", encoding="utf-8")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "T" * 64)
    monkeypatch.setenv("BINANCE_TRADE_PRIVATE_KEY_PATH", str(pem))
    monkeypatch.setenv("BINANCE_API_KEY", "T" * 64 if same == "key" else "R" * 64)
    monkeypatch.setenv("BINANCE_API_PRIVATE_KEY_PATH", str(pem) if same == "pem" else "/other.pem")
    client, reason = build_trade_client()
    assert client is None
    assert reason == "reuses_read_key"


def test_off_mode_callback_replies_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    conn = open_db(tmp_path / "off.db")
    sent: list[str] = []
    upd = {
        "callback_query": {
            "id": "1",
            "data": "ec:DOGEUSDT:s",
            "message": {"chat": {"id": 111}, "message_id": 9, "text": "x"},
        }
    }
    process_update(conn, upd, send=lambda t, **_: sent.append(t) or True, answer=lambda *_a, **_k: None)
    assert MSG_OFF in sent
    conn.close()


def test_foreign_chat_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    conn = open_db(tmp_path / "foreign.db")
    sent: list[str] = []
    upd = {
        "callback_query": {
            "id": "1",
            "data": "ey:aei_deadbeefdeadbeef",
            "message": {"chat": {"id": 999}, "message_id": 9},
        }
    }
    out = process_update(conn, upd, send=lambda t, **_: sent.append(t) or True, answer=lambda *_a, **_k: None)
    assert out.get("ignored") == "foreign_chat"
    assert sent == []
    conn.close()


def test_entry_happy_path_request_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    reset_trade_key_cache()
    conn = open_db(tmp_path / "entry.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("POST", "/fapi/v1/marginType"): (200, {"code": 200, "msg": "success"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (
                200,
                {
                    "orderId": 1,
                    "executedQty": "100",
                    "avgPrice": "0.08",
                    "status": "FILLED",
                },
            ),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            (200, {"algoId": 11, "clientAlgoId": "jv-x-sl0"}),
            (200, {"algoId": 12, "clientAlgoId": "jv-x-tp"}),
        ],
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    # Build preview via create then claim+run
    preview = create_entry_preview(
        conn, symbol="DOGEUSDT", side="short", trade=trade, now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20)
    )
    assert preview["ok"] is True
    intent_id = preview["intent_id"]
    assert claim_exec_intent(conn, intent_id)
    # Flat until the market fill is sent, then the position is open for the stop/TP re-read.
    filled = {"on": False}

    def position_after_fill(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        if filled["on"]:
            return (200, [_open_pos()])
        return (200, [])

    def market_fill(_clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        filled["on"] = True
        return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    handlers[("GET", "/fapi/v3/positionRisk")] = position_after_fill
    handlers[("POST", "/fapi/v1/order")] = market_fill
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (200, {"algoId": 11}),
        (200, {"algoId": 12}),
    ]
    # Fresh mark for stop check + verified sl0 read-back
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.08"})
    _attach_verified_sl0(handlers, intent_id)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert result.startswith("✅")
    methods_paths = [(m, p) for m, p, _ in transport.calls]
    # After preview GETs, entry must POST margin → leverage → market → stop → tp
    post_paths = [p for m, p in methods_paths if m == "POST"]
    assert post_paths[:5] == [
        "/fapi/v1/marginType",
        "/fapi/v1/leverage",
        "/fapi/v1/order",
        "/fapi/v1/algoOrder",
        "/fapi/v1/algoOrder",
    ]
    # Coverage full via trade client orders
    orders = [
        {
            "symbol": "DOGEUSDT",
            "type": "STOP_MARKET",
            "side": "BUY",
            "positionSide": "BOTH",
            "reduceOnly": True,
            "triggerPrice": "0.083",
            "quantity": "100",
        }
    ]
    from jarvise_assist.futures import protective_stop_coverage

    assert (
        protective_stop_coverage(
            orders,
            symbol="DOGEUSDT",
            position_side="BOTH",
            trade_side="short",
            position_qty=Decimal("100"),
            step_size=Decimal("1"),
        )
        == "full"
    )
    conn.close()


def test_double_confirm_one_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "double.db")
    ts = _ms_bangkok(2026, 11, 2, 12, 0)
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "10",
            "stop": "0.083",
            "target": "0.074",
            "leverage": 5,
            "entry": "0.08",
            "loss_at_stop": "0.03",
            "r_distance": "0.003",
        },
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    assert claim_exec_intent(conn, intent_id)
    assert not claim_exec_intent(conn, intent_id)
    finish_exec_intent(conn, intent_id, status="done", result_text="✅ first")
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        now_ms_value=ts + 1000,
    )
    assert sent == ["✅ first"]
    conn.close()


def test_preview_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "exp.db")
    ts = _ms_bangkok(2026, 11, 2, 12, 0)
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "10", "stop": "0.083", "target": "0.07", "leverage": 5, "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 1000,
    )
    sent: list[str] = []
    edited: list[str] = []
    transport = RecordingTransport({})
    trade = _client(transport)
    handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda _m, t, **_: edited.append(t),
        trade=trade,
        now_ms_value=ts + 200_000,
        message={"message_id": 7},
    )
    assert MSG_EXPIRED in sent
    assert transport.calls == []
    conn.close()


def test_kill_switch_blocks_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "kill.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    with patch("jarvise_assist.execute.kill_switch_state", return_value={"engaged": True, "known": True}):
        out = create_entry_preview(
            conn,
            symbol="DOGEUSDT",
            side="short",
            trade=_client(RecordingTransport({})),
            now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
        )
    assert out["ok"] is False
    assert out["text"] == MSG_KILL
    conn.close()


def test_stop_rejected_twice_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "stopfail.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.083",
            "target": "0.07",
            "leverage": 5,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("GET", "/fapi/v3/positionRisk"): _flat_then_open([_open_pos()]),
        ("POST", "/fapi/v1/marginType"): (200, {"msg": "ok"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08"}),
            (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08"}),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            (400, {"code": -2010, "msg": "reject"}),
            (400, {"code": -2010, "msg": "reject"}),
        ],
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert "ปิดไม้แล้ว" in result
    posts = [p for m, p, _ in transport.calls if m == "POST"]
    assert posts.count("/fapi/v1/order") == 2  # entry + emergency close
    conn.close()


def _seed_move_stop_watch(
    conn: Any,
    *,
    entry_ts: int,
    entry: str = "0.08",
    initial_stop: str = "0.083",
    best_low: float = 0.076,
    side: str = "short",
    symbol: str = "DOGEUSDT",
) -> None:
    """Watch row + closed 1h bar so trail advice lands at best_low + 1R (short)."""
    upsert_watch_position(
        conn,
        {
            "symbol": symbol,
            "position_side": "BOTH",
            "side": side,
            "entry": float(entry),
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": float(initial_stop),
            "initial_stop_source": "signal",
            "entry_time_ms": int(entry_ts),
            "no_stop_latched": 0,
            "last_seen_ms": int(entry_ts),
            "closed_at_ms": None,
        },
    )
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": symbol,
                "timestamp": int(entry_ts),
                "timeframe": "1h",
                "open": float(entry),
                "high": float(entry),
                "low": best_low,
                "close": best_low + 0.001,
                "volume": 1.0,
            }
        ],
    )


def test_cancel_algo_code_200_is_success_errors_still_raise() -> None:
    ok_body = {"algoId": 7, "clientAlgoId": "jv-x-tp", "code": "200", "msg": "success"}
    trade = _client(RecordingTransport({("DELETE", "/fapi/v1/algoOrder"): (200, ok_body)}))
    assert trade.cancel_algo_by_client_id(symbol="DOGEUSDT", client_algo_id="jv-x-tp") == ok_body
    assert trade.cancel_algo(symbol="DOGEUSDT", algo_id=7) == ok_body
    for status, body in (
        (400, {"code": -2011, "msg": "Unknown order sent."}),
        (400, {"code": 200, "msg": "?"}),
    ):
        bad = _client(RecordingTransport({("DELETE", "/fapi/v1/algoOrder"): (status, body)}))
        with pytest.raises(TradeClientError):
            bad.cancel_algo(symbol="DOGEUSDT", algo_id=7)


@pytest.mark.parametrize(
    "delete_body",
    [
        {"algoId": 99},
        # Real Binance success body for Cancel Algo Order (seen live 2026-11-05).
        {"algoId": 99, "clientAlgoId": "jv-old-sl", "code": "200", "msg": "success"},
    ],
)
def test_move_stop_place_before_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_body: dict[str, Any]
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "move.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    # entry 0.08, initial 0.083, R=0.003; low 0.076 → trail 0.079
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    old = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.083",
        "quantity": "100",
        "algoId": 99,
        "clientAlgoId": "jv-old-sl",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    handlers = _move_stop_handlers(
        old=old,
        delete=(200, delete_body),
        ms_client_id=client_order_id(intent_id, "ms"),
    )
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_move_stop_sequence(conn, intent_id, trade=trade, now_ms_value=ts)
    assert result.startswith("✅")
    assert "qty ทั้งไม้" in result
    assert MSG_MOVE_STOP_TWO not in result
    assert "ยกเลิก 0.083 (jv-)" in result
    seq = [(m, p) for m, p, _ in transport.calls if m in {"POST", "DELETE"}]
    assert seq[0] == ("POST", "/fapi/v1/algoOrder")
    assert seq[1] == ("DELETE", "/fapi/v1/algoOrder")
    place = next(c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder")
    assert place[2].get("reduceOnly") == "true"
    assert place[2].get("quantity") == "100"
    assert "closePosition" not in place[2]
    assert place[2].get("triggerPrice") == "0.079"
    conn.close()


def test_move_stop_mark_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "passed.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): (
            200,
            [
                {
                    "symbol": "DOGEUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "-100",
                    "entryPrice": "0.08",
                    "markPrice": "0.09",
                }
            ],
        ),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.09"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    sent: list[str] = []
    markups: list[Any] = []
    edits: list[tuple[Any, ...]] = []

    def _send(t: str, **kw: Any) -> bool:
        sent.append(t)
        if "reply_markup" in kw:
            markups.append(kw["reply_markup"])
        return True

    handle_exec_callback(
        conn,
        f"ms:{intent_id}",
        send=_send,
        answer=lambda *_a, **_k: None,
        edit=lambda *a, **k: edits.append((a, k)),
        trade=trade,
        now_ms_value=ts + 1000,
        message={"message_id": 7},
    )
    assert any(MSG_MOVE_STOP_BUFFER in t for t in sent)
    assert not any("xx:" in json.dumps(m) for m in markups)
    assert not any(c[0] == "POST" for c in transport.calls)
    assert edits  # advice buttons stripped
    conn.close()


def _move_stop_handlers(
    *,
    mark: str = "0.078",
    entry: str = "0.08",
    amt: str = "-100",
    old: dict[str, Any] | None = None,
    place: Any = None,
    delete: Any = None,
    open_algo: Any = None,
    ms_client_id: str | None = None,
    new_stop: str = "0.079",
) -> dict[tuple[str, str], Any]:
    old_order = old or {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.083",
        "quantity": "100",
        "algoId": 99,
        "clientAlgoId": "jv-old-sl",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    if open_algo is None and ms_client_id is not None:
        ms_row = _verified_stop(client_id=ms_client_id, algo_id=100, trigger=new_stop, qty=amt.lstrip("-"))
        n = {"i": 0}

        def _open(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
            n["i"] += 1
            # First list (before place): old only. After place: old + verified -ms.
            if n["i"] == 1:
                return (200, [old_order])
            return (200, [old_order, ms_row])

        open_algo = _open
    elif open_algo is None:
        open_algo = (200, [old_order])
    return {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): (
            200,
            [_open_pos(amt=amt, entry=entry, mark=mark)],
        ),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": mark}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("POST", "/fapi/v1/algoOrder"): place if place is not None else (200, {"algoId": 100}),
        ("DELETE", "/fapi/v1/algoOrder"): delete if delete is not None else (200, {"algoId": 99}),
    }


def test_move_stop_widen_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "widen.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    # Trail wants 0.079 but protective is already 0.078 (tighter) — refuse widen/not tighter.
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    old = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.078",
        "quantity": "100",
        "algoId": 99,
        "clientAlgoId": "jv-old",
    }
    handlers = _move_stop_handlers(ms_client_id=client_order_id(intent_id, "ms"), old=old, mark="0.076")
    transport = RecordingTransport(handlers)
    result = run_move_stop_sequence(conn, intent_id, trade=_client(transport), now_ms_value=ts)
    assert "กระชับ" in result or "ไม่เลื่อน" in result or "แนะนำ" in result
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_move_stop_buffer_zero_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "buf.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    # mark almost at stop → buffer fail
    handlers = _move_stop_handlers(ms_client_id=client_order_id(intent_id, "ms"), mark="0.07895")
    transport = RecordingTransport(handlers)
    result = run_move_stop_sequence(conn, intent_id, trade=_client(transport), now_ms_value=ts)
    assert result == MSG_MOVE_STOP_BUFFER
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_move_stop_cancel_connect_error_two_stop_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "cancel-net.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice body"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    handlers = _move_stop_handlers(
        ms_client_id=client_order_id(intent_id, "ms"), delete=httpx.ConnectError("cancel down")
    )
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    edits: list[str] = []
    out = handle_exec_callback(
        conn,
        f"ms:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda _m, t, **_: edits.append(t),
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 11},
    )
    assert out.get("ok") is True
    assert any(t.startswith("✅") and MSG_MOVE_STOP_TWO in t for t in sent)
    assert "ลองใหม่" not in "".join(sent)
    assert edits == ["advice body"]
    conn.close()


def test_move_stop_place_timeout_lookup_finds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-to.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    cid = client_order_id(intent_id, "ms")
    found = _verified_stop(client_id=cid, algo_id=100, trigger="0.079")
    old = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.083",
        "quantity": "100",
        "algoId": 99,
        "clientAlgoId": "jv-old-sl",
    }
    open_n = {"n": 0}

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        open_n["n"] += 1
        # First list (before place lookup uses find after error): include old.
        # After place timeout, lookup finds new + old; cancel then uses list with both.
        if open_n["n"] == 1:
            return (200, [old])
        return (200, [old, found])

    handlers = _move_stop_handlers(
        ms_client_id=cid,
        place=httpx.ConnectError("place down"),
        open_algo=open_algo,
        old=old,
    )
    transport = RecordingTransport(handlers)
    result = run_move_stop_sequence(conn, intent_id, trade=_client(transport), now_ms_value=ts)
    assert result.startswith("✅")
    posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
    assert len(posts) == 1
    assert any(c[0] == "DELETE" for c in transport.calls)
    conn.close()


def test_move_stop_place_unknown_lookup_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-unk.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    old = _verified_stop(client_id="jv-old-sl", algo_id=99, trigger="0.083")
    handlers = _move_stop_handlers(
        place=httpx.ConnectError("place down"),
        open_algo=(200, [old]),  # lookup never finds new
        old=old,
    )
    transport = RecordingTransport(handlers)
    result = run_move_stop_sequence(conn, intent_id, trade=_client(transport), now_ms_value=ts)
    assert MSG_MOVE_STOP_UNKNOWN_OLD_OK in result
    assert "stop เดิมยังอยู่" in result
    assert not any(c[0] == "DELETE" for c in transport.calls)
    conn.close()


def test_move_stop_close_position_old_coexists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-cp.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    old = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "closePosition": True,
        "triggerPrice": "0.083",
        "algoId": 99,
        "clientAlgoId": "owner-cp",
    }
    handlers = _move_stop_handlers(ms_client_id=client_order_id(intent_id, "ms"), old=old)
    transport = RecordingTransport(handlers)
    result = run_move_stop_sequence(conn, intent_id, trade=_client(transport), now_ms_value=ts)
    assert result.startswith("✅")
    place = next(c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder")
    assert place[2].get("reduceOnly") == "true"
    assert place[2].get("quantity") == "100"
    assert "closePosition" not in place[2]
    assert any(c[0] == "DELETE" for c in transport.calls)
    assert "owner" in result
    conn.close()


def test_move_stop_4130_keeps_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-4130.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    handlers = _move_stop_handlers(
        place=(400, {"code": -4130, "msg": "An open stop or take profit already exists"})
    )
    transport = RecordingTransport(handlers)
    result = run_move_stop_sequence(conn, intent_id, trade=_client(transport), now_ms_value=ts)
    assert MSG_MOVE_STOP_KEEP in result
    assert "-4130" in result
    assert not any(c[0] == "DELETE" for c in transport.calls)
    conn.close()


def test_move_stop_recompute_changed_sends_new_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-recomp.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    # Button level 0.079; fresh candle low 0.074 → trail 0.077 (>1 tick away)
    _seed_move_stop_watch(conn, entry_ts=entry_ts, best_low=0.074)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "old advice"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    handlers = _move_stop_handlers(ms_client_id=client_order_id(intent_id, "ms"), mark="0.075")
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    markups: list[Any] = []

    def _send(t: str, **kw: Any) -> int:
        sent.append(t)
        if "reply_markup" in kw:
            markups.append(kw["reply_markup"])
        return 99

    out = handle_exec_callback(
        conn,
        f"ms:{intent_id}",
        send=_send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert out.get("ok") is False
    assert any("stop แนะนำเปลี่ยนเป็น" in t for t in sent)
    assert any("ms:" in json.dumps(m) and "mn:" in json.dumps(m) for m in markups)
    assert not any(c[0] == "POST" for c in transport.calls)
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "failed"
    conn.close()


def test_move_stop_advice_none_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-none.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    # No favorable extreme → no advice
    _seed_move_stop_watch(conn, entry_ts=entry_ts, best_low=0.0795)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    handlers = _move_stop_handlers(ms_client_id=client_order_id(intent_id, "ms"))
    transport = RecordingTransport(handlers)
    result = run_move_stop_sequence(conn, intent_id, trade=_client(transport), now_ms_value=ts)
    assert result == MSG_MOVE_STOP_NO_ADVICE
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_move_stop_expired_and_mn_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-exp.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice body"},
        created_at_ms=ts,
        expires_at_ms=ts + 1000,
    )
    handlers = _move_stop_handlers(ms_client_id=client_order_id(intent_id, "ms"))
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    edits: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda _m, t, **_: edits.append(t),
        trade=_client(transport),
        now_ms_value=ts + MOVE_STOP_TTL_MS + 1,
        message={"message_id": 3},
    )
    assert sent == [MSG_MOVE_STOP_EXPIRED]
    assert not any(c[0] == "POST" for c in transport.calls)
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    assert row["status"] == "expired"
    assert row["result_text"] == MSG_MOVE_STOP_EXPIRED
    again: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{intent_id}",
        send=lambda t, **_: again.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts + MOVE_STOP_TTL_MS + 1,
        message={"message_id": 3},
    )
    assert again == [MSG_MOVE_STOP_EXPIRED]
    assert not any(c[0] == "POST" for c in transport.calls)

    intent2 = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice2"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    edits.clear()
    handle_exec_callback(
        conn,
        f"mn:{intent2}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda _m, t, **_: edits.append(t),
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 4},
    )
    assert get_exec_intent(conn, intent2)["status"] == "cancelled"
    assert edits == ["advice2"]
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_move_stop_already_closed_and_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-closed.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 5},
    )
    assert MSG_ALREADY_CLOSED in sent
    assert not any(c[0] == "POST" for c in transport.calls)
    again: list[str] = []
    out = handle_exec_callback(
        conn,
        f"ms:{intent_id}",
        send=lambda t, **_: again.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 5},
    )
    assert out.get("replay") is True
    assert again == [MSG_ALREADY_CLOSED]
    conn.close()


def test_move_stop_long_tighten_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ms-long.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    # long: entry 100, initial 90, R=10; high 111 → trail 101 (best_high - 1R)
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
            "entry_time_ms": entry_ts,
            "no_stop_latched": 0,
            "last_seen_ms": entry_ts,
            "closed_at_ms": None,
        },
    )
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": entry_ts,
                "timeframe": "1h",
                "open": 100.0,
                "high": 111.0,
                "low": 99.0,
                "close": 108.0,
                "volume": 1.0,
            }
        ],
    )
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="BTCUSDT",
        side="long",
        preview={"stop": "101", "qty": "0.1"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    claim_exec_intent(conn, intent_id)
    old = _verified_stop(
        client_id="jv-btc",
        algo_id=1,
        trigger="90",
        qty="0.1",
        side="SELL",
        symbol="BTCUSDT",
    )
    ms_cid = client_order_id(intent_id, "ms")
    ms_row = _verified_stop(
        client_id=ms_cid,
        algo_id=2,
        trigger="101",
        qty="0.1",
        side="SELL",
        symbol="BTCUSDT",
    )
    open_n = {"n": 0}

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        open_n["n"] += 1
        if open_n["n"] == 1:
            return (200, [old])
        return (200, [old, ms_row])

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): (
            200,
            [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "0.1",
                    "entryPrice": "100",
                    "markPrice": "108",
                }
            ],
        ),
        ("GET", "/fapi/v1/exchangeInfo"): (
            200,
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                        ],
                    }
                ]
            },
        ),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "108"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("POST", "/fapi/v1/algoOrder"): (200, {"algoId": 2}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 1}),
    }
    transport = RecordingTransport(handlers)
    result = run_move_stop_sequence(conn, intent_id, trade=_client(transport), now_ms_value=ts)
    assert result.startswith("✅"), result
    place = next(c for c in transport.calls if c[0] == "POST")
    assert Decimal(str(place[2]["triggerPrice"])) == Decimal("101")
    conn.close()


def test_exit_and_already_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "exit.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): (200, []),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    assert run_exit_sequence(conn, intent_id, trade=trade) == MSG_ALREADY_CLOSED
    assert not any(c[0] == "POST" for c in transport.calls)

    # Happy exit
    intent2 = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent2)
    handlers[("GET", "/fapi/v3/positionRisk")] = _open_then_flat(
        [_open_pos(amt="-100", entry="0.08", mark="0.07")]
    )
    handlers[("POST", "/fapi/v1/order")] = (200, {"orderId": 9, "avgPrice": "0.07", "executedQty": "100"})
    handlers[("GET", "/fapi/v1/userTrades")] = (200, [{"realizedPnl": "1.5"}])
    handlers[("GET", "/fapi/v1/openOrders")] = (200, [])
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (
        200,
        [
            {
                "symbol": "DOGEUSDT",
                "type": "STOP_MARKET",
                "side": "BUY",
                "positionSide": "BOTH",
                "reduceOnly": True,
                "triggerPrice": "0.083",
                "algoId": 55,
                "clientAlgoId": "jv-abc-sl0",
            }
        ],
    )
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (200, {"algoId": 55})
    transport2 = RecordingTransport(handlers)
    trade2 = _client(transport2)
    result = run_exit_sequence(conn, intent2, trade=trade2)
    assert result.startswith("✅")
    assert "1.5" in result
    assert ("DELETE", "/fapi/v1/algoOrder") in [(m, p) for m, p, _ in transport2.calls]
    conn.close()


def test_trade_key_withdrawals_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    reset_trade_key_cache()
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/sapi/v1/account/apiRestrictions"): (
            200,
            {"enableFutures": True, "enableWithdrawals": True, "ipRestrict": True},
        ),
    }
    transport = RecordingTransport(handlers)
    trade = BinanceTradeClient(
        _auth(),
        client=httpx.Client(transport=transport),
        base_url="https://fapi.binance.com",
        sapi_base="https://api.binance.com",
    )
    with pytest.raises(Exception) as exc:
        trade.assert_trade_key_safe(mode="live")
    assert "enableWithdrawals" in str(exc.value)


def test_read_key_never_used_for_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_API_KEY", "READ_KEY")
    monkeypatch.setenv("BINANCE_API_SECRET", "readsecret")
    monkeypatch.delenv("BINANCE_TRADE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TRADE_PRIVATE_KEY_PATH", raising=False)
    client, reason = build_trade_client()
    assert client is None
    assert reason == "no_key"


def test_orphan_cancels_only_jv(tmp_path: Path) -> None:
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/openAlgoOrders"): (
            200,
            [
                {"algoId": 1, "clientAlgoId": "jv-abc-sl0", "symbol": "DOGEUSDT"},
                {"algoId": 2, "clientAlgoId": "manual-stop", "symbol": "DOGEUSDT"},
            ],
        ),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 1}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    cancelled, ok = cancel_jv_orders_for_symbol(trade, "DOGEUSDT")
    assert cancelled == ["1"]
    assert ok is True
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0][2].get("algoId") == "1"


def test_client_order_id_prefix() -> None:
    cid = client_order_id("aei_abcdef0123456789", "sl0")
    assert cid.startswith("jv-")
    assert len(cid) <= 36


def _entry_intent(conn: Any, *, ts: int | None = None, qty: str = "100") -> str:
    t = int(ts if ts is not None else now_ms())
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": qty,
            "stop": "0.083",
            "target": "0.07",
            "leverage": 5,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=t,
        expires_at_ms=t + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    return intent_id


def test_market_timeout_then_filled_continues_to_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "mkt_to.db")
    intent_id = _entry_intent(conn)
    cid = client_order_id(intent_id, "mkt")
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("POST", "/fapi/v1/marginType"): (200, {"msg": "ok"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [httpx.TimeoutException("timeout")],
        ("GET", "/fapi/v1/order"): (
            200,
            {
                "orderId": 1,
                "clientOrderId": cid,
                "executedQty": "100",
                "avgPrice": "0.08",
                "status": "FILLED",
            },
        ),
        ("POST", "/fapi/v1/algoOrder"): [
            (200, {"algoId": 11}),
            (200, {"algoId": 12}),
        ],
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("GET", "/fapi/v3/positionRisk"): _flat_then_open([_open_pos()]),
    }
    _attach_verified_sl0(handlers, intent_id)
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert result.startswith("✅")
    assert ("GET", "/fapi/v1/order") in [(m, p) for m, p, _ in transport.calls]
    posts = [p for m, p, _ in transport.calls if m == "POST"]
    assert posts.count("/fapi/v1/algoOrder") == 2
    conn.close()


def test_market_timeout_order_not_found_skips_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "mkt_nf.db")
    intent_id = _entry_intent(conn)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("POST", "/fapi/v1/marginType"): (200, {"msg": "ok"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [httpx.TimeoutException("timeout")],
        ("GET", "/fapi/v1/order"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert result == MSG_OUTCOME_UNKNOWN
    assert getattr(result, "status", None) == "unknown"
    assert not any(p == "/fapi/v1/algoOrder" for m, p, _ in transport.calls)
    conn.close()


def test_stop_timeout_with_algo_present_no_second_stop_or_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl_to.db")
    intent_id = _entry_intent(conn)
    scid = client_order_id(intent_id, "sl0")
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): _flat_then_open([_open_pos()]),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("POST", "/fapi/v1/marginType"): (200, {"msg": "ok"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08"}),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            httpx.TimeoutException("timeout"),
            (200, {"algoId": 12}),  # TP only
        ],
        ("GET", "/fapi/v1/openAlgoOrders"): (
            200,
            [_verified_stop(client_id=scid, algo_id=11)],
        ),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert result.startswith("✅")
    # One market fill + no emergency close; stop POST once (timed out) + TP.
    post_orders = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(post_orders) == 1
    algo_posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
    assert len(algo_posts) == 2  # timed-out stop attempt + TP
    conn.close()


def test_stop_timeout_and_lookup_timeout_retries_instead_of_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Network down during the stop step must not escape with a filled, unprotected position.
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl_lookup_to.db")
    intent_id = _entry_intent(conn)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): _flat_then_open([_open_pos()]),
        ("POST", "/fapi/v1/marginType"): (200, {"msg": "ok"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08"}),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            httpx.TimeoutException("timeout"),
            (200, {"algoId": 11}),  # stop retry
            (200, {"algoId": 12}),  # TP
        ],
        ("GET", "/fapi/v1/openAlgoOrders"): [httpx.ConnectError("down")],
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    sl0 = client_order_id(intent_id, "sl0")
    sl1 = client_order_id(intent_id, "sl1")
    # sl1's scripted POST is a success body; its read-back 404s, so stay unknown.
    assert result == msg_stop_unverifiable(symbol="DOGEUSDT", qty=Decimal("100"), stop=Decimal("0.083"))
    assert getattr(result, "status", None) == "unknown"
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [c[2].get("clientAlgoId") for c in stop_posts] == [sl0, sl1]
    assert not any(
        c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
        for c in transport.calls
    )
    conn.close()


def test_stop_fails_close_fails_close_yourself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl_cls.db")
    intent_id = _entry_intent(conn)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 9, "executedQty": "0", "status": "NEW"}),
        ("POST", "/fapi/v1/marginType"): (200, {"msg": "ok"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08"}),
            (400, {"code": -2010, "msg": "close reject"}),
            (400, {"code": -2010, "msg": "close reject"}),
            (400, {"code": -2010, "msg": "close reject"}),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            (400, {"code": -2010, "msg": "reject"}),
            (400, {"code": -2010, "msg": "reject"}),
        ],
        ("GET", "/fapi/v3/positionRisk"): _flat_then_open([_open_pos()]),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith("🚨")
    assert "DOGEUSDT" in result
    assert "SHORT" in result
    assert "เข้า Binance" in result
    assert "qty 100" in result
    assert "stop 0.083" in result
    assert "ลองใหม่" not in result
    assert not result.startswith("✅")
    assert MSG_STOP_CLOSED not in result
    conn.close()


def test_guard_exception_leaves_intent_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "guard.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.083",
            "target": "0.07",
            "leverage": 5,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    sent: list[str] = []

    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("guard blew up")

    monkeypatch.setattr("jarvise_assist.execute._confirm_guards_entry", boom)
    trade = _client(
        RecordingTransport(
            {
                ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
                ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
            }
        )
    )
    handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1000,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    assert row["status"] == "failed"
    assert any(MSG_BINANCE_NOT_SENT in t for t in sent)
    conn.close()


def test_testnet_entry_does_not_write_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "tn_watch.db")
    intent_id = _entry_intent(conn)
    # Attach a signal id so live would mark taken / write watch.
    conn.execute(
        "UPDATE assist_exec_intent SET preview_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "qty": "100",
                    "stop": "0.083",
                    "target": "0.07",
                    "leverage": 5,
                    "entry": "0.08",
                    "loss_at_stop": "0.3",
                    "r_distance": "0.003",
                    "signal_id": "sig-test",
                }
            ),
            intent_id,
        ),
    )
    conn.commit()
    handlers = _base_entry_handlers(intent_id=intent_id)
    trade = _client(RecordingTransport(handlers))
    assert run_entry_sequence(conn, intent_id, trade=trade).startswith("✅")
    n = conn.execute("SELECT COUNT(*) FROM assist_watch_position").fetchone()[0]
    assert n == 0
    conn.close()


def test_testnet_watch_advice_no_buttons_no_trade_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "tn_btn.db")
    day = _ms_bangkok(2026, 11, 2, 12, 0)
    # Seed ATR so watch can freeze an initial stop, then force 2R exit via mark.
    rows = []
    for i in range(30):
        ts = day - (30 - i) * 4 * 3_600_000
        rows.append(
            {
                "timestamp": ts,
                "symbol": "DOGEUSDT",
                "timeframe": "4h",
                "open": 0.08,
                "high": 0.081,
                "low": 0.079,
                "close": 0.08,
                "volume": 1.0,
            }
        )
    upsert_market_technicals(conn, rows)
    for row in rows:
        conn.execute(
            "UPDATE market_technicals SET atr_14=? WHERE symbol=? AND timeframe=? AND timestamp=?",
            (0.002, "DOGEUSDT", "4h", row["timestamp"]),
        )
    conn.commit()
    # initial ATR stop ≈ entry - 1.5*0.002 for long → risk 0.003; 2R at entry+0.006
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.086"),
        update_time_ms=day,
    )
    stop_order = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "SELL",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.077",
        "quantity": "100",
        "algoId": 9,
    }
    fake = FakeFutures(positions=[pos], orders=[], algo_orders=[stop_order])
    trade_calls: list[str] = []

    def no_trade() -> Any:
        trade_calls.append("build")
        return None, "no_key"

    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", no_trade)
    sent: list[dict[str, Any]] = []

    def capture(text: str, **kwargs: Any) -> bool:
        sent.append({"text": text, "kwargs": kwargs})
        return True

    out = run_assist_watch(conn, futures=fake, now_ms_value=day, send=capture)
    assert out.get("ok") is True
    assert trade_calls == []
    intents = conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0]
    assert intents == 0
    assert sent, "expected at least one advice send"
    assert all(s["kwargs"].get("reply_markup") is None for s in sent)
    conn.close()


def test_risk_cap_uses_open_position_real_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "risk.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    # ETH long with a wide protective stop → 50 USDT risk on 1000 balance (budget 40).
    eth_pos = {
        "symbol": "ETHUSDT",
        "positionSide": "BOTH",
        "positionAmt": "1",
        "entryPrice": "2000",
        "markPrice": "2000",
    }
    eth_stop = {
        "symbol": "ETHUSDT",
        "type": "STOP_MARKET",
        "side": "SELL",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "1950",
        "quantity": "1",
        "algoId": 77,
    }
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, [eth_pos]),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [eth_stop]),
    }
    trade = _client(RecordingTransport(handlers))
    sized, reason = size_for_execution(
        conn,
        trade=trade,
        symbol="DOGEUSDT",
        side="short",
        now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
    )
    assert sized is None
    assert reason == "risk_cap"
    conn.close()


def test_deduped_watch_poll_inserts_no_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "dedupe.db")
    day = _ms_bangkok(2026, 11, 2, 12, 0)
    entry_ms = day - 3_600_000
    rows = []
    for i in range(30):
        ts = day - (30 - i) * 4 * 3_600_000
        rows.append(
            {
                "timestamp": ts,
                "symbol": "DOGEUSDT",
                "timeframe": "4h",
                "open": 0.08,
                "high": 0.081,
                "low": 0.079,
                "close": 0.08,
                "volume": 1.0,
            }
        )
    upsert_market_technicals(conn, rows)
    for row in rows:
        conn.execute(
            "UPDATE market_technicals SET atr_14=? WHERE symbol=? AND timeframe=? AND timestamp=?",
            (0.002, "DOGEUSDT", "4h", row["timestamp"]),
        )
    conn.commit()
    # Prior exit advice already sent for this position window (after entry_ms).
    insert_watch_advice(
        conn,
        symbol="DOGEUSDT",
        position_side="BOTH",
        kind="exit",
        reason="target_2r",
        level=None,
        approx_r=2.0,
        status="sent",
        created_at_ms=day - 60_000,
        sent_at_ms=day - 60_000,
    )
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.086"),
        update_time_ms=entry_ms,
    )
    stop_order = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "SELL",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.077",
        "quantity": "100",
        "algoId": 9,
    }
    fake = FakeFutures(positions=[pos], orders=[], algo_orders=[stop_order])
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t, **_: True)
    n = conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0]
    assert n == 0
    conn.close()


def test_watch_default_send_keeps_exit_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The job calls run_assist_watch without send=; the exit button must still reach Telegram."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "default_send.db")
    day = _ms_bangkok(2026, 11, 2, 12, 0)
    rows = [
        {
            "timestamp": day - (30 - i) * 4 * 3_600_000,
            "symbol": "DOGEUSDT",
            "timeframe": "4h",
            "open": 0.08,
            "high": 0.081,
            "low": 0.079,
            "close": 0.08,
            "volume": 1.0,
        }
        for i in range(30)
    ]
    upsert_market_technicals(conn, rows)
    conn.execute("UPDATE market_technicals SET atr_14=0.002 WHERE symbol='DOGEUSDT' AND timeframe='4h'")
    conn.commit()
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.086"),
        update_time_ms=day - 3_600_000,
    )
    stop_order = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "SELL",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.077",
        "quantity": "100",
        "algoId": 9,
    }
    fake = FakeFutures(positions=[pos], orders=[], algo_orders=[stop_order])
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (None, "no_key"))
    sent: list[tuple[str, Any]] = []

    def fake_send_message(text: str, *, reply_markup: Any = None, **_: Any) -> int:
        sent.append((text, reply_markup))
        return 1

    monkeypatch.setattr("jarvise_assist.watch.send_message", fake_send_message)
    run_assist_watch(conn, futures=fake, now_ms_value=day)
    exits = [(t, m) for t, m in sent if "ออกตอนนี้" in t]
    assert exits, sent
    assert "xx:" in json.dumps(exits[0][1])
    conn.close()


def test_round_level_to_tick_directional() -> None:
    from jarvise_assist.sizing import round_level_to_tick

    tick = Decimal("0.001")
    price = Decimal("0.083456")
    assert round_level_to_tick(price, tick, side="long", kind="stop") == Decimal("0.083")
    assert round_level_to_tick(price, tick, side="short", kind="stop") == Decimal("0.084")
    assert round_level_to_tick(price, tick, side="long", kind="target") == Decimal("0.084")
    assert round_level_to_tick(price, tick, side="short", kind="target") == Decimal("0.083")
    # Already on a tick: ceil leaves it.
    assert round_level_to_tick(Decimal("0.084"), tick, side="short", kind="stop") == Decimal("0.084")
    assert round_level_to_tick(price, Decimal("0"), side="long", kind="stop") == price


def test_size_for_execution_qty_from_rounded_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "round_size.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.8,
        invalidation=0.083456,
        ts=bar,
    )
    info = {
        "symbols": [
            {
                "symbol": "DOGEUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                ],
            }
        ]
    }
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, info),
    }
    trade = _client(RecordingTransport(handlers))
    sized, reason = size_for_execution(
        conn,
        trade=trade,
        symbol="DOGEUSDT",
        side="short",
        now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
    )
    assert reason is None and sized is not None
    # Short stop 0.083456 ceils to 0.084; qty/loss from R=0.004.
    assert sized.stop == Decimal("0.084")
    assert sized.r_distance == Decimal("0.004")
    assert sized.loss_at_stop == sized.qty * sized.r_distance
    # Target floors (2R from rounded stop via size_signal, then directional round).
    assert sized.target == Decimal("0.072")
    conn.close()


def test_size_for_execution_requote_cap_and_chase_vs_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.sizing import size_signal
    from jarvise_assist.store import insert_signal

    # Live: demo/mainnet marks match; chase vs seen must still refuse.
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "requote.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    from dataclasses import replace

    # Cap below a fresh 1%-of-balance size but keep notional above exchange min.
    first = replace(first, qty=Decimal("100"), notional=Decimal("8"), loss_at_stop=Decimal("0.3"))
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    trade = _client(RecordingTransport(handlers), base="https://fapi.binance.com")
    sized, reason = size_for_execution(conn, trade=trade, symbol="DOGEUSDT", side="short", now_ms_value=now)
    assert reason is None and sized is not None
    assert sized.qty == Decimal("100")

    # Chase vs seen: move mark through 0.3R of the seen entry/stop.
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.078"})
    chased, chase_reason = size_for_execution(
        conn, trade=trade, symbol="DOGEUSDT", side="short", now_ms_value=now
    )
    assert chased is None
    assert chase_reason == "chase"
    conn.close()


def test_size_for_execution_risk_cap_uses_requote_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loss-capped re-quote can exceed the portfolio budget that the old qty cap fit under."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "requote_risk.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    first_qty = Decimal("100")
    first_loss = Decimal("3")
    first = replace(first, qty=first_qty, notional=first_qty * first.entry, loss_at_stop=first_loss)
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    # ETH long risks 38 USDT. Portfolio budget is 4% of 1000 = 40.
    # Qty cap would keep 100 * R(0.003) = 0.3 and fit. Loss cap is 3 and does not.
    eth_pos = {
        "symbol": "ETHUSDT",
        "positionSide": "BOTH",
        "positionAmt": "1",
        "entryPrice": "2000",
        "markPrice": "2000",
    }
    eth_stop = {
        "symbol": "ETHUSDT",
        "type": "STOP_MARKET",
        "side": "SELL",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "1962",
        "quantity": "1",
        "algoId": 77,
    }
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, [eth_pos]),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [eth_stop]),
    }
    trade = _client(RecordingTransport(handlers))
    sized, reason = size_for_execution(conn, trade=trade, symbol="DOGEUSDT", side="short", now_ms_value=now)
    assert sized is None
    assert reason == "risk_cap"
    conn.close()


def test_size_for_execution_requote_cap_zero_sends_no_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "cap_zero.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    first = replace(first, qty=Decimal("10"), notional=Decimal("0.8"), loss_at_stop=Decimal("0.001"))
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    sized, reason = size_for_execution(conn, trade=trade, symbol="DOGEUSDT", side="short", now_ms_value=now)
    assert sized is None
    assert reason == "requote_cap_zero"

    sent: list[str] = []
    out = handle_exec_callback(
        conn,
        "ec:DOGEUSDT:s",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
    )
    assert out.get("ok") is False
    assert out.get("reason") == "requote_cap_zero"
    assert any("จำกัดตามไอเดียแรกแล้วขนาดเหลือ 0 — ไม่ส่ง" in t for t in sent)
    assert [c for c in transport.calls if c[0] == "POST"] == []
    conn.close()


def test_size_for_execution_requote_under_min_notional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "cap_min.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    first = replace(first, qty=Decimal("100"), notional=Decimal("8"), loss_at_stop=Decimal("0.003"))
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    trade = _client(RecordingTransport(handlers))
    sized, reason = size_for_execution(conn, trade=trade, symbol="DOGEUSDT", side="short", now_ms_value=now)
    assert sized is None
    assert reason == "below_min_notional"
    conn.close()


def test_size_for_execution_short_stop_round_keeps_requote_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "round_cap.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.8,
        invalidation=0.0831,
        ts=bar,
    )
    first_loss = Decimal("1")
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    first = replace(first, qty=Decimal("200"), notional=Decimal("16"), loss_at_stop=first_loss)
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    info = {
        "symbols": [
            {
                "symbol": "DOGEUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                ],
            }
        ]
    }
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, info),
    }
    trade = _client(RecordingTransport(handlers))
    sized, reason = size_for_execution(conn, trade=trade, symbol="DOGEUSDT", side="short", now_ms_value=now)
    assert reason is None and sized is not None
    assert sized.stop == Decimal("0.084")
    r_rounded = abs(sized.entry - sized.stop)
    assert r_rounded == Decimal("0.004")
    fresh = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.084"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert fresh is not None
    assert sized.loss_at_stop == sized.qty * r_rounded
    assert sized.loss_at_stop <= first_loss
    assert sized.qty <= floor_to_step(first_loss / r_rounded, Decimal("1"))
    assert sized.qty <= fresh.qty
    conn.close()


def test_size_for_execution_returns_margin_shrunk_qty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "margin_qty.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "10000", "availableBalance": "20"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    sized, reason = size_for_execution(conn, trade=trade, symbol="DOGEUSDT", side="short", now_ms_value=now)
    assert reason is None and sized is not None
    assert sized.qty == Decimal("1250")
    assert sized.notional == Decimal("100")
    assert sized.loss_at_stop == Decimal("1250") * Decimal("0.003")
    assert sized.leverage == 5
    preview = create_entry_preview(conn, symbol="DOGEUSDT", side="short", trade=trade, now_ms_value=now)
    assert preview["ok"] is True
    assert "ขนาด: 1250" in str(preview["text"])
    assert preview["sized"].qty == Decimal("1250")
    conn.close()


def test_size_for_execution_margin_shrink_under_min_notional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "margin_min.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    # available 0.1 → max notional 0.5 → qty floor 6 → notional 0.48 < min 5.
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "10000", "availableBalance": "0.1"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    trade = _client(RecordingTransport(handlers))
    sized, reason = size_for_execution(conn, trade=trade, symbol="DOGEUSDT", side="short", now_ms_value=now)
    assert sized is None
    assert reason == "below_min_notional"
    conn.close()


def test_preview_stores_seen_and_confirm_chases_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_entry
    from jarvise_assist.sizing import size_signal
    from jarvise_assist.store import insert_signal

    # Preview can be built in testnet; confirm seen-chase assertion is live-only.
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "seen_preview.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    trade = _client(RecordingTransport(handlers))
    preview = create_entry_preview(conn, symbol="DOGEUSDT", side="short", trade=trade, now_ms_value=now)
    assert preview["ok"] is True
    assert "ยืนยันภายใน 2 นาที" in preview["text"]
    # Expiry line includes seconds for the 2-minute confirm window.
    assert "หมดอายุ" in preview["text"]
    intent = get_exec_intent(conn, preview["intent_id"])
    assert intent is not None
    assert intent["preview"]["seen_entry"] == "0.08"
    assert intent["preview"]["seen_stop"] == "0.083"

    # Live confirm: mark chases seen but not a looser preview entry/stop.
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    intent["preview"] = {
        **intent["preview"],
        "entry": "0.078",
        "stop": "0.081",
        "seen_entry": "0.08",
        "seen_stop": "0.083",
    }
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.0785"})
    guard = _confirm_guards_entry(conn, intent, trade, now_ms_value=now + 1_000)
    assert guard == "chase"
    conn.close()


def test_testnet_seen_chase_uses_read_mark_not_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_entry

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "testnet_chase.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_doge_short(conn, ts=bar)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.08"),
        stop=Decimal("0.083"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    # Loss stays above the unmapped demo minimum: mark 0.078, stop 0.083, R=0.005
    # needs qty >= 65 so notional clears 5. 0.5 USDT does; 0.3 floors under it.
    first = replace(first, qty=Decimal("100"), notional=Decimal("8"), loss_at_stop=Decimal("0.5"))
    insert_signal(
        conn,
        first,
        status="sent",
        created_at_ms=now - 60_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 60_000,
    )
    # Demo mark far below mainnet seen; comparing demo to seen would false-chase.
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.06"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    trade = _client(RecordingTransport(handlers))
    sized, reason = size_for_execution(
        conn,
        trade=trade,
        symbol="DOGEUSDT",
        side="short",
        now_ms_value=now,
        read_mark=Decimal("0.08"),
    )
    assert reason is None and sized is not None
    # Demo price is lower, so the same percent stop is a smaller dollar R.
    # Qty grows; loss stays at or under the first sent loss, not the first qty.
    expected = floor_to_step(first.loss_at_stop / sized.r_distance, Decimal("1"))
    assert sized.qty == expected
    assert sized.qty > first.qty
    assert sized.loss_at_stop <= first.loss_at_stop

    # Missing read_mark: skip seen chase even when demo mark would chase seen.
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.078"})
    no_read, no_read_reason = size_for_execution(
        conn,
        trade=trade,
        symbol="DOGEUSDT",
        side="short",
        now_ms_value=now,
        read_mark=None,
    )
    assert no_read_reason is None and no_read is not None

    # Confirm in testnet skips seen-entry chase; preview entry/stop still guarded.
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.081",
            "target": "0.072",
            "leverage": 1,
            "entry": "0.078",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
            "seen_entry": "0.08",
            "seen_stop": "0.083",
        },
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    intent = get_exec_intent(conn, intent_id)
    assert intent is not None
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.0785"})
    guard = _confirm_guards_entry(conn, intent, trade, now_ms_value=now + 1_000)
    assert guard != "chase"
    conn.close()


def test_trade_auth_reasons_and_ec_pem_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import (
        MSG_NO_TRADE_KEY,
        MSG_TRADE_FILE_MISSING,
        MSG_TRADE_PEM_FAILED,
        MSG_TRADE_REUSES_READ,
        resolve_trade_auth,
        trade_auth_reason_th,
    )

    monkeypatch.delenv("BINANCE_TRADE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TRADE_PRIVATE_KEY_PATH", raising=False)
    assert resolve_trade_auth() == (None, "no_key")
    assert trade_auth_reason_th("no_key") == MSG_NO_TRADE_KEY

    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "T" * 64)
    monkeypatch.delenv("BINANCE_TRADE_PRIVATE_KEY_PATH", raising=False)
    assert resolve_trade_auth() == (None, "no_path")

    monkeypatch.setenv("BINANCE_TRADE_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("BINANCE_API_KEY", "R" * 64)
    monkeypatch.setenv("BINANCE_API_PRIVATE_KEY_PATH", "/other.pem")
    assert resolve_trade_auth() == (None, "file_missing")
    assert trade_auth_reason_th("file_missing") == MSG_TRADE_FILE_MISSING

    pem = tmp_path / "bad.pem"
    pem.write_text("not a real key", encoding="utf-8")
    monkeypatch.setenv("BINANCE_TRADE_PRIVATE_KEY_PATH", str(pem))
    assert resolve_trade_auth() == (None, "pem_load_failed")
    assert trade_auth_reason_th("pem_load_failed") == MSG_TRADE_PEM_FAILED
    assert trade_auth_reason_th("reuses_read_key") == MSG_TRADE_REUSES_READ

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "pem_ec.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        "ec:DOGEUSDT:s",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
    )
    assert MSG_TRADE_PEM_FAILED in sent
    conn.close()


def test_long_preview_expiry_and_directional_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "long_prev.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="long",
        confidence=0.8,
        invalidation=0.076456,
        ts=bar,
    )
    info = {
        "symbols": [
            {
                "symbol": "DOGEUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                ],
            }
        ]
    }
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, info),
    }
    trade = _client(RecordingTransport(handlers))
    preview = create_entry_preview(
        conn,
        symbol="DOGEUSDT",
        side="long",
        trade=trade,
        now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
    )
    assert preview["ok"] is True
    assert "ยืนยันภายใน 2 นาที" in preview["text"]
    sized = preview["sized"]
    assert sized.stop == Decimal("0.076")  # long stop floors
    assert "Stop: 0.076" in preview["text"]
    conn.close()


def test_entry_zero_avg_price_uses_order_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "avg_query.db")
    ts = now_ms()
    intent_id = _entry_intent(conn, ts=ts, qty="759")
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v3/positionRisk")] = _flat_then_open(
        [_open_pos(amt="-759", entry="0.07526", mark="0.075")]
    )
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (
        200,
        [_verified_stop(client_id=client_order_id(intent_id, "sl0"), qty="759")],
    )
    handlers[("POST", "/fapi/v1/order")] = (
        200,
        {"orderId": 1, "executedQty": "759", "avgPrice": "0.000000", "status": "FILLED"},
    )
    handlers[("GET", "/fapi/v1/order")] = (
        200,
        {"orderId": 1, "executedQty": "759", "avgPrice": "0.07526", "status": "FILLED"},
    )
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.075"})
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert result.startswith("✅")
    assert "fill 0.07526 × 759" in result
    assert "ถ้าชน stop" in result
    assert "ราคายังไม่ยืนยัน" not in result
    methods_paths = [(m, p) for m, p, _ in transport.calls]
    # Price query must run after market → stop → TP (not before protective stops).
    post_order_i = methods_paths.index(("POST", "/fapi/v1/order"))
    algo_indices = [i for i, mp in enumerate(methods_paths) if mp == ("POST", "/fapi/v1/algoOrder")]
    get_order_i = methods_paths.index(("GET", "/fapi/v1/order"))
    assert len(algo_indices) >= 2
    assert post_order_i < algo_indices[0] < algo_indices[1] < get_order_i
    conn.close()


def test_entry_zero_avg_price_falls_back_to_position_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "avg_pos.db")
    ts = now_ms()
    intent_id = _entry_intent(conn, ts=ts, qty="759")
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v3/positionRisk")] = _flat_then_open(
        [_open_pos(amt="-759", entry="0.0753", mark="0.075")]
    )
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (
        200,
        [_verified_stop(client_id=client_order_id(intent_id, "sl0"), qty="759")],
    )
    handlers[("POST", "/fapi/v1/order")] = (
        200,
        {"orderId": 1, "executedQty": "759", "avgPrice": "0.000000", "status": "FILLED"},
    )
    handlers[("GET", "/fapi/v1/order")] = (
        200,
        {"orderId": 1, "executedQty": "759", "avgPrice": "0.000000", "status": "FILLED"},
    )
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.075"})
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert "fill 0.0753 × 759" in result
    assert "ถ้าชน stop" in result
    conn.close()


def test_entry_unknown_price_skips_watch_and_loss_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "avg_unknown.db")
    ts = now_ms()
    intent_id = _entry_intent(conn, ts=ts, qty="759")
    handlers = _base_entry_handlers(intent_id=intent_id)
    # Open with entry 0 so the fill price stays unknown, but the position is not flat.
    handlers[("GET", "/fapi/v3/positionRisk")] = _flat_then_open(
        [_open_pos(amt="-759", entry="0", mark="0.075")]
    )
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (
        200,
        [_verified_stop(client_id=client_order_id(intent_id, "sl0"), qty="759")],
    )
    handlers[("POST", "/fapi/v1/order")] = (
        200,
        {"orderId": 1, "executedQty": "759", "avgPrice": "0.000000", "status": "FILLED"},
    )
    handlers[("GET", "/fapi/v1/order")] = (
        200,
        {"orderId": 1, "executedQty": "759", "avgPrice": "0", "status": "FILLED"},
    )
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.075"})
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert "ราคายังไม่ยืนยัน × 759" in result
    assert "ถ้าชน stop" not in result
    n = conn.execute("SELECT COUNT(*) FROM assist_watch_position").fetchone()[0]
    assert n == 0
    conn.close()


def test_exit_zero_avg_price_uses_query_or_omits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "exit_avg.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([_open_pos(amt="-100", entry="0.08", mark="0.07")]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.000000", "executedQty": "100"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.071", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.9"}]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    trade = _client(RecordingTransport(handlers))
    result = run_exit_sequence(conn, intent_id, trade=trade)
    assert "@ 0.071" in result
    assert "@ 0 " not in result and "@ 0.000000" not in result

    intent2 = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent2)
    handlers2: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([_open_pos(amt="-100", entry="0.08", mark="0.07")]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 10, "avgPrice": "0.000000", "executedQty": "100"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 10, "avgPrice": "0.000000", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.5"}]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    result2 = run_exit_sequence(conn, intent2, trade=_client(RecordingTransport(handlers2)))
    assert "@ 0" not in result2
    assert result2.startswith("✅ ปิด DOGEUSDT · PnL")
    conn.close()


def test_size_for_execution_split_reasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _reason_th

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "size_reasons.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    now = _ms_bangkok(2026, 11, 2, 8, 20)

    zero_handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "0", "availableBalance": "0"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    sized, reason = size_for_execution(
        conn,
        trade=_client(RecordingTransport(zero_handlers)),
        symbol="DOGEUSDT",
        side="short",
        now_ms_value=now,
    )
    assert sized is None and reason == "balance_zero"
    assert _reason_th("balance_zero") == "ยอด USDT ในบัญชีเป็น 0"

    def boom(_params: dict[str, Any]) -> tuple[int, Any]:
        raise TradeClientError("mark down", http_status=503, binance_code=-1001)

    mark_handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/premiumIndex"): boom,
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
    }
    sized2, reason2 = size_for_execution(
        conn,
        trade=_client(RecordingTransport(mark_handlers)),
        symbol="DOGEUSDT",
        side="short",
        now_ms_value=now,
    )
    assert sized2 is None and reason2 == "fapi_read_failed:mark"
    assert _reason_th("fapi_read_failed:mark") == "อ่านบัญชีไม่ได้ (mark)"

    # Short stop not above entry → size_signal None → size_failed
    bad_stop_conn = open_db(tmp_path / "size_failed.db")
    _seed_analysis(
        bad_stop_conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.8,
        invalidation=0.07,  # below entry 0.08 → invalid short stop
        ts=bar,
    )
    ok_handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    sized3, reason3 = size_for_execution(
        bad_stop_conn,
        trade=_client(RecordingTransport(ok_handlers)),
        symbol="DOGEUSDT",
        side="short",
        now_ms_value=now,
    )
    assert sized3 is None and reason3 == "size_failed"
    conn.close()
    bad_stop_conn.close()


def test_trade_client_ed25519_signature_encoded_once() -> None:
    """Regression: sapi returned -1022 because the Ed25519 signature was percent-encoded twice."""
    import base64
    from urllib.parse import parse_qsl

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(
            200, json={"enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    trade = BinanceTradeClient(BinanceAuth(api_key="TRADE_KEY_TEST", private_key=key), client=http)
    reset_trade_key_cache()
    assert trade.assert_trade_key_safe(mode="live") == {"ok": True}
    reset_trade_key_cache()

    raw_query = seen[0].query.decode()
    assert "%25" not in raw_query  # no double encoding
    pairs = parse_qsl(raw_query, keep_blank_values=True)  # one decode, as the server does
    signature = dict(pairs)["signature"]
    payload = "&".join(f"{k}={v}" for k, v in pairs if k != "signature")
    key.public_key().verify(base64.b64decode(signature), payload.encode())


def test_on_tick_stop_and_target_unchanged_for_long_and_short() -> None:
    tick = Decimal("0.00001")
    cases = (
        ("long", Decimal("0.07600"), Decimal("0.08400")),
        ("short", Decimal("0.08400"), Decimal("0.07600")),
    )
    for side, stop, target in cases:
        assert round_level_to_tick(stop, tick, side=side, kind="stop") == stop
        assert round_level_to_tick(target, tick, side=side, kind="target") == target


def _send_box() -> tuple[list[str], Any]:
    sent: list[str] = []

    def send(text: str, **_kwargs: Any) -> bool:
        sent.append(text)
        return True

    return sent, send


def test_ec_preview_connect_error_replies_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "ec-net.db")
    transport = RecordingTransport({})
    trade = _client(transport)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise httpx.ConnectError("down")

    trade.assert_trade_key_safe = boom  # type: ignore[method-assign]
    sent, send = _send_box()
    out = handle_exec_callback(
        conn,
        "ec:DOGEUSDT:s",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
    )
    assert out["ok"] is False
    assert sent == [MSG_BINANCE_RETRY]
    assert "ลองใหม่" in MSG_BINANCE_RETRY
    claimed = conn.execute("SELECT status FROM assist_exec_intent WHERE status='confirmed'").fetchall()
    assert claimed == []
    assert [m for m, _p, _q in transport.calls if m == "POST"] == []
    conn.close()


def test_ey_stop_connect_error_after_fill_emergency_closes_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    reset_trade_key_cache()
    conn = open_db(tmp_path / "ey-stop.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )

    def stop_boom(_clean: dict[str, Any]) -> None:
        raise httpx.ConnectError("stop down")

    filled = {"n": 0}

    def position_risk(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        # Stay flat through preview/guards/baseline; open only after the market fill.
        if filled["n"] == 0:
            return (200, [])
        return (200, [_open_pos()])

    def order_handler(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if clean.get("reduceOnly") == "true":
            return (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})
        filled["n"] += 1
        return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): position_risk,
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 9, "executedQty": "0", "status": "NEW"}),
        ("POST", "/fapi/v1/marginType"): (200, {"code": 200, "msg": "success"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): order_handler,
        ("POST", "/fapi/v1/algoOrder"): stop_boom,
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    preview = create_entry_preview(conn, symbol="DOGEUSDT", side="short", trade=trade, now_ms_value=now)
    assert preview["ok"] is True
    intent_id = str(preview["intent_id"])
    sent, send = _send_box()
    out = handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
    )
    # A filled entry whose stop cannot be placed (network) is closed reduce-only,
    # never left open without a stop, and never answered with "retry".
    assert out["ok"] is False
    expect = sent[-1]
    assert expect.startswith(MSG_STOP_CLOSED)
    assert "ลองใหม่" not in expect
    assert get_exec_intent(conn, intent_id)["status"] == "failed"
    markets = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(markets) == 2  # entry + emergency close
    assert markets[1][2].get("reduceOnly") == "true"
    again = handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
    )
    assert again.get("replay") is True
    assert sent[-1] == expect
    markets = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(markets) == 2
    conn.close()


def test_ey_preorder_connect_error_fails_without_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "ey-pre.db")
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "1", "stop": "0.07", "target": "0.09", "entry": "0.08", "leverage": 1},
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    transport = RecordingTransport({})
    trade = _client(transport, base="https://fapi.binance.com")

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise httpx.ConnectError("down")

    trade.assert_trade_key_safe = boom  # type: ignore[method-assign]
    sent, send = _send_box()
    out = handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
    )
    assert out["ok"] is False
    assert sent == [MSG_BINANCE_NOT_SENT]
    assert "ลองใหม่" not in MSG_BINANCE_NOT_SENT
    assert get_exec_intent(conn, intent_id)["status"] == "failed"
    assert [c for c in transport.calls if c[0] == "POST"] == []
    again = handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
    )
    assert again.get("replay") is True
    assert sent[-1] == MSG_BINANCE_NOT_SENT
    assert [c for c in transport.calls if c[0] == "POST"] == []
    conn.close()


def _noop_sleep(_seconds: float) -> None:
    return None


def _exec_log_steps(conn: Any, intent_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT step FROM assist_exec_log WHERE intent_id=? ORDER BY id",
        (intent_id,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _base_entry_handlers(*, intent_id: str | None = None) -> dict[tuple[str, str], Any]:
    """Default entry handlers. When intent_id is set, openAlgoOrders returns a VERIFIED sl0."""
    if intent_id is not None:
        sl0 = client_order_id(intent_id, "sl0")
        open_algo: Any = (200, [_verified_stop(client_id=sl0)])
    else:
        open_algo = (200, [])
    return {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("GET", "/fapi/v3/positionRisk"): _flat_then_open([_open_pos()]),
        ("POST", "/fapi/v1/marginType"): (200, {"code": 200, "msg": "success"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            (200, {"algoId": 11}),
            (200, {"algoId": 12}),
        ],
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }


def _attach_verified_sl0(handlers: dict[tuple[str, str], Any], intent_id: str) -> None:
    sl0 = client_order_id(intent_id, "sl0")
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [_verified_stop(client_id=sl0)])
    handlers.setdefault(("GET", "/fapi/v1/algoOrder"), _algo_not_found())


def test_sl0_lands_on_second_lookup_skips_sl1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl0-late.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    sl0 = client_order_id(intent_id, "sl0")
    lookups = {"n": 0}
    handlers = _base_entry_handlers(intent_id=intent_id)

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        lookups["n"] += 1
        # pre-market empty; first post-sl0 verify finds it (skip sl1).
        if lookups["n"] >= 2:
            return (200, [_verified_stop(client_id=sl0, algo_id=11)])
        return (200, [])

    handlers[("POST", "/fapi/v1/algoOrder")] = [
        httpx.ConnectError("sl0 timeout"),
        (200, {"algoId": 12, "clientAlgoId": client_order_id(intent_id, "tp")}),
    ]
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = open_algo
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith("✅")
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert len(stop_posts) == 1
    assert stop_posts[0][2].get("clientAlgoId") == sl0
    steps = _exec_log_steps(conn, intent_id)
    # Best-effort cancel of absent -sl1/-slr may log dup_stop_cancel; no second STOP_MARKET.
    assert "emergency_close" not in steps
    conn.close()


def test_both_stops_fail_lookups_empty_emergency_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "both-fail.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
        (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    assert "ลองใหม่" not in result
    markets = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(markets) == 2
    assert markets[1][2].get("reduceOnly") == "true"
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert len(deletes) >= 2  # direct sl0 + sl1
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    assert "orphan_cancel" in steps
    conn.close()


def test_both_stops_fail_lookups_network_still_emergency_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "lookup-net.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
        (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    # Two real stop responses (not a pre-send ConnectError). Lookups stay down.
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (500, {"code": -1001, "msg": "internal"}),
        (500, {"code": -1001, "msg": "internal"}),
    ]

    def open_boom(_clean: dict[str, Any]) -> BaseException:
        return httpx.ConnectError("lookup down")

    handlers[("GET", "/fapi/v1/openAlgoOrders")] = open_boom
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    assert "ลองใหม่" not in result
    sl0 = client_order_id(intent_id, "sl0")
    sl1 = client_order_id(intent_id, "sl1")
    cls = client_order_id(intent_id, "cls")
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [c[2].get("clientAlgoId") for c in stop_posts] == [sl0, sl1]
    closes = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
    ]
    assert len(closes) == 1
    assert closes[0][2].get("newClientOrderId") == cls
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    conn.close()


def test_mark_get_failure_tries_next_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "mark-next.db")
    intent_id = _entry_intent(conn)
    sl1 = client_order_id(intent_id, "sl1")
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/premiumIndex")] = [
        httpx.ConnectError("mark down"),
        (200, {"markPrice": "0.08"}),
    ]
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [_verified_stop(client_id=sl1)])
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (200, {"algoId": 11}),
        (200, {"algoId": 12}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith("✅")
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert len(stop_posts) == 1
    assert stop_posts[0][2].get("clientAlgoId") == sl1
    assert not any(c[2].get("newClientOrderId", "").endswith("-cls") for c in transport.calls)
    conn.close()


def test_stop_post_success_unreadable_readback_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl-unread.db")
    intent_id = _entry_intent(conn)
    sl0 = client_order_id(intent_id, "sl0")
    handlers = _base_entry_handlers()
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [(200, {"algoId": 11, "clientAlgoId": sl0})]

    def open_boom(_clean: dict[str, Any]) -> BaseException:
        return httpx.ConnectError("lookup down")

    handlers[("GET", "/fapi/v1/openAlgoOrders")] = open_boom
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result == msg_stop_unverifiable(symbol="DOGEUSDT", qty=Decimal("100"), stop=Decimal("0.083"))
    assert getattr(result, "status", None) == "unknown"
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [c[2].get("clientAlgoId") for c in stop_posts] == [sl0]
    assert not any(c[2].get("newClientOrderId", "").endswith("-cls") for c in transport.calls)
    conn.close()


def test_stop_success_not_found_then_verified_no_sl1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl-grace-ok.db")
    intent_id = _entry_intent(conn)
    sl0 = client_order_id(intent_id, "sl0")
    reads = {"n": 0}

    def algo_query(_clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        reads["n"] += 1
        if reads["n"] == 1:
            return _algo_not_found()
        return (
            200,
            _verified_stop(client_id=sl0, algo_id=11),
        )

    handlers = _base_entry_handlers()
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    handlers[("GET", "/fapi/v1/algoOrder")] = algo_query
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (200, {"algoId": 11, "clientAlgoId": sl0}),
        (200, {"algoId": 12}),
    ]
    transport = RecordingTransport(handlers)
    result = run_entry_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert result.startswith("✅")
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [c[2].get("clientAlgoId") for c in stop_posts] == [sl0]
    assert not any(str(c[2].get("newClientOrderId") or "").endswith("-cls") for c in transport.calls)
    assert reads["n"] == 2
    conn.close()


def test_stop_success_not_protected_all_reads_emergency_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl-grace-cls.db")
    intent_id = _entry_intent(conn)
    sl0 = client_order_id(intent_id, "sl0")
    handlers = _base_entry_handlers()
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    handlers[("GET", "/fapi/v1/algoOrder")] = _algo_not_found()
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
        (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [(200, {"algoId": 11, "clientAlgoId": sl0})]
    transport = RecordingTransport(handlers)
    result = run_entry_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [c[2].get("clientAlgoId") for c in stop_posts] == [sl0]
    assert any(
        c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder" and c[2].get("clientAlgoId") == sl0
        for c in transport.calls
    )
    conn.close()


def test_stop_success_not_protected_then_unverifiable_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl-grace-unread.db")
    intent_id = _entry_intent(conn)
    sl0 = client_order_id(intent_id, "sl0")
    reads = {"n": 0}

    def algo_query(_clean: dict[str, Any]) -> tuple[int, dict[str, Any]] | BaseException:
        reads["n"] += 1
        if reads["n"] == 1:
            return _algo_not_found()
        return httpx.ConnectError("algo read down")

    handlers = _base_entry_handlers()
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    handlers[("GET", "/fapi/v1/algoOrder")] = algo_query
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [(200, {"algoId": 11, "clientAlgoId": sl0})]
    transport = RecordingTransport(handlers)
    result = run_entry_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert result == msg_stop_unverifiable(symbol="DOGEUSDT", qty=Decimal("100"), stop=Decimal("0.083"))
    assert getattr(result, "status", None) == "unknown"
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [c[2].get("clientAlgoId") for c in stop_posts] == [sl0]
    assert not any(str(c[2].get("newClientOrderId") or "").endswith("-cls") for c in transport.calls)
    conn.close()


def test_position_flat_before_sl1_sends_nothing_further(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "flat-sl1.db")
    intent_id = _entry_intent(conn)
    sl0 = client_order_id(intent_id, "sl0")
    handlers = _base_entry_handlers()
    handlers[("GET", "/fapi/v3/positionRisk")] = (200, [])
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [(400, {"code": -2010, "msg": "reject"})]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result == MSG_ALREADY_CLOSED
    assert getattr(result, "status", None) == "done"
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [c[2].get("clientAlgoId") for c in stop_posts] == [sl0]
    assert not any(
        c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
        for c in transport.calls
    )
    conn.close()


def test_position_flat_before_emergency_close_sends_nothing_further(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "flat-cls.db")
    intent_id = _entry_intent(conn)
    sl0 = client_order_id(intent_id, "sl0")
    sl1 = client_order_id(intent_id, "sl1")
    state = {"n": 0}

    def position(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        state["n"] += 1
        if state["n"] == 2:
            return (200, [_open_pos()])
        return (200, [])

    handlers = _base_entry_handlers()
    handlers[("GET", "/fapi/v3/positionRisk")] = position
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result == MSG_ALREADY_CLOSED
    assert getattr(result, "status", None) == "done"
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [c[2].get("clientAlgoId") for c in stop_posts] == [sl0, sl1]
    assert not any(
        c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
        for c in transport.calls
    )
    conn.close()


def test_sl0_and_sl1_both_found_cancels_sl1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "dup-stop.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    sl0 = client_order_id(intent_id, "sl0")
    sl1 = client_order_id(intent_id, "sl1")
    lookups = {"n": 0}
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        httpx.ConnectError("sl0"),
        httpx.ConnectError("sl1"),
        (200, {"algoId": 12}),  # tp
    ]

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        lookups["n"] += 1
        # pre + sl0 verify + sl1 verify empty; final _verify_stop_ids finds both.
        if lookups["n"] >= 4:
            return (
                200,
                [
                    _verified_stop(client_id=sl0, algo_id=10),
                    _verified_stop(client_id=sl1, algo_id=11),
                ],
            )
        return (200, [])

    handlers[("GET", "/fapi/v1/openAlgoOrders")] = open_algo
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith("✅")
    deletes = [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"]
    assert [c[2].get("clientAlgoId") for c in deletes] == [sl1]
    steps = _exec_log_steps(conn, intent_id)
    assert "dup_stop_cancel" in steps
    assert "emergency_close" not in steps
    conn.close()


def test_premarket_cancels_stale_jv_not_non_jv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "pre-jv.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    stale = [
        {"algoId": 7, "clientAlgoId": "jv-stale-sl0", "symbol": "DOGEUSDT"},
        {"algoId": 8, "clientAlgoId": "manual-owner-stop", "symbol": "DOGEUSDT"},
    ]
    phase = {"n": 0}

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        phase["n"] += 1
        if phase["n"] == 1:
            return (200, stale)
        return (200, [_verified_stop(client_id=client_order_id(intent_id, "sl0"))])

    handlers[("GET", "/fapi/v1/openAlgoOrders")] = open_algo
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (200, {"algoId": 11}),
        (200, {"algoId": 12}),
    ]
    cancel_failed = {"n": 0}

    def delete_handler(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        cancel_failed["n"] += 1
        return (200, {"algoId": clean.get("algoId")})

    handlers[("DELETE", "/fapi/v1/algoOrder")] = delete_handler
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith("✅")
    deletes_before_market = []
    for method, path, params in transport.calls:
        if method == "POST" and path == "/fapi/v1/order":
            break
        if method == "DELETE" and path == "/fapi/v1/algoOrder":
            deletes_before_market.append(params)
    assert any(d.get("algoId") == "7" for d in deletes_before_market)
    assert all(d.get("algoId") != "8" for d in deletes_before_market)
    conn.close()


def test_premarket_cancel_failure_does_not_block_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "pre-fail.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)

    phase = {"n": 0}

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]] | BaseException:
        phase["n"] += 1
        if phase["n"] == 1:
            return httpx.ConnectError("list down")
        return (200, [_verified_stop(client_id=client_order_id(intent_id, "sl0"))])

    handlers[("GET", "/fapi/v1/openAlgoOrders")] = open_algo
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (200, {"algoId": 11}),
        (200, {"algoId": 12}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith("✅")
    markets = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(markets) >= 1
    conn.close()


def test_set_leverage_connect_error_not_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "lev-net.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)

    def lev_boom(_clean: dict[str, Any]) -> BaseException:
        return httpx.ConnectError("leverage down")

    handlers[("POST", "/fapi/v1/leverage")] = lev_boom
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result == MSG_BINANCE_NOT_SENT
    assert [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"] == []
    conn.close()


def test_preview_balance_connect_error_no_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    reset_trade_key_cache()
    conn = open_db(tmp_path / "bal-net.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): [
            (200, [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}]),
            httpx.ConnectError("balance down"),
        ],
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    preview = create_entry_preview(
        conn, symbol="DOGEUSDT", side="short", trade=trade, now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20)
    )
    assert preview["ok"] is False
    assert preview["text"] == MSG_BINANCE_RETRY
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == 0
    conn.close()


def test_run_entry_rounds_off_tick_preview_levels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "round.db")
    ts = now_ms()
    # Short: stop rounds up, target rounds down.
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.083004",
            "target": "0.070006",
            "leverage": 5,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (200, {"algoId": 11}),
        (200, {"algoId": 12}),
    ]
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (
        200,
        [_verified_stop(client_id=client_order_id(intent_id, "sl0"), trigger="0.08301")],
    )
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith("✅")
    algo_posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
    stop_call = next(c for c in algo_posts if c[2].get("type") == "STOP_MARKET")
    tp_call = next(c for c in algo_posts if c[2].get("type") == "TAKE_PROFIT_MARKET")
    assert stop_call[2]["triggerPrice"] == "0.08301"
    assert tp_call[2]["triggerPrice"] == "0.07000"
    conn.close()


def test_emergency_close_position_gone_after_first_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "cls-gone.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    pos_calls = {"n": 0}

    def close_then_gone(clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if clean.get("reduceOnly") == "true":
            return httpx.ConnectError("close down")
        return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    def position_risk(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        pos_calls["n"] += 1
        # 1 = baseline flat. Reads 2-4 stay open (before sl1, final verify, pre-close).
        # The read after the failed close is flat.
        if pos_calls["n"] == 1 or pos_calls["n"] >= 5:
            return (200, [])
        return (200, [_open_pos()])

    handlers[("POST", "/fapi/v1/order")] = close_then_gone
    handlers[("GET", "/fapi/v3/positionRisk")] = position_risk
    handlers[("GET", "/fapi/v1/order")] = (400, {"code": -2013, "msg": "Order does not exist."})
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
    ]
    assert len(cls_posts) == 1
    assert cls_posts[0][2].get("newClientOrderId") == client_order_id(intent_id, "cls")
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert any(c[2].get("clientAlgoId") == client_order_id(intent_id, "sl0") for c in deletes)
    assert any(c[2].get("clientAlgoId") == client_order_id(intent_id, "sl1") for c in deletes)
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    assert "orphan_cancel" in steps
    conn.close()


def test_emergency_close_retries_same_client_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "cls-retry.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    cls_n = {"n": 0}

    def order_handler(clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if clean.get("reduceOnly") != "true":
            return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})
        cls_n["n"] += 1
        if cls_n["n"] == 1:
            return httpx.ConnectError("close1")
        return (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    handlers[("POST", "/fapi/v1/order")] = order_handler
    handlers[("GET", "/fapi/v3/positionRisk")] = _flat_then_open(
        [_open_pos(amt="-100", entry="0.08", mark="0.08")]
    )
    handlers[("GET", "/fapi/v1/order")] = (200, {"orderId": 9, "executedQty": "0", "status": "NEW"})
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    cls_id = client_order_id(intent_id, "cls")
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
    ]
    assert len(cls_posts) == 2
    assert all(c[2].get("newClientOrderId") == cls_id for c in cls_posts)
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    assert "emergency_close_2" in steps
    conn.close()


def test_emergency_close_all_fail_sends_alert_and_notifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    reset_trade_key_cache()
    conn = open_db(tmp_path / "cls-fail.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (
            200,
            [
                {
                    "symbol": "DOGEUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "-100",
                    "entryPrice": "0.08",
                    "markPrice": "0.08",
                }
            ],
        ),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 9, "executedQty": "0", "status": "NEW"}),
        ("POST", "/fapi/v1/marginType"): (200, {"code": 200, "msg": "success"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
            httpx.ConnectError("c1"),
            httpx.ConnectError("c2"),
            httpx.ConnectError("c3"),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            (400, {"code": -2010, "msg": "reject"}),
            (400, {"code": -2010, "msg": "reject"}),
        ],
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    # After market fill, guards see no position; during close keep position open.
    pos_phase = {"entry_done": False}

    def position_risk(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        if not pos_phase["entry_done"]:
            return (200, [])
        return (
            200,
            [
                {
                    "symbol": "DOGEUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "-100",
                    "entryPrice": "0.08",
                    "markPrice": "0.08",
                }
            ],
        )

    def order_handler(clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if clean.get("reduceOnly") == "true":
            pos_phase["entry_done"] = True
            return httpx.ConnectError("close fail")
        pos_phase["entry_done"] = True
        return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    handlers[("GET", "/fapi/v3/positionRisk")] = position_risk
    handlers[("POST", "/fapi/v1/order")] = order_handler
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    preview = create_entry_preview(conn, symbol="DOGEUSDT", side="short", trade=trade, now_ms_value=now)
    assert preview["ok"] is True
    intent_id = str(preview["intent_id"])
    sent, send = _send_box()
    edited: list[str] = []
    monkeypatch.setattr("jarvise_assist.execute.time.sleep", _noop_sleep)
    out = handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda _m, t, **_: edited.append(t),
        trade=trade,
        now_ms_value=now,
        message={"message_id": 42},
    )
    assert out["ok"] is False
    expect = sent[-1]
    assert expect.startswith("🚨")
    assert "DOGEUSDT" in expect
    assert "เข้า Binance" in expect
    assert "ลองใหม่" not in expect
    assert edited == [expect]
    assert len(sent) == 1
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
    ]
    assert len(cls_posts) == 3
    again = handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
        message={"message_id": 42},
    )
    assert again.get("replay") is True
    assert sent[-1] == expect
    assert (
        len(
            [
                c
                for c in transport.calls
                if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
            ]
        )
        == 3
    )
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    assert "emergency_close_2" in steps
    assert "emergency_close_3" in steps
    conn.close()


def test_emergency_close_check_failures_still_send_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When position/fill checks raise, still POST reduceOnly three times, then alert."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "cls-check-fail.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    close_phase = {"on": False}
    pos_n = {"n": 0}

    def order_handler(clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if clean.get("reduceOnly") == "true":
            close_phase["on"] = True
            return httpx.ConnectError("close down")
        return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    def position_risk(_clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if close_phase["on"]:
            return httpx.ConnectError("positionRisk down")
        pos_n["n"] += 1
        if pos_n["n"] == 1:
            return (200, [])
        return (200, [_open_pos()])

    def query_order(_clean: dict[str, Any]) -> BaseException:
        return httpx.ConnectError("order query down")

    handlers[("POST", "/fapi/v1/order")] = order_handler
    handlers[("GET", "/fapi/v3/positionRisk")] = position_risk
    handlers[("GET", "/fapi/v1/order")] = query_order
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith("🚨")
    assert "เข้า Binance" in result
    assert "ลองใหม่" not in result
    cls_id = client_order_id(intent_id, "cls")
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
    ]
    assert len(cls_posts) == 3
    assert all(c[2].get("newClientOrderId") == cls_id for c in cls_posts)
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    assert "emergency_close_2" in steps
    assert "emergency_close_3" in steps
    conn.close()


def test_emergency_close_check_fail_second_fill_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checks fail but 2nd reduceOnly fills → success and orphan jv- cancel."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "cls-check-fill2.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    close_phase = {"on": False}
    cls_n = {"n": 0}
    pos_n = {"n": 0}

    def order_handler(clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if clean.get("reduceOnly") != "true":
            return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})
        close_phase["on"] = True
        cls_n["n"] += 1
        if cls_n["n"] == 1:
            return httpx.ConnectError("close1")
        return (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    def position_risk(_clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if close_phase["on"]:
            return httpx.ConnectError("positionRisk down")
        pos_n["n"] += 1
        if pos_n["n"] == 1:
            return (200, [])
        return (200, [_open_pos()])

    def query_order(_clean: dict[str, Any]) -> BaseException:
        return httpx.ConnectError("order query down")

    handlers[("POST", "/fapi/v1/order")] = order_handler
    handlers[("GET", "/fapi/v3/positionRisk")] = position_risk
    handlers[("GET", "/fapi/v1/order")] = query_order
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    cls_id = client_order_id(intent_id, "cls")
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
    ]
    assert len(cls_posts) == 2
    assert all(c[2].get("newClientOrderId") == cls_id for c in cls_posts)
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert any(c[2].get("clientAlgoId") == client_order_id(intent_id, "sl0") for c in deletes)
    assert any(c[2].get("clientAlgoId") == client_order_id(intent_id, "sl1") for c in deletes)
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    assert "emergency_close_2" in steps
    assert "orphan_cancel" in steps
    conn.close()


def test_emergency_close_reduce_only_2022_flat_counts_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-2022 then positionRisk empty → closed, no second POST."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "cls-2022-flat.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    pos_calls = {"n": 0}

    def order_handler(clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if clean.get("reduceOnly") == "true":
            return (400, {"code": -2022, "msg": "ReduceOnly Order is rejected."})
        return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    def position_risk(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        pos_calls["n"] += 1
        # Open through the pre-close re-read; flat once -2022 is being confirmed.
        if pos_calls["n"] == 1 or pos_calls["n"] >= 5:
            return (200, [])
        return (200, [_open_pos()])

    handlers[("POST", "/fapi/v1/order")] = order_handler
    handlers[("GET", "/fapi/v3/positionRisk")] = position_risk
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
    ]
    assert len(cls_posts) == 1
    assert cls_posts[0][2].get("newClientOrderId") == client_order_id(intent_id, "cls")
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    assert "emergency_close_2" not in steps
    conn.close()


def test_emergency_close_reduce_only_2022_still_open_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-2022 while positionRisk still open → resend, then fill on attempt 2."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "cls-2022-open.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    cls_n = {"n": 0}
    open_pos = [
        {
            "symbol": "DOGEUSDT",
            "positionSide": "BOTH",
            "positionAmt": "-100",
            "entryPrice": "0.08",
            "markPrice": "0.08",
        }
    ]

    def order_handler(clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        if clean.get("reduceOnly") != "true":
            return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})
        cls_n["n"] += 1
        if cls_n["n"] == 1:
            return (400, {"code": -2022, "msg": "ReduceOnly Order is rejected."})
        return (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    handlers[("POST", "/fapi/v1/order")] = order_handler
    handlers[("GET", "/fapi/v3/positionRisk")] = _flat_then_open(open_pos)
    handlers[("GET", "/fapi/v1/order")] = (200, {"orderId": 9, "executedQty": "0", "status": "NEW"})
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    cls_id = client_order_id(intent_id, "cls")
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
    ]
    assert len(cls_posts) == 2
    assert all(c[2].get("newClientOrderId") == cls_id for c in cls_posts)
    steps = _exec_log_steps(conn, intent_id)
    assert "emergency_close" in steps
    assert "emergency_close_2" in steps
    conn.close()


def test_orphan_list_fail_direct_cancel_unknown_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "orphan-direct.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
        (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    transport_box: dict[str, RecordingTransport] = {}

    def open_after_close(_clean: dict[str, Any]) -> tuple[int, Any] | BaseException:
        # Pre-market ok; post-close list fails so direct cancels run.
        transport = transport_box["t"]
        if any(
            c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"
            for c in transport.calls
        ):
            return httpx.ConnectError("list down")
        return (200, [])

    def delete_handler(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if clean.get("clientAlgoId") == client_order_id(intent_id, "sl0"):
            return (400, {"code": -2011, "msg": "Unknown order sent."})
        return (200, {"algoId": 0})

    handlers[("GET", "/fapi/v1/openAlgoOrders")] = open_after_close
    handlers[("DELETE", "/fapi/v1/algoOrder")] = delete_handler
    transport = RecordingTransport(handlers)
    transport_box["t"] = transport
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    assert "ยกเลิก stop ค้างไม่ได้" not in result
    direct = [
        c
        for c in transport.calls
        if c[0] == "DELETE"
        and c[2].get("clientAlgoId")
        in {
            client_order_id(intent_id, "sl0"),
            client_order_id(intent_id, "sl1"),
        }
    ]
    assert len(direct) == 2
    steps = _exec_log_steps(conn, intent_id)
    assert "orphan_cancel" in steps
    conn.close()


def test_orphan_cancel_unconfirmed_appends_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "orphan-warn.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("GET", "/fapi/v1/openAlgoOrders")] = (200, [])
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
        (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (400, {"code": -2010, "msg": "reject"}),
        (400, {"code": -2010, "msg": "reject"}),
    ]
    handlers[("DELETE", "/fapi/v1/algoOrder")] = (500, {"code": -1001, "msg": "internal"})
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert result.startswith(MSG_STOP_CLOSED)
    assert "ยกเลิก stop ค้างไม่ได้ — เช็ก Open Orders ของ DOGEUSDT" in result
    conn.close()


def test_msg_stop_closed_edits_and_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    reset_trade_key_cache()
    conn = open_db(tmp_path / "notify.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("POST", "/fapi/v1/marginType"): (200, {"code": 200, "msg": "success"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/algoOrder"): [
            (400, {"code": -2010, "msg": "reject"}),
            (400, {"code": -2010, "msg": "reject"}),
        ],
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    filled = {"on": False}

    def position_after_fill(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        if filled["on"]:
            return (200, [_open_pos()])
        return (200, [])

    def order_script(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if clean.get("reduceOnly") == "true":
            return (200, {"orderId": 2, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})
        filled["on"] = True
        return (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"})

    handlers[("GET", "/fapi/v3/positionRisk")] = position_after_fill
    handlers[("POST", "/fapi/v1/order")] = order_script
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    preview = create_entry_preview(conn, symbol="DOGEUSDT", side="short", trade=trade, now_ms_value=now)
    intent_id = str(preview["intent_id"])
    sent, send = _send_box()
    edited: list[str] = []
    out = handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda _m, t, **_: edited.append(t),
        trade=trade,
        now_ms_value=now,
        message={"message_id": 7},
    )
    assert out["ok"] is False
    assert len(sent) == 1
    assert sent[0].startswith(MSG_STOP_CLOSED)
    assert edited == sent
    conn.close()


def _held_exit_flush_fixtures(conn: Any, *, now: int) -> FuturesPosition:
    """Watch row differs from live qty/entry; mark still hits target_2r for the stored stop."""
    upsert_watch_position(
        conn,
        {
            "symbol": "DOGEUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.09,
            "qty": 50.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.083,
            "initial_stop_source": "signal",
            "entry_time_ms": int(now) - 3_600_000,
            "no_stop_latched": 0,
            "last_seen_ms": int(now),
            "closed_at_ms": None,
        },
    )
    insert_watch_advice(
        conn,
        symbol="DOGEUSDT",
        position_side="BOTH",
        kind="exit",
        reason="target_2r",
        level=None,
        approx_r=2.0,
        status="held",
        created_at_ms=now - 60_000,
    )
    # Live short: risk vs initial_stop 0.083 is 0.003 → target_2r at mark <= 0.074.
    return FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.074"),
        update_time_ms=now - 3_600_000,
    )


def test_flush_held_exit_live_adds_xx_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "flush_live.db")
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    pos = _held_exit_flush_fixtures(conn, now=now)
    fake = FakeFutures(positions=[pos])
    captured: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> bool:
        captured.append({"text": text, "kwargs": kwargs})
        return True

    out = flush_held_watch_advice(conn, futures=fake, now_ms_value=now, send=send)
    assert len(out["sent"]) == 1
    assert len(captured) == 1
    markup = captured[0]["kwargs"].get("reply_markup")
    assert markup is not None
    buttons = markup["inline_keyboard"][0]
    assert any(str(b.get("callback_data") or "").startswith("xx:") for b in buttons)
    rows = conn.execute("SELECT preview_json FROM assist_exec_intent WHERE kind='exit'").fetchall()
    assert len(rows) == 1
    preview = json.loads(str(rows[0][0]))
    assert preview == {"qty": "100", "entry": "0.08"}
    conn.close()


def test_flush_held_exit_off_sends_text_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    conn = open_db(tmp_path / "flush_off.db")
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    pos = _held_exit_flush_fixtures(conn, now=now)
    fake = FakeFutures(positions=[pos])
    captured: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> bool:
        captured.append({"text": text, "kwargs": kwargs})
        return True

    out = flush_held_watch_advice(conn, futures=fake, now_ms_value=now, send=send)
    assert len(out["sent"]) == 1
    assert captured[0]["kwargs"].get("reply_markup") is None
    n = conn.execute("SELECT COUNT(*) FROM assist_exec_intent WHERE kind='exit'").fetchone()[0]
    assert n == 0
    conn.close()


def test_exit_refuses_changed_position(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "exit_changed.db")
    ts = now_ms()
    live = {
        "symbol": "DOGEUSDT",
        "positionSide": "BOTH",
        "positionAmt": "-100",
        "entryPrice": "0.08",
        "markPrice": "0.07",
    }

    def _run(preview: dict[str, str]) -> tuple[str, RecordingTransport]:
        intent_id = insert_exec_intent(
            conn,
            kind="exit",
            symbol="DOGEUSDT",
            side="short",
            preview=preview,
            created_at_ms=ts,
            expires_at_ms=ts + 120_000,
        )
        claim_exec_intent(conn, intent_id)
        handlers: dict[tuple[str, str], Any] = {
            ("GET", "/fapi/v3/positionRisk"): (200, [live]),
            ("POST", "/fapi/v1/order"): (200, {"orderId": 9, "avgPrice": "0.07", "executedQty": "100"}),
        }
        transport = RecordingTransport(handlers)
        trade = _client(transport)
        return run_exit_sequence(conn, intent_id, trade=trade), transport

    for preview in (
        {"qty": "50", "entry": "0.08"},
        {"qty": "100", "entry": "0.09"},
        {"qty": "100"},
        {"qty": "abc", "entry": "0.08"},
    ):
        result, transport = _run(preview)
        assert result == MSG_EXIT_POSITION_CHANGED
        assert not any(c[0] == "POST" for c in transport.calls)

    # Matching qty and entry still closes.
    intent_ok = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_ok)
    handlers_ok: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([live]),
        ("POST", "/fapi/v1/order"): (200, {"orderId": 9, "avgPrice": "0.07", "executedQty": "100"}),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "1.0"}]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
    }
    transport_ok = RecordingTransport(handlers_ok)
    result = run_exit_sequence(conn, intent_ok, trade=_client(transport_ok))
    assert result.startswith("✅")
    assert any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport_ok.calls)
    conn.close()


def test_market_1007_unknown_then_position_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "mkt1007.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("POST", "/fapi/v1/order")] = (400, {"code": -1007, "msg": "Timeout waiting for response"})

    def query_boom(_clean: dict[str, Any]) -> BaseException:
        return httpx.ConnectError("query down")

    handlers[("GET", "/fapi/v1/order")] = query_boom
    handlers[("GET", "/fapi/v3/positionRisk")] = _flat_then_open([_open_pos()])
    transport = RecordingTransport(handlers)
    result = run_entry_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert result.startswith("✅")
    mkt_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") != "true"
    ]
    assert len(mkt_posts) == 1
    conn.close()


def test_market_2019_no_order_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "mkt2019.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers()
    handlers[("POST", "/fapi/v1/order")] = (400, {"code": -2019, "msg": "Margin is insufficient."})
    handlers[("GET", "/fapi/v3/positionRisk")] = (200, [])
    transport = RecordingTransport(handlers)
    result = run_entry_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert "เข้าไม้ไม่สำเร็จ" in result
    assert getattr(result, "status", None) == "failed"
    assert not any(c[0] == "GET" and c[1] == "/fapi/v1/order" for c in transport.calls)
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" for c in transport.calls)
    conn.close()


def test_qty_zero_new_then_filled_on_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "qty0.db")
    intent_id = _entry_intent(conn)
    claim_exec_intent(conn, intent_id)
    handlers = _base_entry_handlers(intent_id=intent_id)
    handlers[("POST", "/fapi/v1/order")] = (
        200,
        {"orderId": 1, "executedQty": "0", "avgPrice": "0", "status": "NEW"},
    )
    handlers[("GET", "/fapi/v1/order")] = (
        200,
        {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
    )
    transport = RecordingTransport(handlers)
    result = run_entry_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert result.startswith("✅")
    conn.close()


def test_preview_refused_while_symbol_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "inflight.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    ts = now_ms()
    insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "stop": "0.083", "target": "0.07", "leverage": 5, "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    finish_exec_intent(
        conn,
        conn.execute("SELECT id FROM assist_exec_intent").fetchone()[0],
        status="unknown",
        result_text=MSG_OUTCOME_UNKNOWN,
    )
    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
    }
    out = create_entry_preview(
        conn,
        symbol="DOGEUSDT",
        side="short",
        trade=_client(RecordingTransport(handlers)),
        now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
    )
    assert out["ok"] is False
    assert "ยังไม่จบ" in out["text"]
    conn.close()


def test_recovery_places_slr_no_second_mkt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "rec-slr.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    slr = client_order_id(intent_id, "slr")
    placed = {"ok": False}

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        if placed["ok"]:
            return (200, [_verified_stop(client_id=slr)])
        return (200, [])

    def place_slr(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        placed["ok"] = True
        return (200, {"algoId": 99, "clientAlgoId": clean.get("clientAlgoId")})

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("POST", "/fapi/v1/algoOrder"): place_slr,
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    out = run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    assert out.get("claimed") == 1
    assert any(c[2].get("clientAlgoId") == slr for c in transport.calls if c[0] == "POST")
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport.calls)
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    conn.close()


def test_recovery_not_found_three_passes_across_15min(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "rec-nf.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    t0 = 1_700_000_000_000
    # Two prior not-found passes already logged 15 minutes apart from the next pass.
    for offset in (0, 5 * 60_000):
        conn.execute(
            """
            INSERT INTO assist_exec_log (intent_id, step, endpoint, binance_code, created_at_ms)
            VALUES (?, 'recover_not_found', '/fapi/v1/order', -2013, ?)
            """,
            (intent_id, t0 + offset),
        )
    conn.commit()
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/order"): (400, {"code": -2013, "msg": "Order does not exist."}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        t0 + 15 * 60_000,
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    assert row["status"] == "not_found"
    assert MSG_ORDER_NOT_FOUND in sent
    conn.close()


def test_recovery_claim_one_of_two_wins(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "claim.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text="x")
    ts = now_ms()
    token = claim_exec_intent_for_recovery(conn, intent_id, now_ms_value=ts)
    assert isinstance(token, int)
    # recovering is leased: second claim inside the lease fails.
    assert claim_exec_intent_for_recovery(conn, intent_id, now_ms_value=ts + 1) is None
    # After the lease expires, recovering is claimable again.
    assert isinstance(
        claim_exec_intent_for_recovery(conn, intent_id, now_ms_value=ts + RECOVER_LEASE_MS),
        int,
    )
    # Fresh confirmed with recent log is blocked.
    finish_exec_intent(conn, intent_id, status="confirmed", result_text="")
    conn.execute(
        "INSERT INTO assist_exec_log (intent_id, step, endpoint, created_at_ms) VALUES (?,?,?,?)",
        (intent_id, "worker_claim", "local", ts + RECOVER_LEASE_MS + 1),
    )
    conn.commit()
    assert claim_exec_intent_for_recovery(conn, intent_id, now_ms_value=ts + RECOVER_LEASE_MS + 2) is None
    conn.close()


def test_account_modes_unreadable_and_one_way_on_ms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "modes.db")
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts)
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "a"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": "false"}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
    }
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport(handlers)),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert any(MSG_MODE_UNREADABLE in t for t in sent)
    assert get_exec_intent(conn, intent_id)["status"] == "failed"

    intent2 = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "a"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    handlers2 = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": True}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
    }
    sent2: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{intent2}",
        send=lambda t, **_: sent2.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport(handlers2)),
        now_ms_value=ts,
        message={"message_id": 2},
    )
    assert any(MSG_ONE_WAY in t for t in sent2)
    conn.close()


def test_stale_card_stop_change_expired_same_stop_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Testnet compares side only; a stop mismatch must not expire. Side change still expires."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    reset_trade_key_cache()
    conn = open_db(tmp_path / "stale.db")
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    handlers = _base_entry_handlers()
    handlers[("GET", "/fapi/v1/positionSide/dual")] = (200, {"dualSidePosition": False})
    handlers[("GET", "/fapi/v1/multiAssetsMargin")] = (200, {"multiAssetsMargin": False})
    handlers[("GET", "/fapi/v3/balance")] = (
        200,
        [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
    )
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    preview = create_entry_preview(conn, symbol="DOGEUSDT", side="short", trade=trade, now_ms_value=now)
    assert preview["ok"] is True
    intent_id = str(preview["intent_id"])
    _attach_verified_sl0(handlers, intent_id)
    # Change invalidation so rounded stop differs — testnet must not expire.
    conn.execute(
        "UPDATE analysis_output SET invalidation_price=? WHERE symbol=?",
        (0.09, "DOGEUSDT"),
    )
    conn.commit()
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
    )
    assert not any(MSG_EXPIRED in t for t in sent)
    assert get_exec_intent(conn, intent_id)["status"] != "expired"

    # Side change still expires on ey: confirm.
    intent2 = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.083",
            "target": "0.07",
            "leverage": 5,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=now + 1,
        expires_at_ms=now + 120_000,
    )
    conn.execute("UPDATE analysis_output SET action=? WHERE symbol=?", ("long", "DOGEUSDT"))
    conn.commit()
    sent2: list[str] = []
    handle_exec_callback(
        conn,
        f"ey:{intent2}",
        send=lambda t, **_: sent2.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now + 1,
    )
    assert any(MSG_EXPIRED in t for t in sent2)
    assert get_exec_intent(conn, intent2)["status"] == "expired"
    conn.close()


def test_live_stale_card_stop_change_refused_same_stop_new_analysis_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    reset_trade_key_cache()
    conn = open_db(tmp_path / "stale-live.db")
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    bar = expected_analysis_bar_ms(now)
    _seed_doge_short(conn, ts=bar)
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/sapi/v1/account/apiRestrictions"): (
            200,
            {"enableFutures": True, "enableWithdrawals": False, "ipRestrict": True},
        ),
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
    }
    trade = _client(RecordingTransport(handlers), base="https://fapi.binance.com")
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.083",
            "target": "0.07",
            "leverage": 5,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    conn.execute(
        "UPDATE analysis_output SET invalidation_price=? WHERE symbol=?",
        (0.09, "DOGEUSDT"),
    )
    conn.commit()
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ey:{intent_id}",
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now,
    )
    assert any(MSG_EXPIRED in t for t in sent)
    assert get_exec_intent(conn, intent_id)["status"] == "expired"

    _seed_doge_short(conn, ts=bar + 1_000)
    intent_same = insert_exec_intent(
        conn,
        kind="entry",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "stop": "0.083",
            "target": "0.07",
            "leverage": 5,
            "entry": "0.08",
            "loss_at_stop": "0.3",
            "r_distance": "0.003",
        },
        created_at_ms=now + 1,
        expires_at_ms=now + 120_000,
    )
    sent_same: list[str] = []
    handle_exec_callback(
        conn,
        f"ey:{intent_same}",
        send=lambda t, **_: sent_same.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=now + 1,
    )
    assert not any(MSG_EXPIRED in t for t in sent_same)
    assert any("ถืออยู่แล้ว" in t for t in sent_same)
    assert get_exec_intent(conn, intent_same)["status"] != "expired"
    conn.close()


def test_ms_xx_preorder_failure_does_not_use_entry_retry_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "preorder-copy.db")
    ts = now_ms()
    move_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    exit_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    mode_trade = _client(
        RecordingTransport({("GET", "/fapi/v1/positionSide/dual"): httpx.ConnectError("mode")})
    )
    sent_mode: list[str] = []
    mode_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100", "advice_text": "advice"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    handle_exec_callback(
        conn,
        f"ms:{mode_id}",
        send=lambda t, **_: sent_mode.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=mode_trade,
        now_ms_value=ts,
        message={"message_id": 3},
    )
    assert sent_mode == [MSG_MODE_UNREADABLE]
    assert MSG_KEY_REFUSED not in sent_mode[0]
    preorder_transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
            ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
            ("GET", "/fapi/v3/positionRisk"): httpx.ConnectError("position"),
        }
    )
    trade = _client(preorder_transport)
    sent_ms: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{move_id}",
        send=lambda t, **_: sent_ms.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert sent_ms == ["อ่าน Binance ไม่ได้ — ยังไม่ได้ส่งคำสั่ง รอคำแนะนำรอบถัดไป หรือดู 📋 ไม้ที่ถืออยู่"]
    assert "ลองใหม่" not in sent_ms[0]
    assert get_exec_intent(conn, move_id)["status"] == "failed"
    sent_xx: list[str] = []
    handle_exec_callback(
        conn,
        f"xx:{exit_id}",
        send=lambda t, **_: sent_xx.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 2},
    )
    assert sent_xx == ["อ่าน Binance ไม่ได้ — ยังไม่ได้ส่งคำสั่ง ดู 📋 ไม้ที่ถืออยู่"]
    assert "ลองใหม่" not in sent_xx[0]
    assert get_exec_intent(conn, exit_id)["status"] == "failed"
    assert not any(
        c[0] == "POST" and c[1] in {"/fapi/v1/order", "/fapi/v1/algoOrder"} for c in preorder_transport.calls
    )
    conn.close()


def test_exit_partial_leaves_stop_no_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ex-partial.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "40", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "40", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (
            200,
            [_verified_stop(client_id="jv-abc-sl0")],
        ),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
    }
    transport = RecordingTransport(handlers)
    result = run_exit_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert "ปิดบางส่วน" in result
    assert "เช็ก stop ของส่วนที่เหลือ" in result
    assert not any(c[0] == "DELETE" for c in transport.calls)
    conn.close()


def test_recovery_lease_blocks_second_slr_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First pass places -slr then stays recovering (verify UNVERIFIABLE); second pass posts none."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "lease-slr.db")
    t0 = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=t0)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    slr = client_order_id(intent_id, "slr")
    placed = {"ok": False}

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        return (200, [])

    def place_slr(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        placed["ok"] = True
        return (200, {"algoId": 99, "clientAlgoId": clean.get("clientAlgoId")})

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/algoOrder"): (500, {"code": -1001, "msg": "timeout"}),
        ("POST", "/fapi/v1/algoOrder"): place_slr,
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    run_exec_recovery(conn, trade, t0 + 1_000, send=lambda *_a, **_k: True, sleep=_noop_sleep)
    assert placed["ok"] is True
    assert get_exec_intent(conn, intent_id)["status"] == "recovering"
    posts_after_first = sum(
        1
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("clientAlgoId") == slr
    )
    assert posts_after_first == 1
    # Second pass inside the lease must not claim or post another -slr.
    run_exec_recovery(conn, trade, t0 + 2_000, send=lambda *_a, **_k: True, sleep=_noop_sleep)
    posts_total = sum(
        1
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("clientAlgoId") == slr
    )
    assert posts_total == 1
    conn.close()


def test_recovery_filled_flat_done_no_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "filled-flat.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    assert MSG_ALREADY_CLOSED in sent
    assert not any(c[0] == "POST" for c in transport.calls)
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is False
    conn.close()


def test_recovery_not_filled_with_position_fails_no_slr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "nf-pos.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "0", "avgPrice": "0", "status": "CANCELED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "failed"
    assert any("เข้าไม้ไม่สำเร็จ" in t for t in sent)
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_recovery_opposite_side_mismatch_no_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "side-mis.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    # Intent is short; position is long.
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="100")]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "failed"
    assert any(MSG_RECOVER_SIDE_MISMATCH.format(symbol="DOGEUSDT") in t for t in sent)
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_recovery_older_than_2h_stale_no_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Age > 2h with a readable book reports the true state and sends no order."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "stale-2h.db")
    created = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=created)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 1, "status": "FILLED", "executedQty": "100"}),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    now = created + RECOVER_MAX_AGE_MS + 1
    run_exec_recovery(
        conn,
        _client(transport),
        now,
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "stale"
    assert len(sent) == 1
    assert sent[0] == msg_recover_true_state(
        symbol="DOGEUSDT",
        created_at_ms=created,
        order_text="FILLED 100",
        position_text="100",
        stop_verified=False,
        position_open=True,
    )
    assert "position ไม่มี stop" in sent[0]
    assert not any(c[0] == "POST" for c in transport.calls)
    assert not any(c[0] == "DELETE" for c in transport.calls)
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is False
    conn.close()


def test_recovery_older_than_2h_unreadable_then_readable_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "stale-unread.db")
    created = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=created)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    phase = {"readable": False}

    def position(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]] | BaseException:
        if not phase["readable"]:
            return httpx.ConnectError("position down")
        return (200, [_open_pos()])

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): position,
        ("GET", "/fapi/v1/order"): (200, {"orderId": 1, "status": "FILLED", "executedQty": "100"}),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    trade = _client(transport)
    now = created + RECOVER_MAX_AGE_MS + 1

    def send(text: str, **_kwargs: Any) -> bool:
        sent.append(text)
        return True

    run_exec_recovery(conn, trade, now, send=send, sleep=_noop_sleep)
    assert sent == [MSG_RECOVER_STALE_UNREADABLE.format(symbol="DOGEUSDT")]
    assert get_exec_intent(conn, intent_id)["status"] == "unknown"
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is True
    assert not any(c[0] == "POST" for c in transport.calls)
    run_exec_recovery(conn, trade, now + 1_000, send=send, sleep=_noop_sleep)
    assert sent == [MSG_RECOVER_STALE_UNREADABLE.format(symbol="DOGEUSDT")]
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is True
    phase["readable"] = True
    run_exec_recovery(conn, trade, now + 2_000, send=send, sleep=_noop_sleep)
    assert len(sent) == 2
    assert "FILLED 100" in sent[1]
    assert "position ไม่มี stop" in sent[1]
    assert get_exec_intent(conn, intent_id)["status"] == "stale"
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is False
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def _protective_order(
    *,
    client_id: str,
    qty: str,
    side: str = "BUY",
    order_type: str = "STOP_MARKET",
) -> dict[str, Any]:
    return {
        "symbol": "DOGEUSDT",
        "type": order_type,
        "side": side,
        "positionSide": "BOTH",
        "reduceOnly": True,
        "stopPrice": "0.083",
        "quantity": qty,
        "algoId": 11,
        "clientAlgoId": client_id,
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }


def _run_aged(
    conn: Any, intent_id: str, handlers: dict[tuple[str, str], Any], created: int
) -> tuple[str, str]:
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(RecordingTransport(handlers)),
        created + RECOVER_MAX_AGE_MS + 1,
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None
    return str(row["status"]), sent[0] if sent else ""


def test_aged_move_stop_covering_position_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "aged-ms.db")
    created = 1_700_000_000_000
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=created,
        expires_at_ms=created + 120_000,
    )
    finish_exec_intent(conn, intent_id, status="unknown", result_text="x")
    ms = client_order_id(intent_id, "ms")
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/algoOrder"): (200, {"algoId": 11, "algoStatus": "NEW", "executedQty": "0"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [_protective_order(client_id=ms, qty="100")]),
    }
    status, text = _run_aged(conn, intent_id, handlers, created)
    assert status == "stale"
    assert "ยืนยันแล้ว" in text
    conn.close()


def test_aged_entry_covered_by_later_move_stop_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "aged-replaced.db")
    created = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=created)
    finish_exec_intent(conn, intent_id, status="unknown", result_text="x")
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 1, "status": "FILLED", "executedQty": "100"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (
            200,
            [_protective_order(client_id="jv-later-ms", qty="100")],
        ),
    }
    status, text = _run_aged(conn, intent_id, handlers, created)
    assert status == "stale"
    assert "ยืนยันแล้ว" in text
    conn.close()


def test_aged_entry_take_profit_only_is_not_a_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "aged-tp.db")
    created = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=created)
    finish_exec_intent(conn, intent_id, status="unknown", result_text="x")
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 1, "status": "FILLED", "executedQty": "100"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (
            200,
            [_protective_order(client_id="jv-tp", qty="100", order_type="TAKE_PROFIT_MARKET")],
        ),
    }
    status, text = _run_aged(conn, intent_id, handlers, created)
    assert status == "stale"
    assert "ไม่มี (position ไม่มี stop)" in text
    conn.close()


def test_aged_partial_stop_qty_is_not_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "aged-half.db")
    created = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=created)
    finish_exec_intent(conn, intent_id, status="unknown", result_text="x")
    info = _exchange_info()
    info["symbols"][0]["filters"][0]["stepSize"] = "0.001"
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, info),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-1", entry="0.08")]),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 1, "status": "FILLED", "executedQty": "1"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [_protective_order(client_id="jv-half", qty="0.5")]),
    }
    status, text = _run_aged(conn, intent_id, handlers, created)
    assert status == "stale"
    assert "ยืนยันแล้ว" not in text
    assert "ไม่มี (position ไม่มี stop)" in text
    conn.close()


def test_aged_flat_position_does_not_say_missing_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "aged-flat.db")
    created = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=created)
    finish_exec_intent(conn, intent_id, status="unknown", result_text="x")
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 1, "status": "FILLED", "executedQty": "100"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    status, text = _run_aged(conn, intent_id, handlers, created)
    assert status == "stale"
    assert "แบน" in text
    assert "ไม่มี stop" not in text
    conn.close()


def test_aged_open_algo_non_list_stays_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "aged-shape.db")
    created = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=created)
    finish_exec_intent(conn, intent_id, status="unknown", result_text="x")
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 1, "status": "FILLED", "executedQty": "100"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, {"code": 0, "msg": "not a list"}),
    }
    sent: list[str] = []
    trade = _client(RecordingTransport(handlers))
    now = created + RECOVER_MAX_AGE_MS + 1
    run_exec_recovery(conn, trade, now, send=lambda t, **_: sent.append(t) or True, sleep=_noop_sleep)
    assert sent == [MSG_RECOVER_STALE_UNREADABLE.format(symbol="DOGEUSDT")]
    assert get_exec_intent(conn, intent_id)["status"] == "unknown"
    run_exec_recovery(conn, trade, now + 1_000, send=lambda t, **_: sent.append(t) or True, sleep=_noop_sleep)
    assert sent == [MSG_RECOVER_STALE_UNREADABLE.format(symbol="DOGEUSDT")]
    conn.close()


def test_stale_open_position_still_blocks_new_entry_and_watch_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releasing the unknown block does not allow another entry on an open, unprotected position."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "stale-guard.db")
    created = 1_700_000_000_000
    intent_id = _entry_intent(conn, ts=created)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (200, {"orderId": 1, "status": "FILLED", "executedQty": "100"}),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    now = created + RECOVER_MAX_AGE_MS + 1
    sent: list[str] = []
    run_exec_recovery(
        conn,
        trade,
        now,
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] == "stale"
    assert symbol_has_in_flight_exec(conn, "DOGEUSDT") is False
    assert any("position ไม่มี stop" in t for t in sent)
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    _seed_doge_short(conn, ts=bar)
    preview = create_entry_preview(
        conn,
        symbol="DOGEUSDT",
        side="short",
        trade=trade,
        now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
    )
    assert preview["ok"] is False
    assert preview["reason"] == "open_position"
    ideas = [
        ExistingIdea(symbol="DOGEUSDT", side="short", risk_usdt=Decimal("1")),
        ExistingIdea(symbol="BTCUSDT", side="long", risk_usdt=Decimal("1")),
    ]
    assert len(ideas) == MAX_OPEN_IDEAS
    candidate = SignalCandidate(
        sized=SizedSignal(
            symbol="ETHUSDT",
            side="long",
            entry=Decimal("1"),
            stop=Decimal("0.9"),
            target=Decimal("1.2"),
            qty=Decimal("1"),
            notional=Decimal("1"),
            leverage=1,
            loss_at_stop=Decimal("0.1"),
            risk_pct=Decimal("1"),
            r_distance=Decimal("0.1"),
            leverage_capped=False,
            uncapped_leverage=1,
            analysis_id=None,
        ),
        confidence=0.8,
        symbol_rank=0,
    )
    admitted, rejected = admit_signals(existing=ideas, candidates=[candidate], balance=Decimal("1000"))
    assert admitted == []
    assert rejected == [candidate]
    day = _ms_bangkok(2026, 11, 2, 12, 0)
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
            "entry_time_ms": day - 3_600_000,
            "no_stop_latched": 0,
            "last_seen_ms": day - 3_600_000,
            "closed_at_ms": None,
        },
    )
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.08"),
        update_time_ms=day - 3_600_000,
    )
    fake = FakeFutures(positions=[pos], marks={"DOGEUSDT": Decimal("0.08")})
    watch_sent: list[str] = []
    first = run_assist_watch(
        conn, futures=fake, now_ms_value=day, send=lambda t, **_: watch_sent.append(t) or True
    )
    assert any(a.get("action") == "no_stop" for a in first["actions"])
    assert len(watch_sent) == 1
    run_assist_watch(
        conn,
        futures=fake,
        now_ms_value=day + 5 * 60_000,
        send=lambda t, **_: watch_sent.append(t) or True,
    )
    assert len(watch_sent) == 1
    conn.close()


def test_recovery_not_found_reset_by_found_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "nf-reset.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    t0 = 1_700_000_000_000
    for offset in (0, 5 * 60_000):
        conn.execute(
            """
            INSERT INTO assist_exec_log (intent_id, step, endpoint, binance_code, created_at_ms)
            VALUES (?, 'recover_not_found', '/fapi/v1/order', -2013, ?)
            """,
            (intent_id, t0 + offset),
        )
    conn.commit()
    found_handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "0", "status": "NEW", "avgPrice": "0"},
        ),
    }
    run_exec_recovery(
        conn,
        _client(RecordingTransport(found_handlers)),
        t0 + 10 * 60_000,
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] != "not_found"
    found_row = conn.execute(
        "SELECT 1 FROM assist_exec_log WHERE intent_id=? AND step='recover_found'",
        (intent_id,),
    ).fetchone()
    assert found_row is not None
    missing: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/order"): (400, {"code": -2013, "msg": "Order does not exist."}),
    }
    run_exec_recovery(
        conn,
        _client(RecordingTransport(missing)),
        t0 + 10 * 60_000 + RECOVER_LEASE_MS,
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] != "not_found"
    conn.close()


def test_recovery_partial_fill_grows_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "partial-grow.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    slr = client_order_id(intent_id, "slr")
    sl0 = client_order_id(intent_id, "sl0")
    state: dict[str, Any] = {"pos": "40", "status": "PARTIALLY_FILLED", "stops": []}

    def position(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        return (200, [_open_pos(amt=f"-{state['pos']}")])

    def order_query(_clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return (
            200,
            {
                "orderId": 1,
                "executedQty": state["pos"],
                "avgPrice": "0.08",
                "status": state["status"],
            },
        )

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        return (
            200,
            [_verified_stop(client_id=cid, qty=qty) for cid, qty in state["stops"]],
        )

    def place(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        cid = str(clean.get("clientAlgoId"))
        qty = str(clean.get("quantity"))
        state["stops"] = [(c, q) for c, q in state["stops"] if c != cid]
        state["stops"].append((cid, qty))
        return (200, {"algoId": 7, "clientAlgoId": cid})

    def delete(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        cid = str(clean.get("clientAlgoId") or "")
        state["stops"] = [(c, q) for c, q in state["stops"] if c != cid]
        return (200, {"algoId": 0, "code": "200", "msg": "success"})

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): position,
        ("GET", "/fapi/v1/order"): order_query,
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("POST", "/fapi/v1/algoOrder"): place,
        ("DELETE", "/fapi/v1/algoOrder"): delete,
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    run_exec_recovery(conn, trade, now_ms(), send=lambda *_a, **_k: True, sleep=_noop_sleep)
    assert get_exec_intent(conn, intent_id)["status"] == "unknown"
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert [(c[2].get("clientAlgoId"), c[2].get("quantity")) for c in stop_posts] == [(slr, "40")]
    state["pos"] = "100"
    run_exec_recovery(conn, trade, now_ms(), send=lambda *_a, **_k: True, sleep=_noop_sleep)
    assert get_exec_intent(conn, intent_id)["status"] == "unknown"
    stop_posts = [
        c
        for c in transport.calls
        if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" and c[2].get("type") == "STOP_MARKET"
    ]
    assert stop_posts[-1][2].get("clientAlgoId") == sl0
    assert stop_posts[-1][2].get("quantity") == "100"
    assert any(c[0] == "DELETE" and c[2].get("clientAlgoId") == slr for c in transport.calls)
    state["status"] = "FILLED"
    run_exec_recovery(conn, trade, now_ms(), send=lambda *_a, **_k: True, sleep=_noop_sleep)
    assert get_exec_intent(conn, intent_id)["status"] == "done"
    conn.close()


def test_recovery_partial_exit_reports_remaining_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "rec-ex-partial.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "stop": "0.083"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-60")]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "executedQty": "40", "avgPrice": "0.07", "status": "PARTIALLY_FILLED"},
        ),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        ts,
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    assert any("เหลือ 60" in t and "ยังไม่ยืนยัน" in t for t in sent)
    assert get_exec_intent(conn, intent_id)["status"] == "unknown"
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_recovery_slr_timeout_then_verifies_no_cls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "slr-to.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    slr = client_order_id(intent_id, "slr")
    oa_n = {"n": 0}

    def place_timeout(_clean: dict[str, Any]) -> BaseException:
        return httpx.ConnectTimeout("slr timeout")

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        oa_n["n"] += 1
        # First read (pre-place verify) empty; post-timeout verify finds -slr.
        if oa_n["n"] >= 2:
            return (200, [_verified_stop(client_id=slr)])
        return (200, [])

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("POST", "/fapi/v1/algoOrder"): place_timeout,
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "done"
    assert not any(
        c[0] == "POST"
        and c[1] == "/fapi/v1/order"
        and c[2].get("origClientOrderId", "").endswith("-cls")
        or (
            c[0] == "POST"
            and c[1] == "/fapi/v1/order"
            and str(c[2].get("newClientOrderId", "")).endswith("-cls")
        )
        for c in transport.calls
    )
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST"
        and c[1] == "/fapi/v1/order"
        and str(c[2].get("newClientOrderId") or "").endswith("-cls")
    ]
    assert cls_posts == []
    conn.close()


def test_recovery_slr_4116_already_verifies_no_cls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "slr-4116.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    slr = client_order_id(intent_id, "slr")
    oa_n = {"n": 0}

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        oa_n["n"] += 1
        if oa_n["n"] >= 2:
            return (200, [_verified_stop(client_id=slr)])
        return (200, [])

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("POST", "/fapi/v1/algoOrder"): (400, {"code": -4116, "msg": "ClientAlgoId already exists"}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] == "done"
    assert not any(
        c[0] == "POST"
        and c[1] == "/fapi/v1/order"
        and str(c[2].get("newClientOrderId") or "").endswith("-cls")
        for c in transport.calls
    )
    conn.close()


def test_recovery_stop_in_open_list_no_placement_log_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "open-list.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    sl0 = client_order_id(intent_id, "sl0")
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [_verified_stop(client_id=sl0)]),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] == "done"
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" for c in transport.calls)
    conn.close()


def test_recovery_open_algo_raise_unverifiable_no_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "oa-raise.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)

    def raise_oa(_clean: dict[str, Any]) -> BaseException:
        return httpx.ConnectError("openAlgo down")

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): raise_oa,
        ("POST", "/fapi/v1/algoOrder"): (200, {"algoId": 1}),
        ("POST", "/fapi/v1/order"): (200, {"orderId": 9, "executedQty": "100", "status": "FILLED"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] == "recovering"
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_recovery_open_algo_dict_unverifiable_no_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "oa-dict.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, {"code": 0, "msg": "ok"}),
        ("POST", "/fapi/v1/algoOrder"): (200, {"algoId": 1}),
        ("POST", "/fapi/v1/order"): (200, {"orderId": 9, "executedQty": "100", "status": "FILLED"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] == "recovering"
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_recovery_verified_from_open_list_no_algo_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "no-algo-get.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    sl0 = client_order_id(intent_id, "sl0")
    # Placement log exists — still must not GET algoOrder when present in open list.
    conn.execute(
        """
        INSERT INTO assist_exec_log (intent_id, step, endpoint, client_order_id, created_at_ms)
        VALUES (?, 'stop', '/fapi/v1/algoOrder', ?, ?)
        """,
        (intent_id, sl0, now_ms()),
    )
    conn.commit()
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [_verified_stop(client_id=sl0)]),
        ("GET", "/fapi/v1/algoOrder"): (500, {"code": -1001, "msg": "should not call"}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] == "done"
    assert not any(c[0] == "GET" and c[1] == "/fapi/v1/algoOrder" for c in transport.calls)
    conn.close()


def test_recovery_empty_open_list_no_log_no_algo_get_places_slr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "empty-place.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    slr = client_order_id(intent_id, "slr")
    placed = {"ok": False}

    def place_slr(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        placed["ok"] = True
        return (200, {"algoId": 99, "clientAlgoId": clean.get("clientAlgoId")})

    def open_algo(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        if placed["ok"]:
            return (200, [_verified_stop(client_id=slr)])
        return (200, [])

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): open_algo,
        ("GET", "/fapi/v1/algoOrder"): (500, {"code": -1001, "msg": "should not call"}),
        ("POST", "/fapi/v1/algoOrder"): place_slr,
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda *_a, **_k: True,
        sleep=_noop_sleep,
    )
    assert placed["ok"] is True
    assert get_exec_intent(conn, intent_id)["status"] == "done"
    assert not any(c[0] == "GET" and c[1] == "/fapi/v1/algoOrder" for c in transport.calls)
    conn.close()


def test_emergency_partial_cls_no_orphan_shows_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "partial-cls.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.083"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-60")]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): _algo_not_found(),
        ("POST", "/fapi/v1/algoOrder"): (400, {"code": -2021, "msg": "Order would immediately trigger."}),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.08", "executedQty": "40", "status": "FILLED"},
        ),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
        ("DELETE", "/fapi/v1/allOpenOrders"): (200, {}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    cls_posts = [
        c
        for c in transport.calls
        if c[0] == "POST"
        and c[1] == "/fapi/v1/order"
        and str(c[2].get("newClientOrderId") or "").endswith("-cls")
    ]
    assert len(cls_posts) == 1
    assert not any(c[0] == "DELETE" for c in transport.calls)
    assert any("🚨" in t and "60" in t for t in sent)
    conn.close()


def test_recovery_exit_not_found_x3_over_15min_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ex-nf.db")
    t0 = 1_700_000_000_000
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=t0,
        expires_at_ms=t0 + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    for offset in (0, 5 * 60_000):
        conn.execute(
            """
            INSERT INTO assist_exec_log (intent_id, step, endpoint, binance_code, created_at_ms)
            VALUES (?, 'recover_not_found', '/fapi/v1/order', -2013, ?)
            """,
            (intent_id, t0 + offset),
        )
    conn.commit()
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 0}),
        ("DELETE", "/fapi/v1/allOpenOrders"): (200, {}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        t0 + 15 * 60_000,
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    row = get_exec_intent(conn, intent_id)
    assert row is not None and row["status"] == "failed"
    assert any("position และ stop ยังเปิดอยู่" in t for t in sent)
    assert not any(c[0] == "DELETE" for c in transport.calls)
    conn.close()


def test_exit_unknown_not_found_open_stays_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ex-unknown.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("POST", "/fapi/v1/order"): httpx.TimeoutException("exit send"),
        ("GET", "/fapi/v1/order"): (400, {"code": -2013, "msg": "Order does not exist."}),
    }
    transport = RecordingTransport(handlers)
    result = run_exit_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert getattr(result, "status", None) == "unknown"
    assert result == MSG_OUTCOME_UNKNOWN
    assert "ปิดไม้ไม่สำเร็จ" not in result
    conn.close()


def test_exit_cancel_failure_warns_then_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "ex-cancel.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    stop_id = "jv-old-sl0"
    deletes = {"n": 0}

    def delete(clean: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        deletes["n"] += 1
        if deletes["n"] == 1:
            return (500, {"code": -1001, "msg": "internal"})
        return (200, {"algoId": 1, "code": "200", "msg": "success"})

    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([_open_pos()]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "1.2"}]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (
            200,
            [
                {
                    "algoId": 5,
                    "clientAlgoId": stop_id,
                    "symbol": "DOGEUSDT",
                    "type": "STOP_MARKET",
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "reduceOnly": True,
                    "stopPrice": "0.083",
                }
            ],
        ),
        ("DELETE", "/fapi/v1/algoOrder"): delete,
    }
    transport = RecordingTransport(handlers)
    result = run_exit_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert str(result).startswith("✅ ปิด")
    assert "ยกเลิก stop/TP เดิมไม่สำเร็จ" in result
    assert stop_id in result
    assert getattr(result, "status", None) == "unknown"
    finish_exec_intent(conn, intent_id, status="unknown", result_text=str(result), expect_status="confirmed")
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        ts,
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    assert get_exec_intent(conn, intent_id)["status"] == "done"
    assert deletes["n"] >= 2
    conn.close()


def test_lost_lease_sends_no_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setattr("jarvise_assist.execute.renew_exec_recovery_lease", lambda *a, **k: False)
    conn = open_db(tmp_path / "lost-lease.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos()]),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("POST", "/fapi/v1/algoOrder"): (200, {"algoId": 1}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    run_exec_recovery(
        conn,
        _client(transport),
        now_ms(),
        send=lambda t, **_: sent.append(t) or True,
        sleep=_noop_sleep,
    )
    assert sent == []
    assert not any(c[0] == "POST" for c in transport.calls)
    assert not any(c[0] == "GET" for c in transport.calls)
    conn.close()


def test_lost_conditional_write_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "lost-write.db")
    intent_id = _entry_intent(conn)
    finish_exec_intent(conn, intent_id, status="unknown", result_text="ก่อนหน้า")
    monkeypatch.setattr("jarvise_assist.execute.finish_exec_intent", lambda *a, **k: False)
    sent: list[str] = []
    _recovery_notify(
        lambda t, **_k: sent.append(t) or True,
        conn,
        intent_id,
        status="done",
        text="ไม่ควรส่ง",
        expect_status="recovering",
    )
    assert sent == []
    conn.close()
