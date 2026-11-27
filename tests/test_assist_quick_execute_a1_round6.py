"""Quick Plan A1 — fix round 6 (emergency unknown, cleanup marker, sync resolve, mutants)."""

from __future__ import annotations

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
from tests.test_assist_quick_execute_a1_round3 import _PartialFillTrade  # noqa: E402
from tests.test_assist_quick_execute_a1_round5 import (  # noqa: E402
    _seed_closed_exec_quick,
    _TimeoutFallbackSlFailTrade,
)

from jarvise_assist.execute import (
    RECOVER_MAX_AGE_MS,
    TradeClientError,
    _recover_entry_intent,
    client_order_id,
    finish_exec_intent,
    run_entry_sequence,
)
from jarvise_assist.quick import UNRESOLVED_EXEC_KEEP_MS
from jarvise_assist.store import (
    claim_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
)
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# 1. High — Quick partial → SL fail → emergency OK → unknown; recovery → done
# ---------------------------------------------------------------------------


def test_r6_quick_emergency_close_nonfinal_stays_unknown_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.quick import create_quick_registration

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r6_emerg_unk.db")
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
    claim_exec_intent(conn, iid)
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

    trade = _TimeoutFallbackSlFailTrade(
        intent_id=iid, mark=Decimal("100"), fill_qty=Decimal("0.05"), resolve_oid="515151"
    )
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status == "unknown", result
    finish_exec_intent(conn, iid, status="unknown", result_text=str(result), expect_status="confirmed")
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "open", reg

    # Recovery: FOUND FILLED + flat → done
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    trade._positions = []
    trade._filled_after_market = False  # prevent position-fallback recreate
    trade.stop_ok = True

    def _filled_query(*, symbol: str, client_id: str) -> dict[str, Any]:
        trade.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
        return {
            "orderId": 515151,
            "status": "FILLED",
            "executedQty": "0.05",
            "avgPrice": "100",
            "clientOrderId": client_id,
        }

    trade.query_order = _filled_query  # type: ignore[method-assign]
    trade.query_order_by_client_id = (  # type: ignore[method-assign]
        lambda **kw: _filled_query(**kw)
    )
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    done = get_exec_intent(conn, iid)
    assert done is not None
    assert done["status"] == "done", done["status"]
    conn.close()


def test_r6_main_emergency_close_still_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Main same scenario → failed (5608315 parity)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r6_emerg_main.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "signal_id": "sig_r6_main_em",
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
    claim_exec_intent(conn, iid)
    trade = _TimeoutFallbackSlFailTrade(
        intent_id=iid, mark=Decimal("100"), fill_qty=Decimal("0.05"), resolve_oid="616161"
    )
    result = run_entry_sequence(conn, iid, trade=trade, origin=None, sleep=lambda _s: None)
    assert result.status == "failed", result
    conn.close()


# ---------------------------------------------------------------------------
# 2. Medium — cleanup marker only when cancel_ok
# ---------------------------------------------------------------------------


