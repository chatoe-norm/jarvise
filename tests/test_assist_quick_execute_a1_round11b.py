"""Quick Plan A1 — fix round 11b.

Low: an exec-linked registration that already has a logged market orderId must
not expire as no-fill when userTrades is a non-list HTTP 200. Strict userTrades
keeps it; a later healthy fill list tags and closes; a healthy empty list plus
flat still expires once.

Mutation killers for round-11 survivors A97, A98, A99, A102, A103. No production
change in those tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_execute_a1 import _daytime_ms, _seed_settings  # noqa: E402
from tests.test_assist_quick_execute_a1_round7 import _seed_unresolved_exec_reg  # noqa: E402
from tests.test_assist_quick_execute_a1_round10b import _FlexibleTransport  # noqa: E402
from tests.test_assist_quick_execute_a1_round11 import (  # noqa: E402
    _DICT_BODY,
    _JSON_STRING,
    _futures,
    _history_handlers,
    _reg_status,
    _sync,
)

from jarvise_assist.futures import ALL_ORDERS_PATH, USER_TRADES_PATH, BinanceFuturesClient
from jarvise_assist.quick import REG_EXPIRE_GRACE_MS
from jarvise_assist.real_trades import (
    HistoryPageUnreadable,
    _history_page,
    fetch_all_orders,
    fetch_all_user_trades,
)
from jarvise_ingest.db import open_db

# Parsed body a fake ``_signed_get`` returns. The HTTP form is ``_JSON_STRING``.
_PARSED_STR = "not-a-list"
_ORDER_ID = "777001"
_START_MS = 1_700_000_000_000
_END_MS = _START_MS + 60_000


def _past_grace(conn: Any, rid: str) -> int:
    row = conn.execute(
        "SELECT expires_at_ms FROM assist_quick_registration WHERE id=?",
        (rid,),
    ).fetchone()
    assert row is not None
    return int(row["expires_at_ms"]) + REG_EXPIRE_GRACE_MS + 1


def _trade_count(conn: Any) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM assist_quick_trade").fetchone()
    assert row is not None
    return int(row["n"])


def _round_trip(created: int, order_id: str) -> list[dict[str, Any]]:
    buy_t = created + 1_000
    sell_t = created + 5_000
    oid = int(order_id)
    return [
        {
            "id": 1,
            "orderId": oid,
            "side": "BUY",
            "price": "100",
            "qty": "0.05",
            "time": buy_t,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": oid + 1,
            "side": "SELL",
            "price": "99",
            "qty": "0.05",
            "time": sell_t,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "-0.05",
        },
    ]


def _seed_known_order(conn: Any, *, created: int) -> tuple[str, str]:
    iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created, order_id_logged=_ORDER_ID)
    return iid, str(rid)


# ---------------------------------------------------------------------------
# Low — known market orderId + malformed userTrades must not expire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_body", [_DICT_BODY, _JSON_STRING])
def test_r11b_known_order_bad_trades_keeps_then_fills_close(tmp_path: Path, bad_body: Any) -> None:
    """Exec-linked, logged orderId, flat, past grace.

    A dict or JSON-string userTrades stays open with no trade and no notice.
    The next pass with that order's fills tags the registration and closes the trade.
    """
    conn = open_db(tmp_path / "r11b_fills.db")
    _seed_settings(conn)
    created = _daytime_ms()
    iid, rid = _seed_known_order(conn, created=created)
    past = _past_grace(conn, rid)
    notices: list[str] = []

    _sync(
        conn,
        _FlexibleTransport(_history_handlers(trades=bad_body, orders=[])),
        now_ms=past,
        notices=notices,
    )
    assert _reg_status(conn, rid) == "open"
    assert _trade_count(conn) == 0
    assert notices == []

    fills = _round_trip(created, _ORDER_ID)
    _sync(
        conn,
        _FlexibleTransport(
            {
                **_history_handlers(trades=fills, orders=[]),
                ("GET", "/fapi/v1/income"): (200, []),
            }
        ),
        now_ms=past + 1,
        notices=notices,
    )
    row = conn.execute(
        "SELECT status, trade_id FROM assist_quick_registration WHERE id=?",
        (rid,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "tagged"
    assert row["trade_id"]
    trade = conn.execute(
        "SELECT status FROM assist_quick_trade WHERE id=?",
        (row["trade_id"],),
    ).fetchone()
    assert trade is not None
    assert trade["status"] == "closed"
    assert all("ไม่พบไม้" not in text for text in notices)
    linked = conn.execute(
        "SELECT exec_intent_id FROM assist_quick_registration WHERE id=?",
        (rid,),
    ).fetchone()
    assert linked is not None
    assert str(linked["exec_intent_id"]) == iid
    conn.close()


def test_r11b_known_order_empty_trades_flat_expires_once(tmp_path: Path) -> None:
    """A strict empty userTrades list, flat, past grace, expires once."""
    conn = open_db(tmp_path / "r11b_empty.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid = _seed_known_order(conn, created=created)
    past = _past_grace(conn, rid)
    notices: list[str] = []
    handlers = _history_handlers(trades=[], orders=[])

    _sync(conn, _FlexibleTransport(handlers), now_ms=past, notices=notices)
    assert _reg_status(conn, rid) == "expired"
    assert len(notices) == 1
    assert _trade_count(conn) == 0

    _sync(conn, _FlexibleTransport(handlers), now_ms=past + 1, notices=notices)
    assert _reg_status(conn, rid) == "expired"
    assert len(notices) == 1
    conn.close()


# ---------------------------------------------------------------------------
# Mutation killers — real_trades history pages (production unchanged)
# ---------------------------------------------------------------------------


class _RawSigned:
    """``_signed_get`` returns the payload as-is. No public history method."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def _signed_get(self, path: str, params: dict[str, Any] | None = None, *, provider: str) -> Any:
        return self.payload


