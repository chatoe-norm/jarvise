"""B2 — 50% menu reduce + manual Quick BE (JARVISE_ASSIST_REDUCE)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures
from tests.test_assist_execute import (
    RecordingTransport,
    _client,
    _move_stop_handlers,
    _open_pos,
    _seed_move_stop_watch,
    _verified_stop,
)
from tests.test_assist_menu_close import _long_pos

from jarvise_assist.execute import (
    MOVE_STOP_TTL_MS,
    MSG_MENU_EXIT_EXPIRED,
    MSG_MENU_LIVE_ONLY,
    MSG_MENU_POSITION_CHANGED,
    MSG_OUTCOME_UNKNOWN,
    MSG_PARTIAL_BE_BUFFER,
    MSG_PARTIAL_BE_FLAG_OFF,
    MSG_PARTIAL_BE_NO_STOP,
    MSG_PARTIAL_BE_NOT_TIGHTER,
    MSG_PARTIAL_BE_ORDERS_READ_FAIL,
    MSG_PARTIAL_BE_TICK,
    MSG_REDUCE_DUST,
    MSG_REDUCE_FILTERS_READ_FAIL,
    MSG_REDUCE_FLAG_OFF,
    MSG_STOP_COVERAGE_READ_FAIL,
    PREVIEW_TTL_MS,
    RECOVER_LEASE_MS,
    RECOVER_NOT_FOUND_PASSES,
    BinanceTradeClient,
    _aged_read_snapshot,
    _maybe_offer_after_reduce,
    _quick_be_level,
    assist_reduce_enabled,
    client_order_id,
    half_close_legs,
    handle_exec_callback,
    run_exec_recovery,
    run_reduce_sequence,
)
from jarvise_assist.exit_log import record_intent_action
from jarvise_assist.keyboard import menu_exit_confirm_keyboard
from jarvise_assist.listen import process_update
from jarvise_assist.sizing import round_level_to_tick
from jarvise_assist.store import (
    finish_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
    now_ms,
    upsert_watch_position,
    write_settings,
)
from jarvise_ingest.db import open_db


def _filters(
    *,
    step: str = "1",
    min_qty: str = "1",
    min_notional: str = "5",
    tick: str = "0.00001",
    symbol: str = "DOGEUSDT",
) -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": symbol,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": step, "minQty": min_qty},
                    {"filterType": "MIN_NOTIONAL", "notional": min_notional},
                    {"filterType": "PRICE_FILTER", "tickSize": tick},
                ],
            }
        ]
    }


def _half_handlers(
    *,
    open_amt: str = "-100",
    remain_amt: str = "-50",
    entry: str = "1",
    mark: str = "1",
    open_orders: Any | None = None,
) -> dict[tuple[str, str], Any]:
    remain = [
        {
            "symbol": "DOGEUSDT",
            "positionSide": "BOTH",
            "positionAmt": remain_amt,
            "entryPrice": entry,
            "markPrice": mark,
        }
    ]
    state = {"n": 0}

    def _pos(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        state["n"] += 1
        if state["n"] <= 1:
            return (200, [_open_pos(amt=open_amt, entry=entry, mark=mark)])
        return (200, remain)

    return {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": mark}),
        ("GET", "/fapi/v3/positionRisk"): _pos,
        ("GET", "/fapi/v1/openOrders"): (200, open_orders if open_orders is not None else []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.5"}]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 42, "avgPrice": "0.07", "executedQty": "50", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 42, "avgPrice": "0.07", "executedQty": "50", "status": "FILLED"},
        ),
    }


def test_half_close_math_odd_steps() -> None:
    close, leave, refuse = half_close_legs(
        Decimal("3"),
        step=Decimal("1"),
        min_qty=Decimal("1"),
        min_notional=Decimal("0"),
        mark=Decimal("10"),
    )
    assert refuse is None
    assert close == Decimal("1")
    assert leave == Decimal("2")


def test_half_close_dust_both_sides() -> None:
    close, leave, refuse = half_close_legs(
        Decimal("1"),
        step=Decimal("1"),
        min_qty=Decimal("1"),
        min_notional=Decimal("0"),
        mark=Decimal("10"),
    )
    assert refuse is not None
    assert close == Decimal("0") or leave < Decimal("1")


def test_assist_reduce_flag_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "ON")
    assert assist_reduce_enabled() is True
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "off")
    assert assist_reduce_enabled() is False
    monkeypatch.delenv("JARVISE_ASSIST_REDUCE", raising=False)
    assert assist_reduce_enabled() is False


def test_flag_off_keyboard_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.delenv("JARVISE_ASSIST_REDUCE", raising=False)
    conn = open_db(tmp_path / "b1kb.db")
    write_settings(conn, enabled=True)
    pos = _long_pos("DOGEUSDT", "100")
    fake = FakeFutures(
        positions=[pos],
        orders=[],
        steps={"DOGEUSDT": Decimal("1")},
        min_qty={"DOGEUSDT": Decimal("1")},
        min_notional={"DOGEUSDT": Decimal("5")},
        marks={"DOGEUSDT": Decimal("0.08")},
    )
    sent: list[dict[str, Any]] = []

    def send(text: str, **kw: Any) -> int:
        sent.append({"text": text, **kw})
        return len(sent)

    handle_exec_callback(
        conn,
        "xc:DOGEUSDT:l",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=fake,
        now_ms_value=1,
    )
    assert len(sent) == 1
    markup = sent[0]["reply_markup"]
    intent_id = get_exec_intent(
        conn,
        conn.execute("SELECT id FROM assist_exec_intent WHERE kind='exit'").fetchone()[0],
    )["id"]
    assert markup == menu_exit_confirm_keyboard(intent_id)
    assert "xr:" not in json.dumps(markup)
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent WHERE kind='reduce'").fetchone()[0] == 0
    conn.close()


def test_flag_on_shows_half_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "half.db")
    write_settings(conn, enabled=True)
    pos = _long_pos("DOGEUSDT", "100")
    fake = FakeFutures(
        positions=[pos],
        orders=[],
        steps={"DOGEUSDT": Decimal("1")},
        min_qty={"DOGEUSDT": Decimal("1")},
        min_notional={"DOGEUSDT": Decimal("5")},
        marks={"DOGEUSDT": Decimal("0.08")},
    )
    sent: list[dict[str, Any]] = []

    def send(text: str, **kw: Any) -> int:
        sent.append({"text": text, **kw})
        return 1

    handle_exec_callback(
        conn,
        "xc:DOGEUSDT:l",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=fake,
        now_ms_value=1,
    )
    markup = sent[0]["reply_markup"]
    cbs = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert any(c.startswith("xx:") for c in cbs)
    assert any(c.startswith("xr:") for c in cbs)
    assert any(c.startswith("en:") for c in cbs)
    assert all(len(c) <= 64 for c in cbs)
    assert "✂️ ปิดครึ่ง" in sent[0]["text"]
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent WHERE kind='reduce'").fetchone()[0] == 1
    conn.close()


def test_dust_preview_no_half_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "dust.db")
    write_settings(conn, enabled=True)
    pos = _long_pos("DOGEUSDT", "1")
    fake = FakeFutures(
        positions=[pos],
        orders=[],
        steps={"DOGEUSDT": Decimal("1")},
        min_qty={"DOGEUSDT": Decimal("1")},
        min_notional={"DOGEUSDT": Decimal("5")},
        marks={"DOGEUSDT": Decimal("0.08")},
    )
    sent: list[dict[str, Any]] = []
    handle_exec_callback(
        conn,
        "xc:DOGEUSDT:l",
        send=lambda t, **kw: sent.append({"text": t, **kw}) or 1,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=fake,
        now_ms_value=1,
    )
    cbs = [b["callback_data"] for row in sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(c.startswith("xr:") for c in cbs)
    assert "✂️ ปิดครึ่งไม่ได้" in sent[0]["text"]
    conn.close()


def test_xr_flag_off_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "off")
    conn = open_db(tmp_path / "xr-off.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "0.08",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport({})),
        now_ms_value=ts,
    )
    assert sent == [MSG_REDUCE_FLAG_OFF]
    assert get_exec_intent(conn, rid)["status"] == "preview"
    conn.close()


def test_xr_reduces_once_reduce_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xr.db")
    ts = now_ms()
    exit_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "source": "menu", "confirm_text": "confirm"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "fraction": "0.5",
            "source": "menu",
            "confirm_text": "confirm",
            "exit_intent_id": exit_id,
            "cohort": "quick",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    conn.execute(
        "UPDATE assist_exec_intent SET preview_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "qty": "100",
                    "entry": "1",
                    "source": "menu",
                    "confirm_text": "confirm",
                    "reduce_intent_id": rid,
                }
            ),
            exit_id,
        ),
    )
    conn.commit()
    handlers = _half_handlers()
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    sent: list[dict[str, Any]] = []
    edits: list[Any] = []

    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **kw: sent.append({"text": t, **kw}) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda mid, text, **kw: edits.append((mid, text, kw)),
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 9},
    )
    posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]
    assert len(posts) == 1
    assert posts[0][2].get("reduceOnly") in {"true", "True", True}
    assert Decimal(str(posts[0][2]["quantity"])) == Decimal("50")
    assert get_exec_intent(conn, rid)["status"] == "done"
    assert get_exec_intent(conn, exit_id)["status"] == "cancelled"
    assert "ปิดครึ่ง" in sent[0]["text"]
    # No cancel of SL/TP on successful partial
    cancels = [c for c in transport.calls if c[0] == "DELETE"]
    assert cancels == []
    # Replay
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **kw: sent.append({"text": t, **kw}) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1,
        message={"message_id": 9},
    )
    assert len([c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]) == 1
    conn.close()


def test_en_cancels_both_siblings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "en.db")
    ts = 1
    exit_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="long",
        preview={"qty": "100", "entry": "1", "source": "menu", "confirm_text": "c"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="long",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            "exit_intent_id": exit_id,
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    conn.execute(
        "UPDATE assist_exec_intent SET preview_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "qty": "100",
                    "entry": "1",
                    "source": "menu",
                    "confirm_text": "c",
                    "reduce_intent_id": rid,
                }
            ),
            exit_id,
        ),
    )
    conn.commit()
    handle_exec_callback(
        conn,
        f"en:{exit_id}",
        send=lambda *_a, **_k: None,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, exit_id)["status"] == "cancelled"
    assert get_exec_intent(conn, rid)["status"] == "cancelled"
    conn.close()


def test_xr_expiry_120s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "exp.db")
    ts = 1000
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "0.08",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "confirm",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport({})),
        now_ms_value=ts + PREVIEW_TTL_MS + 1,
        message={"message_id": 1},
    )
    assert MSG_MENU_EXIT_EXPIRED in sent
    assert get_exec_intent(conn, rid)["status"] == "expired"
    conn.close()


def test_crash_after_reduce_market_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "crash.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "0.08",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    handlers = _half_handlers()
    transport = RecordingTransport(handlers)
    trade = _client(transport)

    def boom(*_a: Any, **_k: Any) -> Any:
        insert_exec_log(
            conn,
            intent_id=rid,
            step="reduce_market",
            endpoint="/fapi/v1/order",
            client_order_id=client_order_id(rid, "rd"),
            http_status=200,
            created_at_ms=ts,
        )
        raise RuntimeError("boom after reduce_market")

    monkeypatch.setattr(
        "jarvise_assist.execute.run_reduce_sequence",
        boom,
    )
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "unknown"
    assert MSG_OUTCOME_UNKNOWN in sent
    assert "ยังไม่ได้ส่ง" not in "".join(sent)
    conn.close()


def test_reduce_dust_at_confirm_no_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm-time dust re-check: qty/entry match preview; min_notional makes half illegal."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "dust2.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    # Same snapshot as preview; mark so close*mark < min_notional (50 * 0.05 = 2.5 < 5).
    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters(min_notional="5")),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.05"}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-100", entry="1", mark="0.05")]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    transport = RecordingTransport(handlers)
    sent: list[dict[str, Any]] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **kw: sent.append({"text": t, **kw}) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert not any(c[0] == "POST" for c in transport.calls)
    assert MSG_REDUCE_DUST in sent[0]["text"]
    assert MSG_MENU_POSITION_CHANGED not in sent[0]["text"]
    kb = sent[0].get("reply_markup") or {}
    cbs = [b["callback_data"] for row in kb.get("inline_keyboard", []) for b in row]
    assert any(c.startswith("xc:") for c in cbs)
    conn.close()


def test_c1_be_profit_side() -> None:
    tick = Decimal("0.01")
    long_be = _quick_be_level(side="long", entry=Decimal("100"), tick=tick)
    short_be = _quick_be_level(side="short", entry=Decimal("100"), tick=tick)
    assert long_be is not None and long_be > Decimal("100")
    assert short_be is not None and short_be < Decimal("100")
    # Coarse tick that floors long BE back to entry
    coarse = Decimal("1")
    raw = Decimal("100") * Decimal("1.002")
    rounded = round_level_to_tick(raw, coarse, side="long", kind="stop")
    assert rounded <= Decimal("100")
    assert _quick_be_level(side="long", entry=Decimal("100"), tick=coarse) is None
    # Short stop rounds up: entry 100 / tick 1 → BE lands at entry → refuse
    assert _quick_be_level(side="short", entry=Decimal("100"), tick=Decimal("1")) is None


def test_aged_snapshot_maps_reduce_to_rd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "aged.db")
    ts = 1
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.08", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="x", expect_status="preview")
    # claim as recovering manually
    conn.execute(
        "UPDATE assist_exec_intent SET status='recovering' WHERE id=?",
        (rid,),
    )
    conn.commit()
    intent = get_exec_intent(conn, rid)
    seen: list[str] = []

    class _T:
        def query_order_by_client_id(self, *, symbol: str, client_id: str) -> None:
            seen.append(client_id)
            return None

        def query_algo_by_client_id(self, **_k: Any) -> None:
            raise AssertionError("reduce must not query algo")

        def position_risk(self, **_k: Any) -> list[Any]:
            return []

        def open_orders(self, **_k: Any) -> list[Any]:
            return []

        def open_algo_orders(self, **_k: Any) -> list[Any]:
            return []

        def symbol_filters(self, _s: str) -> Any:
            from jarvise_assist.futures import SymbolFilters

            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

    snap = _aged_read_snapshot(_T(), conn, intent)  # type: ignore[arg-type]
    assert snap is not None
    assert seen == [client_order_id(rid, "rd")]
    conn.close()


def test_recover_reduce_found_open_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "rec.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "50", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-50", entry="1")]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(conn, _client(transport), now_ms_value=ts + 1000)
    row = get_exec_intent(conn, rid)
    assert row["status"] == "done"
    assert "ปิดครึ่ง" in str(row.get("result_text") or "")
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport.calls)
    conn.close()


def test_recover_reduce_found_open_odd_remaining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "rec-odd.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "50", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-40", entry="1")]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(conn, _client(transport), now_ms_value=ts + 1000)
    row = get_exec_intent(conn, rid)
    assert row["status"] == "done"
    assert "ไม่ตรงที่ตั้งใจ" in str(row.get("result_text") or "")
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport.calls)
    conn.close()


def test_recover_reduce_flat_cancels_jv_lists_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "rec-flat.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    jv_sl = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "1.1",
        "quantity": "50",
        "algoId": 11,
        "clientAlgoId": "jv-old-sl",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    owner_sl = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "1.2",
        "quantity": "50",
        "algoId": 99,
        "clientAlgoId": "owner-sl",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [jv_sl, owner_sl]),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(conn, _client(transport), now_ms_value=ts + 1000)
    row = get_exec_intent(conn, rid)
    assert row["status"] == "done"
    text = str(row.get("result_text") or "")
    assert "ปิด" in text
    assert "owner-sl" in text or "คุณตั้งเอง" in text or "1.2" in text
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert any(str(c[2].get("algoId")) == "11" for c in deletes)
    assert not any(str(c[2].get("algoId")) == "99" for c in deletes)
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport.calls)
    conn.close()


def test_recover_reduce_not_found_counts_then_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1: pin n >= RECOVER_NOT_FOUND_PASSES (3); two past-span passes must not fail."""
    from jarvise_assist.execute import RECOVER_NOT_FOUND_PASSES, RECOVER_NOT_FOUND_SPAN_MS

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "rec-nf.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    handlers = {
        ("GET", "/fapi/v1/order"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-100", entry="1")]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    # Pass 1 early; pass 2 after the 15-min span — still below count of 3.
    run_exec_recovery(conn, trade, now_ms_value=ts + 1000)
    assert get_exec_intent(conn, rid)["status"] in {"unknown", "recovering"}
    run_exec_recovery(
        conn,
        trade,
        now_ms_value=ts + RECOVER_NOT_FOUND_SPAN_MS + 60_000,
    )
    assert get_exec_intent(conn, rid)["status"] in {"unknown", "recovering"}
    assert get_exec_intent(conn, rid)["status"] != "failed"
    n = conn.execute(
        "SELECT COUNT(*) FROM assist_exec_log WHERE intent_id=? AND step='recover_not_found'",
        (rid,),
    ).fetchone()[0]
    assert n == RECOVER_NOT_FOUND_PASSES - 1
    # Pass 3 past the 4-min lease, span already exceeded → failed at count 3.
    run_exec_recovery(
        conn,
        trade,
        now_ms_value=ts + RECOVER_NOT_FOUND_SPAN_MS + 60_000 + RECOVER_LEASE_MS + 60_000,
    )
    assert get_exec_intent(conn, rid)["status"] == "failed"
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport.calls)
    conn.close()


