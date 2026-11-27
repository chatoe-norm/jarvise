"""Quick Plan A1 — fix round 9b (strict order reads + not_found expire)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.test_assist_execute import _client  # noqa: E402
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    RecordingTrade,
    _daytime_ms,
    _full_cfg,
    _insert_quoted_card,
    _quick_preview,
    _seed_quick_settings,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round2 import _ey_env  # noqa: E402

from jarvise_assist.execute import (
    MSG_ORDER_NOT_FOUND,
    RECOVER_NOT_FOUND_PASSES,
    RECOVER_NOT_FOUND_SPAN_MS,
    TradeClientError,
    _confirm_guards_quick_entry,
    _reason_th,
    _recover_entry_intent,
    client_order_id,
    finish_exec_intent,
)
from jarvise_assist.quick import create_quick_registration
from jarvise_assist.quick_execute import maybe_expire_quick_registration, quick_entry_proven_unfilled
from jarvise_assist.store import (
    claim_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
)
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# Transport that can return non-JSON HTTP-200 bodies (RecordingTransport always
# json-encodes). Used only to exercise real BinanceTradeClient parsing.
# ---------------------------------------------------------------------------


class _FlexibleTransport(httpx.BaseTransport):
    """Like RecordingTransport, but body may be raw text/bytes for malformed 200s."""

    def __init__(self, handlers: dict[tuple[str, str], Any]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        params = dict(request.url.params)
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
        if isinstance(body, (bytes, bytearray)):
            return httpx.Response(status, content=bytes(body))
        if isinstance(body, str):
            return httpx.Response(status, content=body.encode("utf-8"))
        return httpx.Response(status, json=body)


def _btc_exchange_info() -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                ],
            }
        ]
    }


def _guard_base_handlers(
    overrides: dict[tuple[str, str], Any] | None = None,
) -> dict[tuple[str, str], Any]:
    handlers: dict[tuple[str, str], Any] = {
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openOrders"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": "100"}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, _btc_exchange_info()),
        ("GET", "/fapi/v3/balance"): (
            200,
            [{"asset": "USDT", "balance": "10000", "availableBalance": "10000"}],
        ),
    }
    if overrides:
        handlers.update(overrides)
    return handlers


def _seed_quick_intent(conn: Any, *, ts: int) -> str:
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.05)
    preview = _quick_preview(qty="0.05", card_id=card)
    preview["cfg"] = _full_cfg()
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
    return iid


# ---------------------------------------------------------------------------
# 1. High — strict openOrders / openAlgoOrders in Quick confirm guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path,bad_body",
    [
        ("/fapi/v1/openOrders", "not-json-at-all"),
        ("/fapi/v1/openOrders", {"oops": "dict-not-list"}),
        ("/fapi/v1/openAlgoOrders", "algo-not-json"),
        ("/fapi/v1/openAlgoOrders", {"code": 0, "msg": "not a list"}),
    ],
)
def test_r9b_strict_order_read_refuses_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_path: str, bad_body: Any
) -> None:
    """Real BinanceTradeClient + malformed 200 → guard refuses; market never sent."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "r9b_strict.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    iid = _seed_quick_intent(conn, ts=ts)
    intent = get_exec_intent(conn, iid)
    assert intent is not None

    bad = {("GET", bad_path): (200, bad_body)}
    transport = _FlexibleTransport(_guard_base_handlers(bad))
    trade = _client(transport)
    # Prove the real client collapses non-strict openOrders to [] on this payload.
    if bad_path.endswith("openOrders"):
        assert trade.open_orders(strict=False) == []
        with pytest.raises(TradeClientError):
            trade.open_orders(strict=True)
    else:
        assert trade.open_algo_orders(strict=False) == []
        with pytest.raises(TradeClientError):
            trade.open_algo_orders(strict=True)

    # Fresh client for the guard (prior calls may have populated filters cache).
    transport2 = _FlexibleTransport(_guard_base_handlers(bad))
    trade2 = _client(transport2)
    guard = _confirm_guards_quick_entry(conn, intent, trade2, now_ms_value=ts + 1)
    assert guard == "fapi_read_failed:orders"
    th = _reason_th(guard)
    assert "อ่านบัญชีไม่ได้" in th
    # Hard rule: the retry phrase must not appear in owner-facing copy.
    assert "\u0e25\u0e2d\u0e07\u0e43\u0e2b\u0e21\u0e48" not in th
    # Guard refuses before any order send — no market POST on this client.
    assert not any(c[0] == "POST" and c[1] == "/fapi/v1/order" for c in transport2.calls)
    assert not hasattr(trade2, "fills") or getattr(trade2, "fills", []) == []
    conn.close()


