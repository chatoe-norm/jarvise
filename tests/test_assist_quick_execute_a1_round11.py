"""Quick Plan A1 — fix round 11.

Medium: a later finished entry intent must not permanently skip intent-scoped
jv- cleanup for an already-closed exec-linked Quick trade.

Low: past the unresolved-exec keep window, a non-list allOrders or userTrades
HTTP 200 must keep the registration (same as a read exception).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.test_assist_execute import _auth, _client  # noqa: E402
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    _daytime_ms,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round5 import _seed_closed_exec_quick  # noqa: E402
from tests.test_assist_quick_execute_a1_round7 import _seed_unresolved_exec_reg  # noqa: E402
from tests.test_assist_quick_execute_a1_round10b import (  # noqa: E402
    _delete_count,
    _FlexibleTransport,
    _marker_rows,
)

from jarvise_assist.execute import client_order_id, finish_exec_intent
from jarvise_assist.futures import BinanceFuturesClient
from jarvise_assist.quick import UNRESOLVED_EXEC_KEEP_MS, sync_quick_trades
from jarvise_assist.real_trades import fetch_all_orders, fetch_all_user_trades
from jarvise_assist.store import insert_exec_intent
from jarvise_assist.watch import QUICK_JV_CLEANUP_STEP, _cleanup_exec_linked_quick_jv
from jarvise_ingest.db import open_db
from jarvise_ingest.http import ProviderError

# JSON string body: get_json returns a str, which futures.py collapses to [].
_JSON_STRING = '"not-a-list"'
_RAW_NON_JSON = "not-json-at-all"
_DICT_BODY: dict[str, Any] = {"code": 0, "msg": "not a list"}


def _futures(transport: httpx.BaseTransport) -> BinanceFuturesClient:
    http = httpx.Client(transport=transport, base_url="https://fapi.binance.com")
    return BinanceFuturesClient(_auth(), client=http, base_url="https://fapi.binance.com")


def _reg_status(conn: Any, rid: int) -> str:
    row = conn.execute(
        "SELECT status FROM assist_quick_registration WHERE id=?",
        (rid,),
    ).fetchone()
    assert row is not None
    return str(row["status"])


def _seed_done_quick_intent(conn: Any, *, intent_id: str, ts: int) -> None:
    insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=ts - 120_000,
        expires_at_ms=ts - 90_000,
    )
    conn.execute("UPDATE assist_exec_intent SET id=? WHERE symbol='BTCUSDT'", (intent_id,))
    conn.commit()
    finish_exec_intent(conn, intent_id, status="done", result_text="done")
    _seed_closed_exec_quick(conn, intent_id=intent_id, closed_at=ts - 30_000)


def _seed_later_entry(conn: Any, *, status: str, created_at_ms: int) -> str:
    """Entry intent created after the closed trade, already in ``status``."""
    later_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=created_at_ms,
        expires_at_ms=created_at_ms + 30_000,
    )
    finish_exec_intent(conn, later_id, status=status, result_text=status)
    return later_id


def _book(
    closed_id: str,
    later_id: str | None,
) -> list[dict[str, Any]]:
    rows = [
        {
            "symbol": "BTCUSDT",
            "algoId": 901,
            "clientAlgoId": client_order_id(closed_id, "sl"),
            "reduceOnly": True,
        },
        {
            "symbol": "BTCUSDT",
            "algoId": 902,
            "clientAlgoId": client_order_id(closed_id, "tp"),
            "reduceOnly": True,
        },
        {
            "symbol": "BTCUSDT",
            "algoId": 903,
            "clientAlgoId": "owner-manual-sl",
            "reduceOnly": True,
        },
        {
            "symbol": "BTCUSDT",
            "algoId": 904,
            "clientAlgoId": client_order_id("aei_otherintent1", "sl"),
            "reduceOnly": True,
        },
    ]
    if later_id is not None:
        rows.append(
            {
                "symbol": "BTCUSDT",
                "algoId": 905,
                "clientAlgoId": client_order_id(later_id, "sl"),
                "reduceOnly": True,
            }
        )
    return rows


def _cleanup_handlers(
    *,
    algo_body: Any,
    position_body: Any,
) -> dict[tuple[str, str], Any]:
    return {
        ("GET", "/fapi/v3/positionRisk"): (200, position_body),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, algo_body),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }


def _deleted_algo_ids(transport: _FlexibleTransport) -> set[str]:
    return {
        str(call[2].get("algoId"))
        for call in transport.calls
        if call[0] == "DELETE" and call[1] == "/fapi/v1/algoOrder"
    }


def _run_cleanup(
    conn: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    algo_body: Any,
    position_body: Any,
    now_ms: int,
) -> tuple[list[dict[str, str]], _FlexibleTransport]:
    transport = _FlexibleTransport(_cleanup_handlers(algo_body=algo_body, position_body=position_body))
    monkeypatch.setattr(
        "jarvise_assist.watch.build_trade_client",
        lambda: (_client(transport), None),
    )
    cancelled = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],
        quick_info={"closed": []},
        now_ms=now_ms,
    )
    return cancelled, transport


# ---------------------------------------------------------------------------
# Medium — later finished intent must not skip the closed intent's jv- cleanup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("later_status", ["expired", "failed"])
def test_r11_later_finished_entry_still_cancels_closed_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    later_status: str,
) -> None:
    """Flat, no in-flight, later finished entry: cancel only the closed intent's jv- ids."""
    conn = open_db(tmp_path / f"r11_later_{later_status}.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    closed_id = "aei_r11closed001"
    _seed_done_quick_intent(conn, intent_id=closed_id, ts=ts)
    # Created after closed_at (ts - 30_000). Today this makes every later pass skip.
    later_id = _seed_later_entry(conn, status=later_status, created_at_ms=ts - 10_000)
    assert later_id != closed_id

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")

    book = _book(closed_id, later_id)
    cancelled, transport = _run_cleanup(
        conn,
        monkeypatch,
        algo_body=book,
        position_body=[],
        now_ms=ts,
    )
    assert sorted(row["algo_id"] for row in cancelled) == ["901", "902"]
    assert _delete_count(transport) == 2
    assert _deleted_algo_ids(transport) == {"901", "902"}
    assert len(_marker_rows(conn, closed_id)) == 1
    assert _marker_rows(conn, closed_id)[0]["step"] == QUICK_JV_CLEANUP_STEP

    # Second pass: marker present → zero cancels.
    cancelled_2, transport_2 = _run_cleanup(
        conn,
        monkeypatch,
        algo_body=book,
        position_body=[],
        now_ms=ts + 1_000,
    )
    assert cancelled_2 == []
    assert _delete_count(transport_2) == 0
    assert len(_marker_rows(conn, closed_id)) == 1
    conn.close()


def test_r11_inflight_intent_still_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirmed/unknown/recovering intent on the symbol still skips cleanup."""
    conn = open_db(tmp_path / "r11_inflight.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    closed_id = "aei_r11closed002"
    _seed_done_quick_intent(conn, intent_id=closed_id, ts=ts)
    inflight = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=ts - 5_000,
        expires_at_ms=ts + 60_000,
    )
    finish_exec_intent(conn, inflight, status="unknown", result_text="unknown")

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")
    cancelled, transport = _run_cleanup(
        conn,
        monkeypatch,
        algo_body=_book(closed_id, None),
        position_body=[],
        now_ms=ts,
    )
    assert cancelled == []
    assert _delete_count(transport) == 0
    assert _marker_rows(conn, closed_id) == []
    conn.close()


def test_r11_open_position_still_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live position on the symbol still skips, even with no later finished intent."""
    conn = open_db(tmp_path / "r11_open.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    closed_id = "aei_r11closed003"
    _seed_done_quick_intent(conn, intent_id=closed_id, ts=ts)

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")
    open_row = {
        "symbol": "BTCUSDT",
        "positionSide": "BOTH",
        "positionAmt": "0.01",
        "entryPrice": "100",
        "markPrice": "101",
        "updateTime": ts,
    }
    cancelled, transport = _run_cleanup(
        conn,
        monkeypatch,
        algo_body=_book(closed_id, None),
        position_body=[open_row],
        now_ms=ts,
    )
    assert cancelled == []
    assert _delete_count(transport) == 0
    assert _marker_rows(conn, closed_id) == []
    conn.close()


# ---------------------------------------------------------------------------
# Low — malformed allOrders / userTrades must not expire an unresolved exec reg
# ---------------------------------------------------------------------------


def _history_handlers(
    *,
    trades: Any,
    orders: Any,
    positions: Any = None,
) -> dict[tuple[str, str], Any]:
    return {
        ("GET", "/fapi/v1/userTrades"): (200, trades),
        ("GET", "/fapi/v1/allOrders"): (200, orders),
        ("GET", "/fapi/v3/positionRisk"): (200, [] if positions is None else positions),
    }


def _sync(
    conn: Any,
    transport: _FlexibleTransport,
    *,
    now_ms: int,
    notices: list[str],
) -> None:
    sync_quick_trades(
        conn,
        _futures(transport),
        now_ms=now_ms,
        send=lambda text, **_k: notices.append(str(text)) or True,
    )


@pytest.mark.parametrize("bad_body", [_DICT_BODY, _RAW_NON_JSON, _JSON_STRING])
def test_r11_all_orders_malformed_keeps_then_empty_expires(tmp_path: Path, bad_body: Any) -> None:
    """(a)+(c) past keep window, flat, no logged orderId.

    Malformed allOrders stays open with no expire notice. The next pass with
    healthy empty lists expires once.
    """
    conn = open_db(tmp_path / "r11_orders.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created)
    past = created + UNRESOLVED_EXEC_KEEP_MS + 1
    notices: list[str] = []

    _sync(
        conn,
        _FlexibleTransport(_history_handlers(trades=[], orders=bad_body)),
        now_ms=past,
        notices=notices,
    )
    assert _reg_status(conn, rid) == "open"
    assert notices == []

    _sync(
        conn,
        _FlexibleTransport(_history_handlers(trades=[], orders=[])),
        now_ms=past + 1,
        notices=notices,
    )
    assert _reg_status(conn, rid) == "expired"
    assert len(notices) == 1
    conn.close()


@pytest.mark.parametrize("bad_body", [_DICT_BODY, _RAW_NON_JSON, _JSON_STRING])
def test_r11_user_trades_malformed_keeps(tmp_path: Path, bad_body: Any) -> None:
    """(b) malformed userTrades, healthy empty allOrders, flat → stay open."""
    conn = open_db(tmp_path / "r11_trades.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created)
    past = created + UNRESOLVED_EXEC_KEEP_MS + 1
    notices: list[str] = []

    _sync(
        conn,
        _FlexibleTransport(_history_handlers(trades=bad_body, orders=[])),
        now_ms=past,
        notices=notices,
    )
    assert _reg_status(conn, rid) == "open"
    assert notices == []
    conn.close()


def test_r11_matching_mkt_order_tags_not_expired(tmp_path: Path) -> None:
    """(d) healthy allOrders jv-…-mkt plus a fill with that orderId → tagged."""
    conn = open_db(tmp_path / "r11_tag.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, mkt_cid = _seed_unresolved_exec_reg(conn, created=created)
    past = created + UNRESOLVED_EXEC_KEEP_MS + 1
    buy_t = created + 1_000
    # No clientOrderId on the fill: resolve must come from allOrders.
    fills = [
        {
            "id": 1,
            "orderId": 777001,
            "side": "BUY",
            "price": "100",
            "qty": "0.05",
            "time": buy_t,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        }
    ]
    orders = [
        {
            "orderId": 777001,
            "clientOrderId": mkt_cid,
            "status": "FILLED",
            "time": buy_t,
            "updateTime": buy_t,
        }
    ]
    notices: list[str] = []
    _sync(
        conn,
        _FlexibleTransport(_history_handlers(trades=fills, orders=orders)),
        now_ms=past,
        notices=notices,
    )
    row = conn.execute(
        "SELECT status, trade_id FROM assist_quick_registration WHERE id=?",
        (rid,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "tagged"
    assert row["trade_id"]
    assert notices == []
    conn.close()


@pytest.mark.parametrize("bad_body", [_DICT_BODY, _JSON_STRING])
def test_r11_main_fetch_non_list_still_empty(bad_body: Any) -> None:
    """(e) callers that omit strict still collapse a parsed non-list to []."""
    start = 1_700_000_000_000
    end = start + 60_000
    transport = _FlexibleTransport(_history_handlers(trades=bad_body, orders=bad_body))
    client = _futures(transport)
    assert fetch_all_orders(client, "BTCUSDT", start_ms=start, end_ms=end) == []
    assert fetch_all_user_trades(client, "BTCUSDT", start_ms=start, end_ms=end) == []


def test_r11_main_fetch_raw_non_json_still_raises() -> None:
    """Raw non-JSON never reaches the futures collapse; main callers still raise."""
    start = 1_700_000_000_000
    end = start + 60_000
    orders_client = _futures(_FlexibleTransport(_history_handlers(trades=[], orders=_RAW_NON_JSON)))
    with pytest.raises(ProviderError):
        fetch_all_orders(orders_client, "BTCUSDT", start_ms=start, end_ms=end)
    trades_client = _futures(_FlexibleTransport(_history_handlers(trades=_RAW_NON_JSON, orders=[])))
    with pytest.raises(ProviderError):
        fetch_all_user_trades(trades_client, "BTCUSDT", start_ms=start, end_ms=end)