def test_r6_cleanup_marker_only_when_cancel_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.watch import QUICK_JV_CLEANUP_STEP, _cleanup_exec_linked_quick_jv

    conn = open_db(tmp_path / "r6_cleanup_ok.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    intent_a = "aei_r6cleanA001"
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
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))

    # Pass 1: cancel fails → no marker.
    def _cancel_fail(*_a: Any, **_k: Any) -> tuple[list[str], bool]:
        trade.calls.append(("cancel_jv_fail", {}))
        return [], False

    monkeypatch.setattr("jarvise_assist.watch.cancel_jv_orders_for_symbol", _cancel_fail)
    cancelled1 = _cleanup_exec_linked_quick_jv(conn, positions=[], quick_info={"closed": []}, now_ms=ts)
    assert cancelled1 == []
    assert (
        conn.execute(
            "SELECT 1 FROM assist_exec_log WHERE intent_id=? AND step=?",
            (intent_a, QUICK_JV_CLEANUP_STEP),
        ).fetchone()
        is None
    )

    # Pass 2: cancel succeeds → marker + cancelled ids.
    def _cancel_ok(trade_arg: Any, symbol: str, *, intent_id: str | None = None) -> tuple[list[str], bool]:
        trade.calls.append(("cancel_jv_ok", {"symbol": symbol, "intent_id": intent_id}))
        ids = []
        for o in list(trade._orders):
            cid = str(o.get("clientAlgoId") or "")
            if intent_id and cid.startswith(f"jv-{str(intent_id).replace('aei_', '')[:12]}-"):
                ids.append(str(o["algoId"]))
                trade._orders.remove(o)
        return ids, True

    monkeypatch.setattr("jarvise_assist.watch.cancel_jv_orders_for_symbol", _cancel_ok)
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
    assert any(c["algo_id"] == "901" for c in cancelled2), cancelled2
    assert (
        conn.execute(
            "SELECT 1 FROM assist_exec_log WHERE intent_id=? AND step=?",
            (intent_a, QUICK_JV_CLEANUP_STEP),
        ).fetchone()
        is not None
    )

    # Pass 3: marker present → no re-cancel.
    trade.calls.clear()
    trade._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "901",
            "clientAlgoId": client_order_id(intent_a, "tp"),
            "reduceOnly": True,
        }
    ]
    cancelled3 = _cleanup_exec_linked_quick_jv(
        conn, positions=[], quick_info={"closed": []}, now_ms=ts + 2_000
    )
    assert cancelled3 == []
    assert not any(c[0].startswith("cancel_jv") for c in trade.calls)
    conn.close()


# ---------------------------------------------------------------------------
# 3. Medium — unresolved sync: resolve retry / open keep / flat expire
# ---------------------------------------------------------------------------


def test_r6_sync_past_keep_open_position_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "r6_sync_open.db")
    _seed_settings(conn)
    created = _daytime_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.05", "entry": "100", "stop": "99"},
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="",
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

    class _OpenClient:
        def position_risk(self, *, symbol: str | None = None) -> list[Any]:
            return [_Pos("BTCUSDT", "long", "0.05", "100")]

        def all_orders(self, symbol: str, **_kw: Any) -> list[dict[str, Any]]:
            return []

        def user_trades(self, symbol: str, **_kw: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: [])
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        _OpenClient(),
        now_ms=created + UNRESOLVED_EXEC_KEEP_MS + 1,
        send=lambda *_a, **_k: True,
    )
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "open", reg
    conn.close()


def test_r6_sync_past_keep_resolve_tags_fills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "r6_sync_resolve.db")
    _seed_settings(conn)
    created = _daytime_ms()
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
        order_or_algo_id="",
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

    class _ResolveClient:
        def __init__(self) -> None:
            self.all_orders_calls = 0

        def position_risk(self, *, symbol: str | None = None) -> list[Any]:
            return []

        def all_orders(self, symbol: str, **_kw: Any) -> list[dict[str, Any]]:
            self.all_orders_calls += 1
            return [
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

        def user_trades(self, symbol: str, **_kw: Any) -> list[dict[str, Any]]:
            return []

    client = _ResolveClient()
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
    conn.close()


def test_r6_sync_past_keep_flat_resolve_miss_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "r6_sync_expire.db")
    _seed_settings(conn)
    created = _daytime_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.05", "entry": "100", "stop": "99"},
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="",
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

    class _FlatMissClient:
        def position_risk(self, *, symbol: str | None = None) -> list[Any]:
            return []

        def all_orders(self, symbol: str, **_kw: Any) -> list[dict[str, Any]]:
            return []

        def user_trades(self, symbol: str, **_kw: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: [])
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        _FlatMissClient(),
        now_ms=created + UNRESOLVED_EXEC_KEEP_MS + 1,
        send=lambda *_a, **_k: True,
    )
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and reg["status"] == "expired", reg
    conn.close()


