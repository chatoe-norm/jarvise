"""Quick Plan A1 — fix round 7 (allOrders sync resolve, A62 recover keep_unknown, A58 marker)."""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    RecordingTrade,
    _daytime_ms,
    _insert_quoted_card,
    _Pos,
    _quick_preview,
    _seed_quick_settings,
    _seed_settings,
)

from jarvise_assist.execute import (
    TradeClientError,
    _recover_entry_intent,
    client_order_id,
    finish_exec_intent,
)
from jarvise_assist.futures import BinanceFuturesClient
from jarvise_assist.quick import UNRESOLVED_EXEC_KEEP_MS
from jarvise_assist.store import (
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
)
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# Fake read-client parity with production BinanceFuturesClient
# ---------------------------------------------------------------------------

_REQUIRED_READ_METHODS = ("all_orders", "user_trades", "position_risk")


def assert_fake_mirrors_futures_read_client(fake: Any) -> None:
    """Every public callable on *fake* must exist on BinanceFuturesClient.

    Parameter names for all_orders / user_trades / position_risk must match.
    The fake must NOT expose query_order_by_client_id (trade-only API).
    """
    assert not hasattr(fake, "query_order_by_client_id"), "read fake must not expose query_order_by_client_id"
    for name, attr in vars(type(fake)).items():
        if name.startswith("_"):
            continue
        if not callable(attr):
            continue
        assert hasattr(BinanceFuturesClient, name), f"fake method {name!r} missing on BinanceFuturesClient"
        if name in _REQUIRED_READ_METHODS:
            fake_params = list(inspect.signature(attr).parameters.keys())
            real_params = list(inspect.signature(getattr(BinanceFuturesClient, name)).parameters.keys())
            assert fake_params == real_params, (
                f"{name} params {fake_params} != BinanceFuturesClient {real_params}"
            )


class _ReadClientBase:
    """Minimal production-shaped read client (no query_order_by_client_id)."""

    def __init__(self) -> None:
        self.all_orders_calls = 0
        self._orders: list[dict[str, Any]] = []
        self._positions: list[Any] = []

    def all_orders(
        self,
        symbol: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        order_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.all_orders_calls += 1
        return list(self._orders)

    def user_trades(
        self,
        symbol: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        from_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return []

    def position_risk(self, *, symbol: str | None = None) -> list[Any]:
        if symbol is None:
            return list(self._positions)
        return [p for p in self._positions if str(getattr(p, "symbol", "")).upper() == symbol.upper()]


def _seed_unresolved_exec_reg(conn: Any, *, created: int, order_id_logged: str = "") -> tuple[str, int, str]:
    from jarvise_assist.quick import create_quick_registration

    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.05", "entry": "100", "stop": "99"},
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    mkt_cid = client_order_id(iid, "mkt")
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=mkt_cid,
        order_or_algo_id=order_id_logged,
        http_status=200,
        created_at_ms=created,
    )
    rid, reason = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=created,
        exec_intent_id=iid,
    )
    assert rid is not None, reason
    return iid, rid, mkt_cid


# ---------------------------------------------------------------------------
# 1. Medium — past-keep resolve via allOrders (production read client)
# ---------------------------------------------------------------------------


