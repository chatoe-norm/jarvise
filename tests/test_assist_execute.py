"""Owner-confirm assist execution — mock HTTP only, no network."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from tests.test_assist import _ms_bangkok, _seed_analysis

from jarvise_assist.execute import (
    MSG_ALREADY_CLOSED,
    MSG_EXPIRED,
    MSG_KILL,
    MSG_MARK_PASSED,
    MSG_OFF,
    BinanceTradeClient,
    assist_execute_mode,
    build_trade_client,
    cancel_jv_orders_for_symbol,
    client_order_id,
    create_entry_preview,
    handle_exec_callback,
    reset_trade_key_cache,
    run_entry_sequence,
    run_exit_sequence,
    run_move_stop_sequence,
)
from jarvise_assist.listen import process_update
from jarvise_assist.store import (
    claim_exec_intent,
    finish_exec_intent,
    insert_exec_intent,
    now_ms,
)
from jarvise_exchange.binance_spot import BinanceAuth
from jarvise_ingest.db import SCHEMA_VERSION, open_db


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
            status, body = script(clean)
        elif isinstance(script, list):
            status, body = script.pop(0)
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
    assert SCHEMA_VERSION >= 18
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
    # Rebind order handler for entry market + possibly emergency (not used)
    handlers[("POST", "/fapi/v1/order")] = [
        (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08", "status": "FILLED"}),
    ]
    handlers[("POST", "/fapi/v1/algoOrder")] = [
        (200, {"algoId": 11}),
        (200, {"algoId": 12}),
    ]
    # Fresh mark for stop check
    handlers[("GET", "/fapi/v1/premiumIndex")] = (200, {"markPrice": "0.08"})
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
    assert MSG_EXPIRED in (edited or sent)
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
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert "ปิดไม้แล้ว" in result
    posts = [p for m, p, _ in transport.calls if m == "POST"]
    assert posts.count("/fapi/v1/order") == 2  # entry + emergency close
    conn.close()


def test_move_stop_place_before_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "move.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
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
    }
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): (
            200,
            [
                {
                    "symbol": "DOGEUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "-100",
                    "entryPrice": "0.08",
                    "markPrice": "0.078",
                }
            ],
        ),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.078"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [old]),
        ("POST", "/fapi/v1/algoOrder"): (200, {"algoId": 100}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"algoId": 99}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_move_stop_sequence(conn, intent_id, trade=trade)
    assert result.startswith("✅")
    seq = [(m, p) for m, p, _ in transport.calls if m in {"POST", "DELETE"}]
    assert seq[0] == ("POST", "/fapi/v1/algoOrder")
    assert seq[1] == ("DELETE", "/fapi/v1/algoOrder")
    conn.close()


def test_move_stop_mark_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "passed.db")
    ts = now_ms()
    intent_id = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={"stop": "0.079", "qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    handlers: dict[tuple[str, str], Any] = {
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
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "1000", "availableBalance": "1000"}],
        ),
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    sent: list[str] = []
    markups: list[Any] = []

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
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1000,
    )
    assert any(MSG_MARK_PASSED in t for t in sent)
    assert markups and any("xx:" in json.dumps(m) for m in markups)
    assert not any(c[0] == "POST" for c in transport.calls)
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
        preview={"qty": "100"},
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
        preview={"qty": "100"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent2)
    handlers[("GET", "/fapi/v3/positionRisk")] = (
        200,
        [
            {
                "symbol": "DOGEUSDT",
                "positionSide": "BOTH",
                "positionAmt": "-100",
                "entryPrice": "0.08",
                "markPrice": "0.07",
            }
        ],
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
    assert build_trade_client() is None


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
    cancelled = cancel_jv_orders_for_symbol(trade, "DOGEUSDT")
    assert cancelled == ["1"]
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0][2].get("algoId") == "1"


def test_client_order_id_prefix() -> None:
    cid = client_order_id("aei_abcdef0123456789", "sl0")
    assert cid.startswith("jv-")
    assert len(cid) <= 36