def test_r9b_strict_empty_lists_pass_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Well-formed empty openOrders + openAlgoOrders → guard continues (None)."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "r9b_ok.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    iid = _seed_quick_intent(conn, ts=ts)
    intent = get_exec_intent(conn, iid)
    assert intent is not None

    from jarvise_assist.sizing import SizedSignal

    def _sized(**kw: Any) -> SizedSignal:
        return SizedSignal(
            symbol=kw["symbol"],
            side=kw["side"],
            entry=kw["entry"],
            stop=kw["stop"],
            target=kw["entry"],
            qty=Decimal("0.05"),
            notional=Decimal("5"),
            leverage=2,
            loss_at_stop=Decimal("0.05"),
            risk_pct=kw["risk_pct"],
            r_distance=abs(kw["entry"] - kw["stop"]),
            leverage_capped=False,
            uncapped_leverage=2,
            analysis_id=None,
            step_size=Decimal("0.001"),
            balance_usdt=Decimal("10000"),
            budget_usdt=Decimal("50"),
        )

    monkeypatch.setattr("jarvise_assist.execute.size_signal", _sized)
    monkeypatch.setattr("jarvise_assist.execute.below_min_notional", lambda **_: False)
    monkeypatch.setattr(
        "jarvise_assist.execute.assign_shared_leverage",
        lambda admitted, **_: (list(admitted), []),
    )
    monkeypatch.setattr(
        "jarvise_assist.execute.half_close_legs",
        lambda *_a, **_k: (Decimal("0"), Decimal("0"), "skip"),
    )
    monkeypatch.setattr(
        "jarvise_assist.quick_execute.check_quick_and_portfolio_caps",
        lambda *_a, **_k: None,
    )

    transport = _FlexibleTransport(_guard_base_handlers())
    trade = _client(transport)
    assert trade.open_orders(strict=True) == []
    assert trade.open_algo_orders(strict=True) == []
    guard = _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1)
    assert guard is None, guard
    paths = [c[1] for c in transport.calls]
    assert "/fapi/v1/openOrders" in paths
    assert "/fapi/v1/openAlgoOrders" in paths
    conn.close()


def test_r9b_all_open_orders_still_non_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Main helper all_open_orders stays non-strict (unchanged) on malformed 200."""
    _ = monkeypatch
    transport = _FlexibleTransport(
        {
            ("GET", "/fapi/v1/openOrders"): (200, "garbage"),
            ("GET", "/fapi/v1/openAlgoOrders"): (200, {"not": "list"}),
        }
    )
    trade = _client(transport)
    # Non-strict collapse to [] — this is the main-path behaviour we must not change.
    assert trade.all_open_orders() == []
    with pytest.raises(TradeClientError):
        trade.open_orders(strict=True)


def test_r9b_position_risk_already_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """position_risk raises on non-list (incl. non-JSON→{raw}); Quick guard refuses."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "r9b_pos.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    iid = _seed_quick_intent(conn, ts=ts)
    intent = get_exec_intent(conn, iid)
    assert intent is not None

    # Non-JSON → {"raw": ...} → not a list → TradeClientError inside position_risk.
    trade = _client(
        _FlexibleTransport(_guard_base_handlers({("GET", "/fapi/v3/positionRisk"): (200, "not-json")}))
    )
    with pytest.raises(TradeClientError):
        trade.position_risk()

    trade2 = _client(
        _FlexibleTransport(_guard_base_handlers({("GET", "/fapi/v3/positionRisk"): (200, {"not": "list"})}))
    )
    guard = _confirm_guards_quick_entry(conn, intent, trade2, now_ms_value=ts + 1)
    assert guard == "fapi_read_failed:positions"
    conn.close()


