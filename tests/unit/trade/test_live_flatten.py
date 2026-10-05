"""Kill-switch live flatten (mocked venue): preconditions, cancel→sell, idempotency, fallbacks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from jarvise_ingest.db import ensure_paper_account, get_live_order, insert_live_order, open_db
from jarvise_notify import format_live_flatten_message
from jarvise_trade.binance_market import SymbolFilters
from jarvise_trade.cli import trade_app
from jarvise_trade.flatten import flatten_client_order_id, flatten_live_positions, live_risk_tick

ENGAGED = {"engaged": True, "known": True, "reason": "manual", "error": None}
CANCELED = {"status": "CANCELED", "executedQty": "0", "cummulativeQuoteQty": "0"}
SOLD = {"orderId": 9, "status": "FILLED", "executedQty": "0.00999", "cummulativeQuoteQty": "1008.99"}
BTC_FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    base_asset="BTC",
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.01"),
)


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "s")
    db = open_db(tmp_path / "flat.db")
    ensure_paper_account(db)
    yield db
    db.close()


@pytest.fixture
def venue(
    venue_sizing: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, MagicMock]]:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    venue_sizing["filters"].side_effect = None
    venue_sizing["filters"].return_value = BTC_FILTERS
    venue_sizing["free"].return_value = Decimal("0.00999")
    with (
        patch("jarvise_trade.flatten.kill_switch_state", return_value=ENGAGED) as ks,
        patch("jarvise_notify.telegram.send_telegram_message", return_value=True) as notify,
        patch("jarvise_trade.submit.cancel_order", return_value=CANCELED) as cancel,
        patch("jarvise_trade.submit.query_order", return_value=None) as query,
        patch("jarvise_trade.submit.place_spot_market_order", return_value=SOLD) as place,
        patch(
            "jarvise_trade.submit.get_approval", return_value={"id": "open1", "invalidation_price": 97000.0}
        ),
    ):
        yield {**venue_sizing, "ks": ks, "notify": notify, "cancel": cancel, "query": query, "place": place}


def _seed_position(conn: Any, *, qty: float = 0.01, quote: float = 1000.0, with_stop: bool = True) -> None:
    insert_live_order(
        conn,
        {
            "id": "buy1",
            "created_at_ms": 1,
            "approval_id": "open1",
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "status": "filled",
            "venue_status": "FILLED",
            "executed_qty": qty,
            "cummulative_quote_qty": quote,
            "client_order_id": "jrv-open1",
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "realized_pnl_usd": 0.0,
        },
    )
    if with_stop:
        insert_live_order(
            conn,
            {
                "id": "stop1",
                "created_at_ms": 2,
                "approval_id": "open1",
                "venue": "binance",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "order_type": "STOP_LOSS_LIMIT",
                "status": "submitted",
                "venue_status": "NEW",
                "client_order_id": "jrv-xopen1",
                "kill_switch_clear": 1,
                "caps_ok": 1,
            },
        )


def test_flag_off_makes_no_http(
    conn: Any, venue: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "false")
    _seed_position(conn)
    result = flatten_live_positions(conn, now_ms=10_000)
    assert result["ok"] is False
    assert result["ran"] is False
    assert result["skipped_reason"] == "live_trading_disabled"
    for name in ("cancel", "query", "place", "filters", "free", "notify"):
        assert venue[name].call_count == 0, name


@pytest.mark.parametrize(
    ("state", "ok", "skipped"),
    [
        (
            {"engaged": True, "known": False, "reason": None, "error": "redis down"},
            False,
            "kill_switch_unknown",
        ),
        ({"engaged": False, "known": True, "reason": None, "error": None}, True, "kill_switch_clear"),
    ],
)
def test_unknown_or_clear_kill_switch_never_sells(
    conn: Any, venue: dict[str, MagicMock], state: dict, ok: bool, skipped: str
) -> None:
    _seed_position(conn)
    venue["ks"].return_value = state
    result = flatten_live_positions(conn, now_ms=10_000)
    assert result["ok"] is ok
    assert result["ran"] is False
    assert str(result["skipped_reason"]).startswith(skipped)
    assert venue["cancel"].call_count == 0
    assert venue["place"].call_count == 0


def test_cancels_own_stop_then_sells_fee_net_floored(conn: Any, venue: dict[str, MagicMock]) -> None:
    _seed_position(conn)
    result = flatten_live_positions(conn, now_ms=10_000)

    assert result["ok"] is True and result["ran"] is True
    assert venue["cancel"].call_args.kwargs["orig_client_order_id"] == "jrv-xopen1"
    kwargs = venue["place"].call_args.kwargs
    assert kwargs["side"] == "SELL"
    assert kwargs["quantity"] == Decimal("0.00999")
    assert kwargs["new_client_order_id"] == flatten_client_order_id("buy1") == "jrv-fbuy1"
    assert result["cancelled"] == ["stop1"]
    assert get_live_order(conn, "stop1")["status"] == "canceled"

    sell = get_live_order(conn, result["symbols"][0]["live_order_id"])
    assert sell["approval_id"] is None
    assert sell["order_type"] == "MARKET"
    assert sell["kill_switch_clear"] == 0
    assert abs(float(sell["realized_pnl_usd"]) - (1008.99 - 999.0)) < 1e-6
    assert result["sold"] == [
        {"symbol": "BTCUSDT", "qty": 0.00999, "status": "filled", "client_order_id": "jrv-fbuy1"}
    ]
    venue["notify"].assert_called_once()


def test_rerun_never_posts_a_second_sell(conn: Any, venue: dict[str, MagicMock]) -> None:
    _seed_position(conn)
    flatten_live_positions(conn, now_ms=10_000)
    again = flatten_live_positions(conn, now_ms=20_000)

    assert venue["place"].call_count == 1
    assert again["symbols"][0]["status"] == "already_sold"
    assert again["sold"] == []
    assert venue["notify"].call_count == 1


def test_venue_already_holding_the_sell_is_recovered_without_post(
    conn: Any, venue: dict[str, MagicMock]
) -> None:
    _seed_position(conn, with_stop=False)
    venue["query"].return_value = SOLD
    result = flatten_live_positions(conn, now_ms=10_000)

    assert venue["place"].call_count == 0
    assert venue["query"].call_args.kwargs["orig_client_order_id"] == "jrv-fbuy1"
    assert result["sold"][0]["status"] == "filled"
    row = get_live_order(conn, result["symbols"][0]["live_order_id"])
    assert "recovered" in row["error"]


def test_cancel_failure_keeps_stop_and_does_not_sell(conn: Any, venue: dict[str, MagicMock]) -> None:
    _seed_position(conn)
    venue["cancel"].side_effect = httpx.ConnectError("venue down")
    result = flatten_live_positions(conn, now_ms=10_000)

    assert result["ok"] is False
    assert venue["place"].call_count == 0
    assert get_live_order(conn, "stop1")["status"] == "submitted"
    assert result["unprotected"] == []
    assert "stop_cancel_failed" in result["errors"][0]
    venue["notify"].assert_called_once()


def test_sell_failure_re_places_stop(
    conn: Any, venue: dict[str, MagicMock], _mute_protective_stop: MagicMock
) -> None:
    _seed_position(conn)
    venue["place"].side_effect = httpx.ReadTimeout("lost")
    result = flatten_live_positions(conn, now_ms=10_000)

    assert result["ok"] is False
    assert _mute_protective_stop.call_count == 1
    stop_kwargs = _mute_protective_stop.call_args.kwargs
    assert stop_kwargs["new_client_order_id"] == "jrv-xropen1"
    assert stop_kwargs["quantity"] == Decimal("0.00999")
    assert stop_kwargs["stop_price"] == Decimal("97000")
    assert result["stop_replaced"][0]["status"] == "submitted"
    assert result["unprotected"] == []


def test_sell_and_re_place_failure_is_unprotected(
    conn: Any, venue: dict[str, MagicMock], _mute_protective_stop: MagicMock
) -> None:
    _seed_position(conn)
    venue["place"].side_effect = httpx.ReadTimeout("lost")
    _mute_protective_stop.side_effect = httpx.ConnectError("venue down")
    result = flatten_live_positions(conn, now_ms=10_000)

    assert result["unprotected"] == ["BTCUSDT"]
    assert "UNPROTECTED BTCUSDT" in format_live_flatten_message(result)


def test_dust_is_recorded_once_and_not_re_alerted(conn: Any, venue: dict[str, MagicMock]) -> None:
    _seed_position(conn, qty=0.00004, quote=4.0, with_stop=False)
    first = flatten_live_positions(conn, now_ms=10_000)
    second = flatten_live_positions(conn, now_ms=20_000)

    assert venue["place"].call_count == 0
    assert first["ok"] is True
    assert first["dust"][0]["symbol"] == "BTCUSDT"
    assert second["symbols"][0]["status"] == "already_recorded"
    assert venue["notify"].call_count == 1


def test_dry_run_makes_no_http(conn: Any, venue: dict[str, MagicMock]) -> None:
    _seed_position(conn)
    result = flatten_live_positions(conn, now_ms=10_000, dry_run=True)

    assert result["symbols"][0]["status"] == "would_sell"
    assert result["symbols"][0]["stops_to_cancel"] == ["jrv-xopen1"]
    for name in ("cancel", "query", "place", "filters", "free", "notify"):
        assert venue[name].call_count == 0, name
    assert get_live_order(conn, "stop1")["status"] == "submitted"


def test_cli_flatten_refuses_with_flag_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)
    db = tmp_path / "cli.db"
    open_db(db).close()
    out = CliRunner().invoke(trade_app, ["flatten", "--db", str(db)])
    assert out.exit_code == 1
    assert "live_trading_disabled" in out.output


def test_cli_flatten_dry_run_json(tmp_path: Path, conn: Any, venue: dict[str, MagicMock]) -> None:
    _seed_position(conn)
    db = tmp_path / "flat.db"
    out = CliRunner().invoke(trade_app, ["flatten", "--db", str(db), "--dry-run", "--json"])
    assert out.exit_code == 0, out.output
    body = json.loads(out.output)
    assert body["dry_run"] is True
    assert body["symbols"][0]["status"] == "would_sell"
    assert venue["place"].call_count == 0


def test_live_risk_tick_engages_on_day_loss_then_flattens(conn: Any, venue: dict[str, MagicMock]) -> None:
    _seed_position(conn)
    breach = "live_max_daily_loss: day_pnl $-60.00 <= -$50.00"
    with (
        patch("jarvise_trade.flatten.reconcile_live_orders", return_value={"ok": True}) as recon,
        patch("jarvise_trade.flatten.live_day_loss_breach", return_value=breach),
        patch("jarvise_trade.flatten.engage_kill_switch", return_value=True) as engage,
    ):
        tick = live_risk_tick(conn, now_ms=10_000)

    recon.assert_called_once()
    engage.assert_called_once_with(reason=breach)
    assert tick["ok"] is True
    assert tick["kill_switch_engaged_now"] is True
    assert tick["flatten"]["reason"] == breach
    assert venue["place"].call_count == 1


def test_live_risk_tick_without_breach_or_engage_sells_nothing(
    conn: Any, venue: dict[str, MagicMock]
) -> None:
    _seed_position(conn)
    venue["ks"].return_value = {"engaged": False, "known": True, "reason": None, "error": None}
    with (
        patch("jarvise_trade.flatten.reconcile_live_orders", return_value={"ok": True}),
        patch("jarvise_trade.flatten.live_day_loss_breach", return_value=None),
        patch("jarvise_trade.flatten.engage_kill_switch") as engage,
    ):
        tick = live_risk_tick(conn, now_ms=10_000)

    assert engage.call_count == 0
    assert tick["ok"] is True
    assert tick["flatten"]["skipped_reason"] == "kill_switch_clear"
    assert venue["place"].call_count == 0