def test_unknown_kind_cannot_wedge_recovering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "wedge.db")
    ts = now_ms()
    iid = insert_exec_intent(
        conn,
        kind="bogus_kind",
        symbol="DOGEUSDT",
        side="short",
        preview={},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, iid, status="unknown", result_text="u", expect_status="preview")
    transport = RecordingTransport({})
    run_exec_recovery(conn, _client(transport), now_ms_value=ts + 1000)
    assert get_exec_intent(conn, iid)["status"] == "failed"
    conn.close()


def test_process_update_routes_xr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "off")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    conn = open_db(tmp_path / "listen.db")
    write_settings(conn, enabled=True)
    ts = 1
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="long",
        preview={"qty": "2", "entry": "1", "close_qty": "1", "leave_qty": "1", "source": "menu"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    sent: list[str] = []
    upd = {
        "callback_query": {
            "id": "1",
            "from": {"id": 1},
            "message": {"message_id": 7, "chat": {"id": 99}, "text": "x"},
            "data": f"xr:{rid}",
        }
    }
    process_update(conn, upd, send=lambda t, **_: sent.append(t), futures=FakeFutures(), now_ms_value=ts)
    assert MSG_REDUCE_FLAG_OFF in sent
    conn.close()


def test_partial_be_telemetry_skipped(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tel.db")
    ts = 1
    mid = insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="long",
        preview={"via": "partial_be", "source": "partial_be", "stop": "100.2", "qty": "1"},
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )
    intent = get_exec_intent(conn, mid)
    record_intent_action(conn, intent or {}, action="tap", action_at_ms=ts)
    n = conn.execute("SELECT COUNT(*) FROM assist_exit_response").fetchone()[0]
    assert n == 0
    conn.close()


def test_night_cutoff_kill_do_not_gate_xr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill switch engaged must not block xr: (patch the name execute imports)."""
    import jarvise_assist.execute as ex

    assert "quick_cutoff" not in Path(ex.__file__).read_text(encoding="utf-8")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(ex, "_kill_blocks", lambda: True)
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "night.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    transport = RecordingTransport(_half_handlers())
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "done"
    conn.close()


def test_bot_label_reduce_alone_not_menu_close(tmp_path: Path) -> None:
    from jarvise_assist.quick import _bot_menu_quick_close

    conn = open_db(tmp_path / "label.db")
    ts = 1_000
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="long",
        preview={"cohort": "quick", "qty": "2", "entry": "1", "close_qty": "1", "leave_qty": "1"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    insert_exec_log(
        conn,
        intent_id=rid,
        step="reduce_market",
        endpoint="/fapi/v1/order",
        http_status=200,
        created_at_ms=ts,
    )
    assert (
        _bot_menu_quick_close(
            conn, symbol="DOGEUSDT", side="long", entry_time_ms=ts - 10, closed_at_ms=ts + 10
        )
        is False
    )
    conn.close()


def test_odd_remaining_is_done_not_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "odd.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    # Fill 50 but remaining is 40 (odd)
    handlers = _half_handlers(remain_amt="-40")
    trade = _client(RecordingTransport(handlers))
    from jarvise_assist.store import claim_exec_intent

    assert claim_exec_intent(conn, rid)
    result = run_reduce_sequence(conn, rid, trade=trade)
    assert getattr(result, "status", None) == "done"
    assert "ไม่ตรงที่ตั้งใจ" in str(result)
    conn.close()


# --- Finding 2: offer exception must not erase ✅ ปิดครึ่ง ---


def test_offer_raise_still_sends_reduce_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("planned stop boom")

    monkeypatch.setattr("jarvise_assist.menu.quick_planned_stop", boom)
    conn = open_db(tmp_path / "offer-raise.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            "cohort": "quick",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    # Mark far enough for buffer; no open stops so offer hits quick_planned_stop.
    handlers = _half_handlers(mark="0.99", entry="1")
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "done"
    assert any("ปิดครึ่ง" in t and t.startswith("✅") for t in sent)
    assert MSG_OUTCOME_UNKNOWN not in sent
    conn.close()


# --- Findings 6 / 7: truthful BE refusal lines ---


def test_c1_tick_guard_uses_tick_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "tick-msg.db")
    write_settings(conn, enabled=True)
    ts = now_ms()

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            from jarvise_assist.futures import FuturesPosition

            return [
                FuturesPosition(
                    symbol="DOGEUSDT",
                    position_side="BOTH",
                    position_amt=Decimal("50"),
                    entry_price=Decimal("100"),
                    mark_price=Decimal("101"),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            from jarvise_assist.futures import SymbolFilters

            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("1"),  # coarse → C1 fails
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal("101")

        def all_open_orders(self, **_k: Any) -> list[Any]:
            return []

    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _Trade(),  # type: ignore[arg-type]
        intent={"symbol": "DOGEUSDT", "side": "long"},
        preview={"cohort": "quick"},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง DOGEUSDT",
        now_ms_value=ts,
        send=None,
    )
    assert MSG_PARTIAL_BE_TICK in text
    assert MSG_PARTIAL_BE_NO_STOP not in text
    assert markup is None and deferred is None
    conn.close()


def test_orders_read_fail_no_be_button(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ord-fail.db")
    ts = now_ms()

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            from jarvise_assist.futures import FuturesPosition

            return [
                FuturesPosition(
                    symbol="DOGEUSDT",
                    position_side="BOTH",
                    position_amt=Decimal("-50"),
                    entry_price=Decimal("1"),
                    mark_price=Decimal("0.99"),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            from jarvise_assist.futures import SymbolFilters

            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal("0.99")

        def all_open_orders(self, **_k: Any) -> list[Any]:
            raise RuntimeError("orders boom")

    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _Trade(),  # type: ignore[arg-type]
        intent={"symbol": "DOGEUSDT", "side": "short"},
        preview={"cohort": "quick"},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง DOGEUSDT",
        now_ms_value=ts,
        send=None,
    )
    assert MSG_PARTIAL_BE_ORDERS_READ_FAIL in text
    assert markup is None and deferred is None
    conn.close()


# --- Finding 8: shrink + NOT_FOUND → unknown ---


def test_reduce_shrink_not_found_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "shrink.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    handlers = _half_handlers(remain_amt="-80")
    handlers[("POST", "/fapi/v1/order")] = (
        200,
        {"orderId": 42, "avgPrice": "0", "executedQty": "0", "status": "NEW"},
    )
    handlers[("GET", "/fapi/v1/order")] = (400, {"code": -2013, "msg": "Order does not exist."})
    trade = _client(RecordingTransport(handlers))
    from jarvise_assist.store import claim_exec_intent

    assert claim_exec_intent(conn, rid)
    result = run_reduce_sequence(conn, rid, trade=trade)
    assert getattr(result, "status", None) == "unknown"
    assert MSG_OUTCOME_UNKNOWN in str(result)
    assert "ปิดครึ่งไม่สำเร็จ" not in str(result)
    conn.close()


# --- Finding 10 / 5: main advice after reduce result ---


def test_main_advice_sent_after_reduce_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "main-order.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    entry_ts = ts - 2 * 3_600_000
    _seed_move_stop_watch(conn, entry_ts=entry_ts, symbol="BTCUSDT", side="short", entry="0.08")
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="BTCUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "0.08",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            # no cohort → main path
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    order: list[str] = []

    def track_advice(*_a: Any, **kw: Any) -> Any:
        order.append("advice")
        send_fn = kw.get("send")
        if send_fn is not None:
            send_fn("MOVE_ADVICE_CARD")
        return "mid"

    monkeypatch.setattr("jarvise_assist.execute._send_fresh_move_stop_advice", track_advice)
    monkeypatch.setattr(
        "jarvise_assist.execute.current_trail_advice",
        lambda **_k: (Decimal("0.079"), "trail"),
    )
    state = {"n": 0}

    def _pos(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        state["n"] += 1
        row = {
            "symbol": "BTCUSDT",
            "positionSide": "BOTH",
            "positionAmt": "-100" if state["n"] <= 1 else "-50",
            "entryPrice": "0.08",
            "markPrice": "0.076",
        }
        return (200, [row])

    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v1/exchangeInfo"): (
            200,
            _filters(symbol="BTCUSDT", tick="0.00001", min_notional="0"),
        ),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.076"}),
        ("GET", "/fapi/v3/positionRisk"): _pos,
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (
            200,
            [
                _verified_stop(
                    client_id="jv-old",
                    trigger="0.083",
                    qty="50",
                    symbol="BTCUSDT",
                )
            ],
        ),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.5"}]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 42, "avgPrice": "0.07", "executedQty": "50", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 42, "avgPrice": "0.07", "executedQty": "50", "status": "FILLED"},
        ),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []

    def send(t: str, **_k: Any) -> int:
        order.append("result" if "ปิดครึ่ง" in t else "other")
        sent.append(t)
        return len(sent)

    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "done"
    assert "result" in order
    assert "advice" in order
    assert order.index("result") < order.index("advice")
    conn.close()


def test_main_no_tighter_advice_no_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "no-adv.db")
    ts = now_ms()
    monkeypatch.setattr("jarvise_assist.execute.current_trail_advice", lambda **_k: None)

    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.08,
            "qty": 50.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.083,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            from jarvise_assist.futures import FuturesPosition

            return [
                FuturesPosition(
                    symbol="BTCUSDT",
                    position_side="BOTH",
                    position_amt=Decimal("-50"),
                    entry_price=Decimal("0.08"),
                    mark_price=Decimal("0.076"),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            from jarvise_assist.futures import SymbolFilters

            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal("0.076")

        def all_open_orders(self, **_k: Any) -> list[Any]:
            return []

    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _Trade(),  # type: ignore[arg-type]
        intent={"symbol": "BTCUSDT", "side": "short"},
        preview={},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง BTCUSDT",
        now_ms_value=ts,
        send=lambda *_a, **_k: None,
    )
    assert text == "✅ ปิดครึ่ง BTCUSDT"
    assert markup is None and deferred is None
    conn.close()


def test_outside_universe_non_quick_no_be(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "outside.db")
    ts = now_ms()
    # Seed a watch row so the ASSIST_SYMBOLS gate itself is exercised (not
    # merely "no watch → early return").
    upsert_watch_position(
        conn,
        {
            "symbol": "PEPEUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 1.0,
            "qty": 50.0,
            "in_universe": 0,
            "signal_id": None,
            "initial_stop": 1.05,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )
    # Stub trail advice so removing the ASSIST_SYMBOLS gate would defer a card
    # (tighter than initial 1.05, buffer-ok vs mark 0.99). Without the stub,
    # advice is None and the universe check is not exercised.
    monkeypatch.setattr(
        "jarvise_assist.execute.current_trail_advice",
        lambda **_k: (Decimal("1.02"), "trail"),
    )

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            from jarvise_assist.futures import FuturesPosition

            return [
                FuturesPosition(
                    symbol="PEPEUSDT",
                    position_side="BOTH",
                    position_amt=Decimal("-50"),
                    entry_price=Decimal("1"),
                    mark_price=Decimal("0.99"),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            from jarvise_assist.futures import SymbolFilters

            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal("0.99")

        def all_open_orders(self, **_k: Any) -> list[Any]:
            return []

    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _Trade(),  # type: ignore[arg-type]
        intent={"symbol": "PEPEUSDT", "side": "short"},
        preview={},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง PEPEUSDT",
        now_ms_value=ts,
        send=None,
    )
    assert markup is None and deferred is None
    assert "🛡" not in text
    conn.close()


# --- Finding 14: filters read fail note ---


def test_xc_filters_read_fail_shows_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "filt.db")
    write_settings(conn, enabled=True)
    pos = _long_pos("DOGEUSDT", "100")
    inner = FakeFutures(
        positions=[pos],
        orders=[],
        steps={"DOGEUSDT": Decimal("1")},
        min_qty={"DOGEUSDT": Decimal("1")},
        min_notional={"DOGEUSDT": Decimal("5")},
        marks={"DOGEUSDT": Decimal("0.08")},
    )

    class _Boom:
        def __init__(self, wrapped: Any) -> None:
            object.__setattr__(self, "_w", wrapped)

        def __getattr__(self, name: str) -> Any:
            if name == "symbol_filters":
                raise RuntimeError("filters boom")
            return getattr(self._w, name)

    sent: list[dict[str, Any]] = []
    handle_exec_callback(
        conn,
        "xc:DOGEUSDT:l",
        send=lambda t, **kw: sent.append({"text": t, **kw}) or 1,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=_Boom(inner),
        now_ms_value=1,
    )
    assert MSG_REDUCE_FILTERS_READ_FAIL in sent[0]["text"]
    cbs = [b["callback_data"] for row in sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert any(c.startswith("xx:") for c in cbs)
    assert not any(c.startswith("xr:") for c in cbs)
    conn.close()


# --- Finding 5: flat mid-reduce + coverage lines ---


def test_flat_mid_reduce_lists_owner_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "flat-mid.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            "position_side": "BOTH",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    jv_sl = _verified_stop(client_id="jv-sl", algo_id=11, trigger="1.1", qty="100")
    owner_sl = _verified_stop(client_id="owner-sl", algo_id=99, trigger="1.2", qty="100")
    state = {"n": 0}

    def _pos(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        state["n"] += 1
        if state["n"] <= 1:
            return (200, [_open_pos(amt="-100", entry="1", mark="1")])
        return (200, [])  # flat after market

    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
        ("GET", "/fapi/v3/positionRisk"): _pos,
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [jv_sl, owner_sl]),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "1"}]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 42, "avgPrice": "0.99", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 42, "avgPrice": "0.99", "executedQty": "100", "status": "FILLED"},
        ),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "done"
    body = "\n".join(sent)
    assert "ปิด" in body
    assert "owner" in body.lower() or "คุณตั้งเอง" in body or "1.2" in body
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert any(str(c[2].get("algoId")) == "11" for c in deletes)
    assert not any(str(c[2].get("algoId")) == "99" for c in deletes)
    conn.close()


def test_reduce_coverage_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _reduce_coverage_line
    from jarvise_assist.menu import _NO_STOP_LINE, _partial_stop_line

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "cov.db")

    full_stop = _verified_stop(client_id="jv-sl", trigger="0.9", qty="50")
    partial_stop = _verified_stop(client_id="jv-sl", trigger="0.9", qty="20")

    class _T:
        def __init__(self, orders: list[Any], boom: bool = False) -> None:
            self._orders = orders
            self._boom = boom

        def all_open_orders(self, **_k: Any) -> list[Any]:
            if self._boom:
                raise RuntimeError("boom")
            return list(self._orders)

        def symbol_filters(self, _s: str) -> Any:
            from jarvise_assist.futures import SymbolFilters

            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

    assert "คุมส่วนที่เหลือ" in _reduce_coverage_line(
        _T([full_stop]),  # type: ignore[arg-type]
        symbol="DOGEUSDT",
        side="short",
        position_side="BOTH",
        remaining=Decimal("50"),
    )
    partial_line = _reduce_coverage_line(
        _T([partial_stop]),  # type: ignore[arg-type]
        symbol="DOGEUSDT",
        side="short",
        position_side="BOTH",
        remaining=Decimal("50"),
    )
    assert partial_line == _partial_stop_line(Decimal("20"), Decimal("50"))
    assert (
        _reduce_coverage_line(
            _T([]),  # type: ignore[arg-type]
            symbol="DOGEUSDT",
            side="short",
            position_side="BOTH",
            remaining=Decimal("50"),
        )
        == _NO_STOP_LINE
    )
    assert (
        _reduce_coverage_line(
            _T([], boom=True),  # type: ignore[arg-type]
            symbol="DOGEUSDT",
            side="short",
            position_side="BOTH",
            remaining=Decimal("50"),
        )
        == MSG_STOP_COVERAGE_READ_FAIL
    )
    conn.close()


# --- Findings 1+4: partial_be behaviour ---


def _insert_partial_be(
    conn: Any,
    *,
    ts: int,
    qty: str = "50",
    entry: str = "1",
    stop: str = "0.998",
) -> str:
    return insert_exec_intent(
        conn,
        kind="move_stop",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "stop": stop,
            "qty": qty,
            "entry": entry,
            "tick": "0.00001",
            "via": "partial_be",
            "source": "partial_be",
            "cohort": "quick",
            "advice_text": "advice",
        },
        created_at_ms=ts,
        expires_at_ms=ts + MOVE_STOP_TTL_MS,
    )


def test_partial_be_flag_off_zero_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "off")
    conn = open_db(tmp_path / "pbe-off.db")
    ts = now_ms()
    mid = _insert_partial_be(conn, ts=ts)
    transport = RecordingTransport({})
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{mid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
    )
    assert MSG_PARTIAL_BE_FLAG_OFF in sent
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_partial_be_testnet_live_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "pbe-tn.db")
    ts = now_ms()
    mid = _insert_partial_be(conn, ts=ts)
    transport = RecordingTransport({})
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{mid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
    )
    assert MSG_MENU_LIVE_ONLY in sent
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_partial_be_in_flight_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "pbe-inf.db")
    ts = now_ms()
    mid = _insert_partial_be(conn, ts=ts)
    other = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "50", "entry": "1", "source": "menu"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    finish_exec_intent(conn, other, status="confirmed", result_text="x", expect_status="preview")
    transport = RecordingTransport({})
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{mid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
    )
    assert any("ยังไม่จบ" in t for t in sent)
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_partial_be_snapshot_changed_no_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "pbe-snap.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    mid = _insert_partial_be(conn, ts=ts, qty="50", entry="1")
    handlers = _move_stop_handlers(
        ms_client_id=client_order_id(mid, "ms"),
        amt="-40",
        entry="1",
        mark="0.99",
        new_stop="0.998",
    )
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{mid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert MSG_MENU_POSITION_CHANGED in sent
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/algoOrder" for c in transport.calls)
    conn.close()


def test_partial_be_place_then_cancel_owner_keep_tp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    # Seed a planned stop wider than BE so not-tighter passes when no placed stop initially.
    conn = open_db(tmp_path / "pbe-place.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('reg1', 'DOGEUSDT', 'short', 1.0, 1.05, 50, 'open', ?, ?, ?)
        """,
        (ts, ts + 10_000_000, ts),
    )
    conn.commit()
    mid = _insert_partial_be(conn, ts=ts, qty="50", entry="1", stop="0.998")
    owner_sl = _verified_stop(client_id="owner-sl", algo_id=99, trigger="1.05", qty="50")
    jv_tp = {
        "symbol": "DOGEUSDT",
        "type": "TAKE_PROFIT_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.9",
        "quantity": "50",
        "algoId": 98,
        "clientAlgoId": "jv-tp",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    ms_cid = client_order_id(mid, "ms")
    ms_row = _verified_stop(client_id=ms_cid, algo_id=100, trigger="0.998", qty="50")
    n = {"i": 0}

    def _open(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        n["i"] += 1
        if n["i"] == 1:
            return (200, [owner_sl, jv_tp])
        return (200, [owner_sl, jv_tp, ms_row])

    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): (
            200,
            [_open_pos(amt="-50", entry="1", mark="0.99")],
        ),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.99"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): _open,
        ("GET", "/fapi/v1/algoOrder"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("POST", "/fapi/v1/algoOrder"): (200, {"algoId": 100}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"ms:{mid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
    deletes = [c for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"]
    assert len(posts) == 1
    body = posts[0][2]
    assert body.get("reduceOnly") in {"true", "True", True}
    assert Decimal(str(body["quantity"])) == Decimal("50")
    assert "closePosition" not in body
    assert body.get("type") == "STOP_MARKET"
    # place before cancel
    post_i = next(i for i, c in enumerate(transport.calls) if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder")
    del_i = next(i for i, c in enumerate(transport.calls) if c[0] == "DELETE")
    assert post_i < del_i
    assert any(str(c[2].get("algoId")) == "99" for c in deletes)
    assert not any(str(c[2].get("algoId")) == "98" for c in deletes)
    assert get_exec_intent(conn, mid)["status"] == "done"
    conn.close()


def test_partial_be_no_exit_telemetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "pbe-tel.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('reg1', 'DOGEUSDT', 'short', 1.0, 1.05, 50, 'open', ?, ?, ?)
        """,
        (ts, ts + 10_000_000, ts),
    )
    conn.commit()
    # Tap path
    mid = _insert_partial_be(conn, ts=ts)
    handlers = _move_stop_handlers(
        ms_client_id=client_order_id(mid, "ms"),
        amt="-50",
        entry="1",
        mark="0.99",
        new_stop="0.998",
        old=_verified_stop(client_id="owner-sl", algo_id=99, trigger="1.05", qty="50"),
    )
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"ms:{mid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, mid)["status"] == "done"
    # Decline path
    mid2 = _insert_partial_be(conn, ts=ts + 1)
    handle_exec_callback(
        conn,
        f"mn:{mid2}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport({})),
        now_ms_value=ts + 1,
        message={"message_id": 2},
    )
    # Expiry path
    mid3 = _insert_partial_be(conn, ts=ts + 2)
    handle_exec_callback(
        conn,
        f"ms:{mid3}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport({})),
        now_ms_value=ts + 2 + MOVE_STOP_TTL_MS + 1,
        message={"message_id": 3},
    )
    assert conn.execute("SELECT COUNT(*) FROM assist_exit_response").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assist_exit_card").fetchone()[0] == 0
    conn.close()


def test_recover_partial_be_cancels_owner_keeps_tp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "pbe-rec.db")
    ts = now_ms()
    mid = _insert_partial_be(conn, ts=ts, qty="50", entry="1", stop="0.998")
    finish_exec_intent(conn, mid, status="unknown", result_text="u", expect_status="preview")
    ms_cid = client_order_id(mid, "ms")
    ms_row = _verified_stop(client_id=ms_cid, algo_id=100, trigger="0.998", qty="50")
    owner_sl = _verified_stop(client_id="owner-sl", algo_id=99, trigger="1.05", qty="50")
    jv_tp = {
        "symbol": "DOGEUSDT",
        "type": "TAKE_PROFIT_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "triggerPrice": "0.9",
        "quantity": "50",
        "algoId": 98,
        "clientAlgoId": "jv-tp",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
    }
    handlers = {
        ("GET", "/fapi/v3/positionRisk"): (
            200,
            [_open_pos(amt="-50", entry="1", mark="0.99")],
        ),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.99"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, [ms_row, owner_sl, jv_tp]),
        ("GET", "/fapi/v1/algoOrder"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(conn, _client(transport), now_ms_value=ts + 1000)
    assert get_exec_intent(conn, mid)["status"] == "done"
    deletes = [c for c in transport.calls if c[0] == "DELETE"]
    assert any(str(c[2].get("algoId")) == "99" for c in deletes)
    assert not any(str(c[2].get("algoId")) == "98" for c in deletes)
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_kill_switch_does_not_block_partial_be(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvise_assist.execute as ex

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(ex, "_kill_blocks", lambda: True)
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "pbe-kill.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('reg1', 'DOGEUSDT', 'short', 1.0, 1.05, 50, 'open', ?, ?, ?)
        """,
        (ts, ts + 10_000_000, ts),
    )
    conn.commit()
    mid = _insert_partial_be(conn, ts=ts)
    handlers = _move_stop_handlers(
        ms_client_id=client_order_id(mid, "ms"),
        amt="-50",
        entry="1",
        mark="0.99",
        new_stop="0.998",
        old=_verified_stop(client_id="owner-sl", algo_id=99, trigger="1.05", qty="50"),
    )
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"ms:{mid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, mid)["status"] == "done"
    conn.close()


# --- Finding 12: siblings / stale xx / second card / no algo on reduce / preview dust ---


def test_xr_expiry_cancels_exit_sibling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "sib-exp.db")
    ts = 1000
    exit_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "source": "menu", "confirm_text": "c"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            "exit_intent_id": exit_id,
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    conn.execute(
        "UPDATE assist_exec_intent SET preview_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "qty": "100",
                    "entry": "1",
                    "source": "menu",
                    "confirm_text": "c",
                    "reduce_intent_id": rid,
                }
            ),
            exit_id,
        ),
    )
    conn.commit()
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(RecordingTransport({})),
        now_ms_value=ts + PREVIEW_TTL_MS + 1,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "expired"
    assert get_exec_intent(conn, exit_id)["status"] == "cancelled"
    conn.close()