# ---------------------------------------------------------------------------
# 2. Medium — not_found proven unfilled → expire registration
# ---------------------------------------------------------------------------


def test_r9b_not_found_recovery_expires_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """market_attempt only → NOT_FOUND×passes while flat → not_found → reg expired."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    conn = open_db(tmp_path / "r9b_nf.db")
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
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market_attempt",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        created_at_ms=ts,
    )
    finish_exec_intent(conn, iid, status="unknown", result_text="unknown")
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=ts,
        exec_intent_id=iid,
    )
    assert rid is not None
    # Two prior recover_not_found passes; third at +SPAN completes the threshold.
    t0 = ts + 60_000
    for offset in (0, 5 * 60_000):
        conn.execute(
            """
            INSERT INTO assist_exec_log (intent_id, step, endpoint, binance_code, created_at_ms)
            VALUES (?, 'recover_not_found', '/fapi/v1/order', -2013, ?)
            """,
            (iid, t0 + offset),
        )
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()

    class _NfTrade(RecordingTrade):
        def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
            self.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
            raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)

        def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
            return self.query_order(symbol=symbol, client_id=client_id)

    trade = _NfTrade(mark=Decimal("100"), positions=[])
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    assert intent["status"] == "recovering"
    # Before recovery: market_attempt + unknown keeps registration.
    assert quick_entry_proven_unfilled(conn, iid) is False

    now = t0 + RECOVER_NOT_FOUND_SPAN_MS
    assert RECOVER_NOT_FOUND_PASSES == 3
    _recover_entry_intent(conn, trade, intent, now_ms_value=now, send=None, sleep=lambda _s: None)

    row = get_exec_intent(conn, iid)
    assert row is not None
    assert row["status"] == "not_found"
    assert MSG_ORDER_NOT_FOUND in str(row.get("result_text") or "")
    assert quick_entry_proven_unfilled(conn, iid) is True
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and str(reg["status"]) == "expired"
    conn.close()


def test_r9b_not_found_with_fill_evidence_keeps_registration(tmp_path: Path) -> None:
    """Fill-evidence step present → not_found does NOT expire registration."""
    conn = open_db(tmp_path / "r9b_fill.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=_quick_preview(),
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market_attempt",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="stop",
        endpoint="/fapi/v1/algoOrder",
        client_order_id=client_order_id(iid, "sl0"),
        order_or_algo_id="8001",
    )
    finish_exec_intent(conn, iid, status="not_found", result_text=MSG_ORDER_NOT_FOUND)
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=ts,
        exec_intent_id=iid,
    )
    assert quick_entry_proven_unfilled(conn, iid) is False
    maybe_expire_quick_registration(conn, iid, now_ms=ts + 1)
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and str(reg["status"]) == "open"
    conn.close()


@pytest.mark.parametrize("status", ["unknown", "recovering"])
def test_r9b_unknown_recovering_still_keep(tmp_path: Path, status: str) -> None:
    """unknown / recovering with market_attempt still keep the registration."""
    conn = open_db(tmp_path / f"r9b_{status}.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=_quick_preview(),
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market_attempt",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
    )
    if status == "unknown":
        finish_exec_intent(conn, iid, status="unknown", result_text="x")
    else:
        finish_exec_intent(conn, iid, status="unknown", result_text="x")
        conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
        conn.commit()
    assert quick_entry_proven_unfilled(conn, iid) is False
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=ts,
        exec_intent_id=iid,
    )
    maybe_expire_quick_registration(conn, iid, now_ms=ts + 1)
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and str(reg["status"]) == "open"
    conn.close()