# ---------------------------------------------------------------------------
# 4. Test gaps A49–A52, A54, A59
# ---------------------------------------------------------------------------


class _PartialClosedByStopTrade(_PartialFillTrade):
    """Partial fill; sl0 clear-reject then flat → CLOSED_BY_STOP inside place_and_verify."""

    def place_algo_stop(self, **kw: Any) -> dict[str, Any]:
        if kw.get("order_type") == "STOP_MARKET":
            self.calls.append(("place_algo_stop", {**kw, "reduce_only": True}))
            self._positions = []
            raise TradeClientError("would trigger", http_status=400, binance_code=-2021)
        return super().place_algo_stop(**kw)


def test_r6_a49_entry_closed_by_stop_partial_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r6_a49.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.1)
    preview = _quick_preview(qty="0.1", card_id=card)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    trade = _PartialClosedByStopTrade(mark=Decimal("100"), fill_qty=Decimal("0.1"))
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status == "unknown", result
    conn.close()


class _PartialVerifiedThenFlatTrade(_PartialFillTrade):
    """Stop verifies, then position flat before post-verify check."""

    def place_algo_stop(self, **kw: Any) -> dict[str, Any]:
        body = super().place_algo_stop(**kw)
        if kw.get("order_type") == "STOP_MARKET":
            self._positions = []
        return body


def test_r6_a50_entry_flat_after_verify_partial_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r6_a50.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.1)
    preview = _quick_preview(qty="0.1", card_id=card)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    trade = _PartialVerifiedThenFlatTrade(mark=Decimal("100"), fill_qty=Decimal("0.1"))
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status == "unknown", result
    conn.close()