def test_r7_sync_past_keep_all_orders_tags_matching_order_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) past 2h, all_orders has jv-mkt → tag; fills with other orderId ignored."""
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import sync_quick_trades

    conn = open_db(tmp_path / "r7_sync_a.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, mkt_cid = _seed_unresolved_exec_reg(conn, created=created)
    buy_t = created + 1_000
    sell_t = created + 5_000
    # Earlier unrelated SELL (different orderId): without orderId filter this would
    # be first → wrong_side expire. With filter it must be ignored.
    fills = [
        {
            "id": 90,
            "orderId": 888888,
            "side": "SELL",
            "price": "101",
            "qty": "1.0",
            "time": created + 100,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
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
        },
        {
            "id": 2,
            "orderId": 777002,
            "side": "SELL",
            "price": "99",
            "qty": "0.05",
            "time": sell_t,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "-0.05",
        },
    ]

    class _Client(_ReadClientBase):
        def __init__(self) -> None:
            super().__init__()
            self._orders = [
                {
                    "orderId": 777001,
                    "status": "FILLED",
                    "executedQty": "0.05",
                    "avgPrice": "100",
                    "clientOrderId": mkt_cid,
                    "time": buy_t,
                    "updateTime": buy_t,
                }
            ]

    client = _Client()
    assert_fake_mirrors_futures_read_client(client)
    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: list(fills))
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        client,
        now_ms=created + UNRESOLVED_EXEC_KEEP_MS + 1,
        send=lambda *_a, **_k: True,
    )
    assert client.all_orders_calls >= 1
    reg = conn.execute("SELECT status, trade_id FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None
    assert reg["status"] == "tagged", reg["status"]
    assert reg["trade_id"]
    trade = conn.execute(
        "SELECT entry_qty, entry_trade_ids FROM assist_quick_trade WHERE id=?",
        (reg["trade_id"],),
    ).fetchone()
    assert trade is not None
    assert abs(float(trade["entry_qty"]) - 0.05) < 1e-9
    assert "1" in str(trade["entry_trade_ids"])
    assert "90" not in str(trade["entry_trade_ids"])
    conn.close()


def test_r7_sync_past_keep_all_orders_raise_keeps_open_then_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) all_orders raises + flat → stay open; next pass with orders tags."""
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import sync_quick_trades

    conn = open_db(tmp_path / "r7_sync_b.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, mkt_cid = _seed_unresolved_exec_reg(conn, created=created)
    notices: list[str] = []
    buy_t = created + 1_000
    sell_t = created + 5_000
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
        },
        {
            "id": 2,
            "orderId": 777002,
            "side": "SELL",
            "price": "99",
            "qty": "0.05",
            "time": sell_t,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "-0.05",
        },
    ]

    class _RaiseThenOk(_ReadClientBase):
        def __init__(self) -> None:
            super().__init__()
            self._raise = True
            self._orders = [
                {
                    "orderId": 777001,
                    "status": "FILLED",
                    "clientOrderId": mkt_cid,
                    "time": buy_t,
                    "updateTime": buy_t,
                }
            ]

        def all_orders(
            self,
            symbol: str,
            *,
            start_ms: int | None = None,
            end_ms: int | None = None,
            order_id: int | None = None,
            limit: int = 1000,
        ) -> list[dict[str, Any]]:
            self.all_orders_calls += 1
            if self._raise:
                raise RuntimeError("allOrders unavailable")
            return list(self._orders)

    client = _RaiseThenOk()
    assert_fake_mirrors_futures_read_client(client)
    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: list(fills))
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    past = created + UNRESOLVED_EXEC_KEEP_MS + 1
    sync_quick_trades(conn, client, now_ms=past, send=lambda t, **_k: notices.append(str(t)) or True)
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "open", reg
    assert notices == []

    client._raise = False
    sync_quick_trades(conn, client, now_ms=past + 1, send=lambda t, **_k: notices.append(str(t)) or True)
    reg2 = conn.execute(
        "SELECT status, trade_id FROM assist_quick_registration WHERE id=?", (rid,)
    ).fetchone()
    assert reg2 is not None and reg2["status"] == "tagged", reg2
    assert reg2["trade_id"]
    conn.close()