def test_stale_xx_after_done_xr_no_second_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "stale-xx.db")
    ts = now_ms()
    exit_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "source": "menu",
            "confirm_text": "c",
            "position_side": "BOTH",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            "exit_intent_id": exit_id,
            "cohort": "quick",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    conn.execute(
        "UPDATE assist_exec_intent SET preview_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "qty": "100",
                    "entry": "1",
                    "source": "menu",
                    "confirm_text": "c",
                    "reduce_intent_id": rid,
                    "position_side": "BOTH",
                }
            ),
            exit_id,
        ),
    )
    conn.commit()
    transport = RecordingTransport(_half_handlers())
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "done"
    assert get_exec_intent(conn, exit_id)["status"] == "cancelled"
    posts_before = len([c for c in transport.calls if c[0] == "POST"])
    handle_exec_callback(
        conn,
        f"xx:{exit_id}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts + 1,
        message={"message_id": 1},
    )
    posts_after = len([c for c in transport.calls if c[0] == "POST"])
    assert posts_after == posts_before
    conn.close()


def test_second_confirm_card_cannot_double_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "dbl.db")
    ts = now_ms()
    rid1 = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c1",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    rid2 = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c2",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    transport = RecordingTransport(_half_handlers())
    handle_exec_callback(
        conn,
        f"xr:{rid1}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    posts = len([c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"])
    assert posts == 1
    # Second card: remaining qty 50 ≠ preview 100 → position changed OR in-flight if still open
    handle_exec_callback(
        conn,
        f"xr:{rid2}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts + 1,
        message={"message_id": 2},
    )
    posts2 = len([c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"])
    assert posts2 == 1
    conn.close()


def test_successful_partial_zero_algo_posts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "no-algo.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    transport = RecordingTransport(_half_handlers())
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "done"
    assert not any(c[1] == "/fapi/v1/algoOrder" for c in transport.calls)
    conn.close()


def test_preview_dust_just_below_2x_min_qty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "dust-2x.db")
    write_settings(conn, enabled=True)
    # min_qty 10, step 1, qty 19 → close floor(9.5)=9 < 10 → no half button
    pos = _long_pos("DOGEUSDT", "19")
    fake = FakeFutures(
        positions=[pos],
        orders=[],
        steps={"DOGEUSDT": Decimal("1")},
        min_qty={"DOGEUSDT": Decimal("10")},
        min_notional={"DOGEUSDT": Decimal("0")},
        marks={"DOGEUSDT": Decimal("10")},
    )
    sent: list[dict[str, Any]] = []
    handle_exec_callback(
        conn,
        "xc:DOGEUSDT:l",
        send=lambda t, **kw: sent.append({"text": t, **kw}) or 1,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=fake,
        now_ms_value=1,
    )
    cbs = [b["callback_data"] for row in sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(c.startswith("xr:") for c in cbs)
    assert "✂️ ปิดครึ่งไม่ได้" in sent[0]["text"]
    conn.close()


def test_preview_dust_min_notional_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "dust-notional.db")
    write_settings(conn, enabled=True)
    # qty 100, half 50, mark 0.05 → notional 2.5 < min 5 (xc uses pos.mark_price)
    from jarvise_assist.futures import FuturesPosition

    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.05"),
        update_time_ms=1,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[],
        steps={"DOGEUSDT": Decimal("1")},
        min_qty={"DOGEUSDT": Decimal("1")},
        min_notional={"DOGEUSDT": Decimal("5")},
        marks={"DOGEUSDT": Decimal("0.05")},
    )
    sent: list[dict[str, Any]] = []
    handle_exec_callback(
        conn,
        "xc:DOGEUSDT:l",
        send=lambda t, **kw: sent.append({"text": t, **kw}) or 1,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        read_futures=fake,
        now_ms_value=1,
    )
    cbs = [b["callback_data"] for row in sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(c.startswith("xr:") for c in cbs)
    assert "✂️ ปิดครึ่งไม่ได้" in sent[0]["text"]
    conn.close()


# --- B2 fix2: Medium/Low test gaps + Low-10 main orders-read fail ---


def test_quick_be_offer_e2e_planned_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Q3=b / Q-UI-2=a / Q-BE-3=a: after xr: success, offer BE with planned stop."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "be-offer-e2e.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('reg1', 'DOGEUSDT', 'short', 1.0, 1.05, 50, 'open', ?, ?, ?)
        """,
        (ts, ts + 10_000_000, ts),
    )
    conn.commit()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "fraction": "0.5",
            "source": "menu",
            "confirm_text": "confirm",
            "cohort": "quick",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    handlers = _half_handlers(mark="0.99", entry="1")
    transport = RecordingTransport(handlers)
    sent: list[dict[str, Any]] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **kw: sent.append({"text": t, **kw}) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert get_exec_intent(conn, rid)["status"] == "done"
    assert sent, "expected a reduce result send"
    last = sent[-1]
    markup = last.get("reply_markup")
    assert markup is not None
    flat = [b for row in markup["inline_keyboard"] for b in row]
    cbs = [b["callback_data"] for b in flat]
    assert any(c.startswith("ms:") for c in cbs)
    assert any(c.startswith("mn:") for c in cbs)
    assert any("🛡 เลื่อน stop ส่วนที่เหลือ →" in str(b.get("text") or "") for b in flat)
    move_row = conn.execute(
        "SELECT id, created_at_ms, expires_at_ms, preview_json FROM assist_exec_intent WHERE kind='move_stop'"
    ).fetchone()
    assert move_row is not None
    assert int(move_row[2]) - int(move_row[1]) == MOVE_STOP_TTL_MS
    prev = json.loads(move_row[3])
    assert prev.get("via") == "partial_be"
    assert prev.get("source") == "partial_be"
    assert prev.get("qty") == "50"
    assert prev.get("entry") == "1"
    conn.close()


def _quick_offer_trade(
    *,
    side: str = "short",
    entry: str = "1",
    mark: str = "0.99",
    tick: str = "0.00001",
    amt: str = "-50",
    orders: list[Any] | None = None,
    orders_exc: Exception | None = None,
) -> Any:
    from jarvise_assist.futures import FuturesPosition, SymbolFilters

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            return [
                FuturesPosition(
                    symbol="DOGEUSDT",
                    position_side="BOTH",
                    position_amt=Decimal(amt),
                    entry_price=Decimal(entry),
                    mark_price=Decimal(mark),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal(tick),
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal(mark)

        def all_open_orders(self, **_k: Any) -> list[Any]:
            if orders_exc is not None:
                raise orders_exc
            return list(orders or [])

    return _Trade()


def test_maybe_offer_buffer_no_button(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "offer-buf.db")
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('reg1', 'DOGEUSDT', 'short', 1.0, 1.05, 50, 'open', ?, ?, ?)
        """,
        (ts, ts + 10_000_000, ts),
    )
    conn.commit()
    # Mark too close to BE (~0.998) → buffer fails
    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _quick_offer_trade(mark="0.9975"),  # type: ignore[arg-type]
        intent={"symbol": "DOGEUSDT", "side": "short"},
        preview={"cohort": "quick"},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง DOGEUSDT",
        now_ms_value=ts,
        send=None,
    )
    assert MSG_PARTIAL_BE_BUFFER in text
    assert markup is None and deferred is None
    conn.close()