class _PublicNonList:
    """No ``_signed_get``. Public history methods return a non-list."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def user_trades(self, *_a: Any, **_k: Any) -> Any:
        return self.payload

    def all_orders(self, *_a: Any, **_k: Any) -> Any:
        return self.payload


def _assert_strict_raises(client: Any) -> None:
    with pytest.raises(HistoryPageUnreadable):
        fetch_all_user_trades(client, "BTCUSDT", start_ms=_START_MS, end_ms=_END_MS, strict=True)
    with pytest.raises(HistoryPageUnreadable):
        fetch_all_orders(client, "BTCUSDT", start_ms=_START_MS, end_ms=_END_MS, strict=True)


@pytest.mark.parametrize("payload", [_DICT_BODY, _PARSED_STR])
def test_r11b_a97_strict_signed_non_list_raises(payload: Any) -> None:
    """A97: signed strict path raises. ``list(dict|str)`` must not be the page."""
    _assert_strict_raises(_RawSigned(payload))


@pytest.mark.parametrize("payload", [_DICT_BODY, _PARSED_STR])
def test_r11b_a98_strict_public_fallback_non_list_raises(payload: Any) -> None:
    """A98: a client without ``_signed_get`` still raises on a non-list page."""
    client = _PublicNonList(payload)
    assert not callable(getattr(client, "_signed_get", None))
    _assert_strict_raises(client)


@pytest.mark.parametrize("bad_body", [_DICT_BODY, _JSON_STRING])
def test_r11b_a99_main_default_non_list_is_empty(bad_body: Any) -> None:
    """A99: strict=False on the real client returns [] and does not raise.

    ``fetch_all_*`` default never enters ``_history_page``. A99 flips the first
    ``if not strict`` there (the only ``return public_page()``), so the killer
    calls ``_history_page(..., strict=False)`` on the same client.
    """
    transport = _FlexibleTransport(_history_handlers(trades=bad_body, orders=bad_body))
    client = _futures(transport)
    assert isinstance(client, BinanceFuturesClient)
    assert fetch_all_user_trades(client, "BTCUSDT", start_ms=_START_MS, end_ms=_END_MS) == []
    assert fetch_all_orders(client, "BTCUSDT", start_ms=_START_MS, end_ms=_END_MS) == []

    trades = _history_page(
        client,
        lambda: client.user_trades("BTCUSDT", start_ms=_START_MS, end_ms=_END_MS - 1, limit=1000),
        strict=False,
        path=USER_TRADES_PATH,
        params={
            "symbol": "BTCUSDT",
            "limit": 1000,
            "startTime": _START_MS,
            "endTime": _END_MS - 1,
        },
        provider="binance_fapi_user_trades",
    )
    orders = _history_page(
        client,
        lambda: client.all_orders("BTCUSDT", start_ms=_START_MS, end_ms=_END_MS - 1, limit=1000),
        strict=False,
        path=ALL_ORDERS_PATH,
        params={
            "symbol": "BTCUSDT",
            "limit": 1000,
            "startTime": _START_MS,
            "endTime": _END_MS - 1,
        },
        provider="binance_fapi_all_orders",
    )
    assert trades == []
    assert orders == []


def _page_rows(start_ms: int) -> list[dict[str, Any]]:
    return [
        {
            "id": i + 1,
            "orderId": i + 1,
            "time": start_ms,
            "updateTime": start_ms,
            "side": "BUY",
            "price": "100",
            "qty": "0.01",
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        }
        for i in range(1000)
    ]


def _cursor_transport(path: str, cursor_key: str, cursor_body: Any) -> _FlexibleTransport:
    def script(params: dict[str, Any]) -> tuple[int, Any]:
        if cursor_key in params:
            return 200, cursor_body
        return 200, _page_rows(int(params["startTime"]))

    return _FlexibleTransport({("GET", path): script})


def _assert_cursor_page(
    *,
    path: str,
    cursor_key: str,
    cursor_body: Any,
    fetch: Any,
    id_key: str,
) -> None:
    loose = _cursor_transport(path, cursor_key, cursor_body)
    rows = fetch(_futures(loose), "BTCUSDT", start_ms=_START_MS, end_ms=_END_MS)
    assert len(rows) == 1000
    assert [int(rows[0][id_key]), int(rows[-1][id_key])] == [1, 1000]
    assert all(isinstance(row, dict) for row in rows)
    assert any(cursor_key in call[2] for call in loose.calls)

    strict = _cursor_transport(path, cursor_key, cursor_body)
    with pytest.raises(HistoryPageUnreadable):
        fetch(_futures(strict), "BTCUSDT", start_ms=_START_MS, end_ms=_END_MS, strict=True)
    assert any(cursor_key in call[2] for call in strict.calls)


@pytest.mark.parametrize("cursor_body", [_DICT_BODY, _JSON_STRING])
def test_r11b_a102_user_trades_cursor_non_list(cursor_body: Any) -> None:
    """A102: cursor userTrades page is strict when the walk is strict."""
    _assert_cursor_page(
        path=USER_TRADES_PATH,
        cursor_key="fromId",
        cursor_body=cursor_body,
        fetch=fetch_all_user_trades,
        id_key="id",
    )


@pytest.mark.parametrize("cursor_body", [_DICT_BODY, _JSON_STRING])
def test_r11b_a103_all_orders_cursor_non_list(cursor_body: Any) -> None:
    """A103: cursor allOrders page is strict when the walk is strict."""
    _assert_cursor_page(
        path=ALL_ORDERS_PATH,
        cursor_key="orderId",
        cursor_body=cursor_body,
        fetch=fetch_all_orders,
        id_key="orderId",
    )