def test_r7_sync_past_keep_no_all_orders_attr_keeps_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) client without all_orders → stay open (AttributeError → continue)."""
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import sync_quick_trades

    conn = open_db(tmp_path / "r7_sync_c.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created)

    class _NoAllOrders:
        def user_trades(
            self,
            symbol: str,
            *,
            start_ms: int | None = None,
            end_ms: int | None = None,
            from_id: int | None = None,
            limit: int = 1000,
        ) -> list[dict[str, Any]]:
            return []

        def position_risk(self, *, symbol: str | None = None) -> list[Any]:
            return []

    client = _NoAllOrders()
    # Has user_trades + position_risk; deliberately lacks all_orders.
    assert not hasattr(client, "all_orders")
    assert not hasattr(client, "query_order_by_client_id")
    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: [])
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        client,
        now_ms=created + UNRESOLVED_EXEC_KEEP_MS + 1,
        send=lambda *_a, **_k: True,
    )
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "open", reg
    conn.close()


def test_r7_sync_past_keep_no_match_flat_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) all_orders OK, no matching clientOrderId, flat → expired."""
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import sync_quick_trades

    conn = open_db(tmp_path / "r7_sync_d.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created)

    class _Miss(_ReadClientBase):
        def __init__(self) -> None:
            super().__init__()
            self._orders = [
                {
                    "orderId": 1,
                    "clientOrderId": "unrelated",
                    "time": created,
                    "updateTime": created,
                }
            ]
            self._positions = []

    client = _Miss()
    assert_fake_mirrors_futures_read_client(client)
    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: [])
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        client,
        now_ms=created + UNRESOLVED_EXEC_KEEP_MS + 1,
        send=lambda *_a, **_k: True,
    )
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "expired", reg
    conn.close()


def test_r7_sync_past_keep_no_match_open_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(e) all_orders OK, no match, position still open → kept."""
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import sync_quick_trades

    conn = open_db(tmp_path / "r7_sync_e.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created)

    class _OpenMiss(_ReadClientBase):
        def __init__(self) -> None:
            super().__init__()
            self._orders = []
            self._positions = [_Pos("BTCUSDT", "long", "0.05", "100")]

    client = _OpenMiss()
    assert_fake_mirrors_futures_read_client(client)
    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: [])
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        client,
        now_ms=created + UNRESOLVED_EXEC_KEEP_MS + 1,
        send=lambda *_a, **_k: True,
    )
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "open", reg
    conn.close()


def test_r7_sync_within_keep_skips_all_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(f) within 2h → no all_orders call."""
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import sync_quick_trades

    conn = open_db(tmp_path / "r7_sync_f.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created)

    class _Within(_ReadClientBase):
        pass

    client = _Within()
    assert_fake_mirrors_futures_read_client(client)
    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: [])
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        client,
        now_ms=created + UNRESOLVED_EXEC_KEEP_MS - 1,
        send=lambda *_a, **_k: True,
    )
    assert client.all_orders_calls == 0
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "open", reg
    conn.close()


# ---------------------------------------------------------------------------
# 2. A62 — recovery first emergency-close site keeps Quick unknown
# ---------------------------------------------------------------------------


class _RecoverSlrRaiseTrade(RecordingTrade):
    """FOUND PARTIALLY_FILLED + open pos; recover stop place raises; verify NOT_PROTECTED."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.stop_ok = False
        self._positions = [_Pos("BTCUSDT", "long", "0.05", "100")]
        self._orders = []

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
        if client_id.endswith("-mkt") or "-mkt" in client_id:
            return {
                "orderId": 9101,
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.05",
                "avgPrice": "100",
                "clientOrderId": client_id,
            }
        raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        return self.query_order(symbol=symbol, client_id=client_id)

    def query_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_algo_by_client_id", {"symbol": symbol, "client_id": client_id}))
        raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)

    def place_algo_stop(self, **kw: Any) -> dict[str, Any]:
        if kw.get("order_type") == "STOP_MARKET":
            self.calls.append(("place_algo_stop", {**kw, "reduce_only": True}))
            raise TradeClientError("stop refused", http_status=400, binance_code=-2021)
        return super().place_algo_stop(**kw)

    def cancel_algo_by_client_id(self, *, symbol: str, client_algo_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_algo_by_client_id", {"symbol": symbol, "client_algo_id": client_algo_id}))
        raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)


def test_r7_a62_recover_slr_raise_emergency_keeps_quick_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A62: recover_slr raise → verify non-verified → emergency OK → unknown; reg open."""
    from jarvise_assist.quick import create_quick_registration

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r7_a62_quick.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.05)
    preview = _quick_preview(qty="0.05", card_id=card)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    finish_exec_intent(conn, iid, status="unknown", result_text="unknown")
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    rid, reason = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=ts,
        exec_intent_id=iid,
    )
    assert rid is not None, reason

    trade = _RecoverSlrRaiseTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    after = get_exec_intent(conn, iid)
    assert after is not None
    assert after["status"] == "unknown", after["status"]
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "open", reg
    assert any(c[0] == "place_algo_stop" for c in trade.calls)
    assert any(c[0] == "market_order" and c[1].get("reduce_only") is True for c in trade.calls)
    conn.close()


