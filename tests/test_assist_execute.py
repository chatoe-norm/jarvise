"""Owner-confirm assist execution — mock HTTP only, no network."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis

from jarvise_assist.execute import (
    MSG_ALREADY_CLOSED,
    MSG_EXPIRED,
    MSG_KILL,
    MSG_MARK_PASSED,
    MSG_OFF,
    MSG_STOP_CLOSED,
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
    size_for_execution,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.listen import process_update
from jarvise_assist.store import (
    claim_exec_intent,
    finish_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_watch_advice,
    now_ms,
)
from jarvise_assist.watch import run_assist_watch
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
    assert build_trade_client() is None


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


def _entry_intent(conn: Any, *, ts: int | None = None) -> str:
    t = int(ts if ts is not None else now_ms())
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
    }
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
    assert result == "เข้าไม้ไม่สำเร็จ"
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
            [{"algoId": 11, "clientAlgoId": scid, "symbol": "DOGEUSDT"}],
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
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert result.startswith("✅")
    algo_posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
    assert len(algo_posts) == 3
    conn.close()


def test_stop_fails_close_fails_close_yourself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "sl_cls.db")
    intent_id = _entry_intent(conn)
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("POST", "/fapi/v1/marginType"): (200, {"msg": "ok"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08"}),
            (400, {"code": -2010, "msg": "close reject"}),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            (400, {"code": -2010, "msg": "reject"}),
            (400, {"code": -2010, "msg": "reject"}),
        ],
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
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    result = run_entry_sequence(conn, intent_id, trade=trade)
    assert result == "⚠️ วาง stop ไม่ได้และปิด DOGEUSDT เองทันที"
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
    assert any("DOGEUSDT" in t and "เช็ค" in t for t in sent)
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
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/exchangeInfo"): (200, _exchange_info()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.08"}),
        ("POST", "/fapi/v1/marginType"): (200, {"msg": "ok"}),
        ("POST", "/fapi/v1/leverage"): (200, {"leverage": 5}),
        ("POST", "/fapi/v1/order"): [
            (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08"}),
        ],
        ("POST", "/fapi/v1/algoOrder"): [
            (200, {"algoId": 11}),
            (200, {"algoId": 12}),
        ],
    }
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
        return None

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
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: None)
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t, **_: True)
    n = conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0]
    assert n == 0
    conn.close()