class _PartialStopFailFlatTrade(_PartialFillTrade):
    """All STOP_MARKET clear-reject; after 2 attempts position flat → NOT_PROTECTED + flat."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._sl_n = 0

    def place_algo_stop(self, **kw: Any) -> dict[str, Any]:
        if kw.get("order_type") == "STOP_MARKET":
            self._sl_n += 1
            self.calls.append(("place_algo_stop", {**kw, "reduce_only": True}))
            raise TradeClientError("stop refused", http_status=400, binance_code=-2021)
        return super().place_algo_stop(**kw)

    def position_risk(self, symbol: str | None = None) -> list[_Pos]:
        # After both sl tags refused, final verify + post-check see flat.
        if self._sl_n >= 2:
            self._positions = []
        return super().position_risk(symbol=symbol)


def test_r6_a51_entry_flat_unverified_partial_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r6_a51.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.1)
    preview = _quick_preview(qty="0.1", card_id=card)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    trade = _PartialStopFailFlatTrade(mark=Decimal("100"), fill_qty=Decimal("0.1"))
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status == "unknown", result
    conn.close()


def test_r6_a52_recover_closed_by_stop_partial_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "r6_a52.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = _quick_preview(qty="0.05")
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
    insert_exec_log(
        conn,
        intent_id=iid,
        step="sl0",
        endpoint="/fapi/v1/algoOrder",
        client_order_id=client_order_id(iid, "sl0"),
        order_or_algo_id="8001",
        http_status=200,
        created_at_ms=ts,
    )
    mkt_cid = client_order_id(iid, "mkt")
    sl0 = client_order_id(iid, "sl0")

    class _RecoverClosedByStop(RecordingTrade):
        def __init__(self) -> None:
            super().__init__(mark=Decimal("100"), fill_qty=Decimal("0.05"))
            self._pos_reads = 0
            self._orders = [
                {
                    "symbol": "BTCUSDT",
                    "algoId": "8001",
                    "clientAlgoId": sl0,
                    "side": "SELL",
                    "type": "STOP_MARKET",
                    "orderType": "STOP_MARKET",
                    "quantity": "0.05",
                    "triggerPrice": "99",
                    "reduceOnly": True,
                    "algoStatus": "TRIGGERED",
                    "status": "TRIGGERED",
                    "workingType": "MARK_PRICE",
                    "positionSide": "BOTH",
                }
            ]

        def position_risk(self, symbol: str | None = None) -> list[_Pos]:
            self._pos_reads += 1
            self.calls.append(("position_risk", {"symbol": symbol}))
            # First read (recover entry): still open so we reach _verify_stop_ids.
            # Later reads (_verify_stop_ids): flat + TRIGGERED → CLOSED_BY_STOP.
            if self._pos_reads <= 1:
                return [_Pos("BTCUSDT", "long", "0.05", "100")]
            return []

        def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
            self.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
            if client_id == mkt_cid:
                return {
                    "orderId": 9101,
                    "status": "PARTIALLY_FILLED",
                    "executedQty": "0.05",
                    "avgPrice": "100",
                    "clientOrderId": client_id,
                }
            raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)

        def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
            self.calls.append(("query_order_by_client_id", {"symbol": symbol, "client_id": client_id}))
            return self.query_order(symbol=symbol, client_id=client_id)

    trade = _RecoverClosedByStop()
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    after = get_exec_intent(conn, iid)
    assert after is not None
    assert after["status"] == "unknown", after["status"]
    conn.close()


def test_r6_a54_main_emergency_no_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A54: main SL fail → emergency OK must not call attribution resolve / mkt_order_resolve."""
    import jarvise_assist.execute as execute_mod

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r6_a54.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "signal_id": "sig_r6_a54",
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
    claim_exec_intent(conn, iid)
    resolve_calls: list[str] = []
    real_ensure = execute_mod._ensure_mkt_order_id_logged

    def _track(*args: Any, **kwargs: Any) -> Any:
        resolve_calls.append("called")
        return real_ensure(*args, **kwargs)

    monkeypatch.setattr(execute_mod, "_ensure_mkt_order_id_logged", _track)
    trade = _TimeoutFallbackSlFailTrade(
        intent_id=iid, mark=Decimal("100"), fill_qty=Decimal("0.05"), resolve_oid="545454"
    )
    result = run_entry_sequence(conn, iid, trade=trade, origin=None, sleep=lambda _s: None)
    assert result.status == "failed", result
    rows = conn.execute(
        "SELECT step FROM assist_exec_log WHERE intent_id=? AND step='mkt_order_resolve'",
        (iid,),
    ).fetchall()
    assert rows == []
    assert resolve_calls == [], resolve_calls
    conn.close()


def test_r6_a59_pending_cleanup_past_max_age_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.watch import QUICK_JV_CLEANUP_STEP, _cleanup_exec_linked_quick_jv

    conn = open_db(tmp_path / "r6_a59.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    intent_a = "aei_r6ageA001"
    insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=ts - RECOVER_MAX_AGE_MS - 120_000,
        expires_at_ms=ts - RECOVER_MAX_AGE_MS - 90_000,
    )
    conn.execute("UPDATE assist_exec_intent SET id=? WHERE symbol='BTCUSDT'", (intent_a,))
    conn.commit()
    finish_exec_intent(conn, intent_a, status="done", result_text="done")
    closed_at = ts - RECOVER_MAX_AGE_MS - 1
    _seed_closed_exec_quick(conn, intent_id=intent_a, closed_at=closed_at)

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

    def _cancel(*_a: Any, **_k: Any) -> tuple[list[str], bool]:
        cancel_n["n"] += 1
        return ["901"], True

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    monkeypatch.setattr("jarvise_assist.watch.cancel_jv_orders_for_symbol", _cancel)
    cancelled = _cleanup_exec_linked_quick_jv(conn, positions=[], quick_info={"closed": []}, now_ms=ts)
    assert cancelled == []
    assert cancel_n["n"] == 0
    assert (
        conn.execute(
            "SELECT 1 FROM assist_exec_log WHERE intent_id=? AND step=?",
            (intent_a, QUICK_JV_CLEANUP_STEP),
        ).fetchone()
        is None
    )
    conn.close()