def test_r7_a62_recover_slr_raise_emergency_main_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paired main path: same recover_slr-raise emergency → failed (5608315)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r7_a62_main.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "signal_id": "sig_r7_a62_main",
    }
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    finish_exec_intent(conn, iid, status="unknown", result_text="unknown")
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()

    trade = _RecoverSlrRaiseTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    after = get_exec_intent(conn, iid)
    assert after is not None
    assert after["status"] == "failed", after["status"]
    conn.close()


# ---------------------------------------------------------------------------
# 3. A58 — cleanup marker step must be exactly quick_jv_cleanup
# ---------------------------------------------------------------------------


def test_r7_a58_cleanup_marker_step_exact_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After successful flat cleanup: exactly one quick_jv_cleanup log; 2nd pass no cancel."""
    from tests.test_assist_quick_execute_a1_round5 import _seed_closed_exec_quick

    from jarvise_assist.watch import QUICK_JV_CLEANUP_STEP, _cleanup_exec_linked_quick_jv

    conn = open_db(tmp_path / "r7_a58.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    intent_a = "aei_r7a58A001"
    insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=ts - 120_000,
        expires_at_ms=ts - 90_000,
    )
    conn.execute("UPDATE assist_exec_intent SET id=? WHERE symbol='BTCUSDT'", (intent_a,))
    conn.commit()
    finish_exec_intent(conn, intent_a, status="done", result_text="done")
    _seed_closed_exec_quick(conn, intent_id=intent_a, closed_at=ts - 30_000)

    trade = RecordingTrade()
    trade._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "901",
            "clientAlgoId": client_order_id(intent_a, "tp"),
            "reduceOnly": True,
        }
    ]
    trade._positions = []
    cancel_n = {"n": 0}

    def _cancel_ok(trade_arg: Any, symbol: str, *, intent_id: str | None = None) -> tuple[list[str], bool]:
        cancel_n["n"] += 1
        trade.calls.append(("cancel_jv_ok", {"symbol": symbol, "intent_id": intent_id}))
        ids = []
        for o in list(trade._orders):
            cid = str(o.get("clientAlgoId") or "")
            if intent_id and cid.startswith(f"jv-{str(intent_id).replace('aei_', '')[:12]}-"):
                ids.append(str(o["algoId"]))
                trade._orders.remove(o)
        return ids, True

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    monkeypatch.setattr("jarvise_assist.watch.cancel_jv_orders_for_symbol", _cancel_ok)

    cancelled = _cleanup_exec_linked_quick_jv(conn, positions=[], quick_info={"closed": []}, now_ms=ts)
    assert any(c["algo_id"] == "901" for c in cancelled), cancelled
    rows = conn.execute(
        "SELECT step FROM assist_exec_log WHERE intent_id=? AND step LIKE 'quick_jv_cleanup%'",
        (intent_a,),
    ).fetchall()
    assert len(rows) == 1, rows
    assert rows[0]["step"] == "quick_jv_cleanup"
    assert rows[0]["step"] == QUICK_JV_CLEANUP_STEP

    cancel_n["n"] = 0
    trade.calls.clear()
    trade._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "901",
            "clientAlgoId": client_order_id(intent_a, "tp"),
            "reduceOnly": True,
        }
    ]
    cancelled2 = _cleanup_exec_linked_quick_jv(
        conn, positions=[], quick_info={"closed": []}, now_ms=ts + 1_000
    )
    assert cancelled2 == []
    assert cancel_n["n"] == 0
    assert not any(c[0].startswith("cancel_jv") for c in trade.calls)
    rows2 = conn.execute(
        "SELECT step FROM assist_exec_log WHERE intent_id=? AND step=?",
        (intent_a, QUICK_JV_CLEANUP_STEP),
    ).fetchall()
    assert len(rows2) == 1
    conn.close()