def test_maybe_offer_not_tighter_no_button(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "offer-nt.db")
    ts = now_ms()
    placed = _verified_stop(client_id="owner-sl", trigger="0.997", qty="50")
    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _quick_offer_trade(mark="0.99", orders=[placed]),  # type: ignore[arg-type]
        intent={"symbol": "DOGEUSDT", "side": "short"},
        preview={"cohort": "quick"},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง DOGEUSDT",
        now_ms_value=ts,
        send=None,
    )
    assert MSG_PARTIAL_BE_NOT_TIGHTER in text
    assert markup is None and deferred is None
    conn.close()


def test_maybe_offer_no_stop_no_button(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "offer-ns.db")
    ts = now_ms()
    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _quick_offer_trade(mark="0.99", orders=[]),  # type: ignore[arg-type]
        intent={"symbol": "DOGEUSDT", "side": "short"},
        preview={"cohort": "quick"},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง DOGEUSDT",
        now_ms_value=ts,
        send=None,
    )
    assert MSG_PARTIAL_BE_NO_STOP in text
    assert markup is None and deferred is None
    conn.close()


def test_partial_be_tap_planned_stop_no_placed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tap uses planned stop when no protective stop is placed (Q-BE-3=a)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "pbe-planned.db")
    write_settings(conn, enabled=True)
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty, status,
            created_at_ms, expires_at_ms, updated_at_ms
        ) VALUES ('reg1', 'DOGEUSDT', 'short', 1.0, 1.05, 50, 'open', ?, ?, ?)
        """,
        (ts, ts + 10_000_000, ts),
    )
    conn.commit()
    mid = _insert_partial_be(conn, ts=ts, qty="50", entry="1", stop="0.998")
    ms_cid = client_order_id(mid, "ms")
    ms_row = _verified_stop(client_id=ms_cid, algo_id=100, trigger="0.998", qty="50")
    n = {"i": 0}

    def _open(_clean: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        n["i"] += 1
        if n["i"] == 1:
            return (200, [])  # no placed stop
        return (200, [ms_row])

    handlers = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): (
            200,
            [_open_pos(amt="-50", entry="1", mark="0.99")],
        ),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "0.99"}),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): _open,
        ("GET", "/fapi/v1/algoOrder"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("POST", "/fapi/v1/algoOrder"): (200, {"algoId": 100}),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }
    transport = RecordingTransport(handlers)
    handle_exec_callback(
        conn,
        f"ms:{mid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    posts = [c for c in transport.calls if c[0] == "POST" and c[1] == "/fapi/v1/algoOrder"]
    assert len(posts) == 1
    assert posts[0][2].get("type") == "STOP_MARKET"
    assert get_exec_intent(conn, mid)["status"] == "done"
    conn.close()


def test_c1_short_offer_coarse_tick(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "c1-short.db")
    ts = now_ms()
    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _quick_offer_trade(side="short", entry="100", mark="99", tick="1", amt="-50"),  # type: ignore[arg-type]
        intent={"symbol": "DOGEUSDT", "side": "short"},
        preview={"cohort": "quick"},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง DOGEUSDT",
        now_ms_value=ts,
        send=None,
    )
    assert MSG_PARTIAL_BE_TICK in text
    assert markup is None and deferred is None
    conn.close()


def test_xr_in_flight_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    monkeypatch.setattr(BinanceTradeClient, "assert_trade_key_safe", lambda self, **_: {"ok": True})
    monkeypatch.setattr(BinanceTradeClient, "assert_account_modes", lambda self: None)
    conn = open_db(tmp_path / "xr-inf.db")
    ts = now_ms()
    other = insert_exec_intent(
        conn,
        kind="exit",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "source": "menu"},
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    finish_exec_intent(conn, other, status="unknown", result_text="u", expect_status="preview")
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
            "cohort": "quick",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    transport = RecordingTransport(_half_handlers())
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert any("ยังไม่จบ" in t for t in sent)
    assert not any(c[0] == "POST" for c in transport.calls)
    assert get_exec_intent(conn, rid)["status"] == "preview"
    conn.close()


def test_xr_testnet_live_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("JARVISE_ASSIST_REDUCE", "on")
    conn = open_db(tmp_path / "xr-tn.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={
            "qty": "100",
            "entry": "1",
            "close_qty": "50",
            "leave_qty": "50",
            "source": "menu",
            "confirm_text": "c",
        },
        created_at_ms=ts,
        expires_at_ms=ts + PREVIEW_TTL_MS,
    )
    transport = RecordingTransport(_half_handlers())
    sent: list[str] = []
    handle_exec_callback(
        conn,
        f"xr:{rid}",
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=_client(transport),
        now_ms_value=ts,
        message={"message_id": 1},
    )
    assert MSG_MENU_LIVE_ONLY in sent
    assert not any(c[0] == "POST" for c in transport.calls)
    conn.close()


def test_recover_reduce_wrong_side_done_odd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "rec-side.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0.07", "executedQty": "50", "status": "FILLED"},
        ),
        # Open position on the other side (long)
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="50", entry="1")]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(conn, _client(transport), now_ms_value=ts + 1000)
    row = get_exec_intent(conn, rid)
    assert row["status"] == "done"
    assert "ไม่ตรงที่ตั้งใจ" in str(row.get("result_text") or "")
    conn.close()


def test_recover_reduce_found_not_filled_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "rec-nf-exp.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    handlers = {
        ("GET", "/fapi/v1/order"): (
            200,
            {"orderId": 1, "avgPrice": "0", "executedQty": "0", "status": "EXPIRED"},
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-100", entry="1")]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    run_exec_recovery(conn, _client(transport), now_ms_value=ts + 1000)
    assert get_exec_intent(conn, rid)["status"] == "failed"
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport.calls)
    conn.close()


def test_recover_reduce_not_found_inside_span_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "rec-nf-span.db")
    ts = now_ms()
    rid = insert_exec_intent(
        conn,
        kind="reduce",
        symbol="DOGEUSDT",
        side="short",
        preview={"qty": "100", "entry": "1", "close_qty": "50", "leave_qty": "50"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, rid, status="unknown", result_text="u", expect_status="preview")
    handlers = {
        ("GET", "/fapi/v1/order"): (400, {"code": -2013, "msg": "Order does not exist."}),
        ("GET", "/fapi/v3/positionRisk"): (200, [_open_pos(amt="-100", entry="1")]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _filters()),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "1"}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    # Space passes past the 4-min recovery lease so each pass can write
    # recover_not_found; last pass is ~10 min, still inside the 15-min span.
    for i in range(RECOVER_NOT_FOUND_PASSES):
        run_exec_recovery(
            conn,
            trade,
            now_ms_value=ts + 1000 + i * (RECOVER_LEASE_MS + 60_000),
        )
    n = conn.execute(
        "SELECT COUNT(*) FROM assist_exec_log WHERE intent_id=? AND step='recover_not_found'",
        (rid,),
    ).fetchone()[0]
    assert n == RECOVER_NOT_FOUND_PASSES
    assert get_exec_intent(conn, rid)["status"] != "failed"
    assert get_exec_intent(conn, rid)["status"] in {"unknown", "recovering"}
    conn.close()


def test_maybe_offer_long_not_tighter_no_button(tmp_path: Path) -> None:
    """F3: Quick long-side not-tighter (execute.py:1014) must suppress the BE button."""
    conn = open_db(tmp_path / "offer-nt-long.db")
    ts = now_ms()
    placed = _verified_stop(client_id="owner-sl", trigger="1.003", qty="50", side="SELL")
    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _quick_offer_trade(
            side="long",
            entry="1",
            mark="1.01",
            amt="50",
            orders=[placed],
        ),  # type: ignore[arg-type]
        intent={"symbol": "DOGEUSDT", "side": "long"},
        preview={"cohort": "quick"},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง DOGEUSDT",
        now_ms_value=ts,
        send=None,
    )
    assert MSG_PARTIAL_BE_NOT_TIGHTER in text
    assert markup is None and deferred is None
    conn.close()


def test_main_advice_not_tighter_no_deferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "main-nt.db")
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.08,
            "qty": 50.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.083,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )
    # Advice worse than placed stop (short: higher = not tighter)
    monkeypatch.setattr(
        "jarvise_assist.execute.current_trail_advice",
        lambda **_k: (Decimal("0.080"), "trail"),
    )
    from jarvise_assist.futures import FuturesPosition, SymbolFilters

    placed = _verified_stop(client_id="jv-old", trigger="0.079", qty="50", symbol="BTCUSDT")

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            return [
                FuturesPosition(
                    symbol="BTCUSDT",
                    position_side="BOTH",
                    position_amt=Decimal("-50"),
                    entry_price=Decimal("0.08"),
                    mark_price=Decimal("0.076"),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal("0.076")

        def all_open_orders(self, **_k: Any) -> list[Any]:
            return [placed]

    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _Trade(),  # type: ignore[arg-type]
        intent={"symbol": "BTCUSDT", "side": "short"},
        preview={},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง BTCUSDT",
        now_ms_value=ts,
        send=lambda *_a, **_k: None,
    )
    assert text == "✅ ปิดครึ่ง BTCUSDT"
    assert markup is None and deferred is None
    conn.close()


def test_main_advice_long_not_tighter_no_deferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F3: main long-side not-tighter (execute.py:1070) must not defer advice."""
    conn = open_db(tmp_path / "main-nt-long.db")
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "long",
            "entry": 0.08,
            "qty": 50.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.077,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )
    # Advice worse than placed stop (long: lower = not tighter)
    monkeypatch.setattr(
        "jarvise_assist.execute.current_trail_advice",
        lambda **_k: (Decimal("0.079"), "trail"),
    )
    from jarvise_assist.futures import FuturesPosition, SymbolFilters

    placed = _verified_stop(
        client_id="jv-old",
        trigger="0.0795",
        qty="50",
        side="SELL",
        symbol="BTCUSDT",
    )

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            return [
                FuturesPosition(
                    symbol="BTCUSDT",
                    position_side="BOTH",
                    position_amt=Decimal("50"),
                    entry_price=Decimal("0.08"),
                    mark_price=Decimal("0.084"),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal("0.084")

        def all_open_orders(self, **_k: Any) -> list[Any]:
            return [placed]

    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _Trade(),  # type: ignore[arg-type]
        intent={"symbol": "BTCUSDT", "side": "long"},
        preview={},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง BTCUSDT",
        now_ms_value=ts,
        send=lambda *_a, **_k: None,
    )
    assert text == "✅ ปิดครึ่ง BTCUSDT"
    assert markup is None and deferred is None
    conn.close()


def test_main_advice_buffer_blocks_deferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "main-buf.db")
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.08,
            "qty": 50.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.083,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )
    monkeypatch.setattr(
        "jarvise_assist.execute.current_trail_advice",
        lambda **_k: (Decimal("0.079"), "trail"),
    )
    from jarvise_assist.futures import FuturesPosition, SymbolFilters

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            return [
                FuturesPosition(
                    symbol="BTCUSDT",
                    position_side="BOTH",
                    position_amt=Decimal("-50"),
                    entry_price=Decimal("0.08"),
                    mark_price=Decimal("0.07895"),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal("0.07895")

        def all_open_orders(self, **_k: Any) -> list[Any]:
            return []

    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _Trade(),  # type: ignore[arg-type]
        intent={"symbol": "BTCUSDT", "side": "short"},
        preview={},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง BTCUSDT",
        now_ms_value=ts,
        send=lambda *_a, **_k: None,
    )
    assert markup is None and deferred is None
    conn.close()


def test_main_orders_read_fail_no_advice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Low-10: main-path open-orders read fail must not defer advice."""
    conn = open_db(tmp_path / "main-ord-fail.db")
    ts = now_ms()
    upsert_watch_position(
        conn,
        {
            "symbol": "BTCUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.08,
            "qty": 50.0,
            "in_universe": 1,
            "signal_id": None,
            "initial_stop": 0.083,
            "initial_stop_source": "signal",
            "entry_time_ms": ts,
            "no_stop_latched": 0,
            "last_seen_ms": ts,
            "closed_at_ms": None,
        },
    )
    monkeypatch.setattr(
        "jarvise_assist.execute.current_trail_advice",
        lambda **_k: (Decimal("0.079"), "trail"),
    )
    from jarvise_assist.futures import FuturesPosition, SymbolFilters

    class _Trade:
        def position_risk(self, **_k: Any) -> list[Any]:
            return [
                FuturesPosition(
                    symbol="BTCUSDT",
                    position_side="BOTH",
                    position_amt=Decimal("-50"),
                    entry_price=Decimal("0.08"),
                    mark_price=Decimal("0.076"),
                    update_time_ms=1,
                )
            ]

        def symbol_filters(self, _s: str) -> Any:
            return SymbolFilters(
                step_size=Decimal("1"),
                min_qty=Decimal("1"),
                min_notional=Decimal("5"),
                tick_size=Decimal("0.00001"),
            )

        def mark_price(self, _s: str) -> Decimal:
            return Decimal("0.076")

        def all_open_orders(self, **_k: Any) -> list[Any]:
            raise RuntimeError("orders boom")

    text, markup, deferred = _maybe_offer_after_reduce(
        conn,
        _Trade(),  # type: ignore[arg-type]
        intent={"symbol": "BTCUSDT", "side": "short"},
        preview={},
        reduce_intent_id="r1",
        result_text="✅ ปิดครึ่ง BTCUSDT",
        now_ms_value=ts,
        send=lambda *_a, **_k: None,
    )
    assert text == "✅ ปิดครึ่ง BTCUSDT"
    assert markup is None and deferred is None
    conn.close()
