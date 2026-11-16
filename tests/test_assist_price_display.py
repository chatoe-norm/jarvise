"""Owner-facing price display: strip float noise and round stops to tick."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_candle
from tests.test_assist_execute import (
    RecordingTransport,
    _attach_verified_sl0,
    _client,
    _exchange_info,
    _noop_sleep,
    _open_pos,
    _open_then_flat,
    _seed_doge_short,
)

from jarvise_assist.execute import (
    create_entry_preview,
    reset_trade_key_cache,
    run_entry_sequence,
    run_exit_sequence,
)
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.messages import (
    fmt_price_display,
    format_no_stop_warning,
    format_partial_stop_warning,
)
from jarvise_assist.store import claim_exec_intent, insert_exec_intent, upsert_watch_position
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db


def test_fmt_price_display_strips_float_noise() -> None:
    assert fmt_price_display(0.9436999999999999) == "0.9437"
    assert fmt_price_display(0.9436999999999999, Decimal("0.0001")) == "0.9437"
    assert fmt_price_display(Decimal("0.94370000")) == "0.9437"
    assert fmt_price_display(0.00001234) == "0.00001234"
    assert fmt_price_display(97123.45) == "97123.45"


def test_no_stop_warning_rounds_stop_to_tick_directionally() -> None:
    stop = Decimal("0.17475")
    short = format_no_stop_warning(
        symbol="ADAUSDT",
        side="short",
        stop=stop,
        tick=Decimal("0.0001"),
    )
    assert short.endswith("ที่ 0.1748")
    long = format_no_stop_warning(
        symbol="ADAUSDT",
        side="long",
        stop=stop,
        tick=Decimal("0.0001"),
    )
    assert long.endswith("ที่ 0.1747")
    noisy = format_no_stop_warning(
        symbol="XRPUSDT",
        side="short",
        stop=Decimal(str(0.9436999999999999)),
    )
    assert noisy.endswith("ที่ 0.9437")
    assert "0.9436999999999999" not in noisy


def test_partial_stop_warning_rounds_stop_to_tick() -> None:
    text = format_partial_stop_warning(
        symbol="ADAUSDT",
        side="short",
        covered=Decimal("50"),
        position_qty=Decimal("100"),
        stop=Decimal("0.17475"),
        tick=Decimal("0.0001"),
    )
    assert "50/100" in text
    assert text.endswith("ที่ 0.1748")


def test_watch_no_stop_warning_uses_symbol_tick(tmp_path: Path) -> None:
    day = _ms_bangkok(2026, 11, 14, 12, 0)
    entry_ms = day - 3_600_000
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.1673"),
        mark_price=Decimal("0.1650"),
        update_time_ms=entry_ms,
    )
    conn = open_db(tmp_path / "tick.db")
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=day - 8 * 3_600_000,
        open_=0.17,
        high=0.18,
        low=0.16,
        close=0.167,
        atr=0.005,
    )
    upsert_watch_position(
        conn,
        {
            "symbol": "ADAUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.1673,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": "sig1",
            "initial_stop": 0.17475,
            "initial_stop_source": "signal",
            "entry_time_ms": entry_ms,
            "no_stop_latched": 0,
            "last_seen_ms": entry_ms,
            "closed_at_ms": None,
        },
    )
    sent: list[str] = []
    fake = FakeFutures(
        positions=[pos],
        orders=[],
        ticks={"ADAUSDT": Decimal("0.0001")},
    )
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert any(t.endswith("ที่ 0.1748") for t in sent)
    conn.close()

    broken = open_db(tmp_path / "broken.db")
    _seed_candle(
        broken,
        symbol="ADAUSDT",
        ts=day - 8 * 3_600_000,
        open_=0.17,
        high=0.18,
        low=0.16,
        close=0.167,
        atr=0.005,
    )
    upsert_watch_position(
        broken,
        {
            "symbol": "ADAUSDT",
            "position_side": "BOTH",
            "side": "short",
            "entry": 0.1673,
            "qty": 100.0,
            "in_universe": 1,
            "signal_id": "sig1",
            "initial_stop": 0.17475,
            "initial_stop_source": "signal",
            "entry_time_ms": entry_ms,
            "no_stop_latched": 0,
            "last_seen_ms": entry_ms,
            "closed_at_ms": None,
        },
    )

    class BrokenTick(FakeFutures):
        def symbol_filters(self, symbol: str) -> Any:
            raise RuntimeError("tick down")

    sent2: list[str] = []
    run_assist_watch(
        broken,
        futures=BrokenTick(positions=[pos], orders=[]),
        now_ms_value=day,
        send=lambda t: sent2.append(t) or True,
    )
    warn = next(t for t in sent2 if "ไม่มี stop order" in t)
    # Failed lookup must not force the 0.01 progress/trail fallback (2 decimals).
    assert not warn.endswith("ที่ 0.17")
    assert warn.endswith("ที่ 0.17475")
    broken.close()


def test_exit_result_text_strips_trailing_zeros(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "exit.db")
    ts = _ms_bangkok(2026, 11, 14, 12, 0)
    intent_id = insert_exec_intent(
        conn,
        kind="exit",
        symbol="ADAUSDT",
        side="short",
        preview={"qty": "100", "entry": "0.1673"},
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, intent_id)
    live = {
        "symbol": "ADAUSDT",
        "positionSide": "BOTH",
        "positionAmt": "-100",
        "entryPrice": "0.1673",
        "markPrice": "0.1636",
    }
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v1/positionSide/dual"): (200, {"dualSidePosition": False}),
        ("GET", "/fapi/v1/multiAssetsMargin"): (200, {"multiAssetsMargin": False}),
        ("GET", "/fapi/v3/positionRisk"): _open_then_flat([live]),
        ("POST", "/fapi/v1/order"): (
            200,
            {"orderId": 9, "avgPrice": "0.16360000", "executedQty": "100", "status": "FILLED"},
        ),
        ("GET", "/fapi/v1/userTrades"): (200, [{"realizedPnl": "0.37"}]),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
    }
    transport = RecordingTransport(handlers)
    result = run_exit_sequence(conn, intent_id, trade=_client(transport), sleep=_noop_sleep)
    assert result.startswith("✅ ปิด ADAUSDT @ 0.1636")
    posts = [call for call in transport.calls if call[0] == "POST" and call[1] == "/fapi/v1/order"]
    assert len(posts) == 1
    assert posts[0][2]["quantity"] == "100"
    assert posts[0][2]["reduceOnly"] == "true"
    conn.close()


def test_entry_result_prices_at_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    reset_trade_key_cache()
    conn = open_db(tmp_path / "entry-tick.db")
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
        ("POST", "/fapi/v1/order"): (200, {"orderId": 1, "executedQty": "100", "avgPrice": "0.08"}),
        ("POST", "/fapi/v1/algoOrder"): [(200, {"algoId": 11}), (200, {"algoId": 12})],
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/algoOrder"): (400, {"code": -2013, "msg": "Order does not exist."}),
    }
    transport = RecordingTransport(handlers)
    trade = _client(transport)
    preview = create_entry_preview(
        conn,
        symbol="DOGEUSDT",
        side="short",
        trade=trade,
        now_ms_value=_ms_bangkok(2026, 11, 2, 8, 20),
    )
    assert preview["ok"] is True
    intent_id = preview["intent_id"]
    assert claim_exec_intent(conn, intent_id)
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
    handlers[("POST", "/fapi/v1/algoOrder")] = [(200, {"algoId": 11}), (200, {"algoId": 12})]
    _attach_verified_sl0(handlers, intent_id)
    result = run_entry_sequence(conn, intent_id, trade=trade, sleep=_noop_sleep)
    assert "fill 0.08000 ×" in result
    assert "stop 0.08300 · TP " in result
    conn.close()
