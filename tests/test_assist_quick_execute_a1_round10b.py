"""Quick Plan A1 — fix round 10b (intent-scoped jv- cleanup strict algo list)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.test_assist_execute import _client  # noqa: E402
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    _daytime_ms,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round5 import _seed_closed_exec_quick  # noqa: E402

from jarvise_assist.execute import (
    cancel_jv_orders_for_symbol,
    client_order_id,
    finish_exec_intent,
)
from jarvise_assist.store import insert_exec_intent
from jarvise_assist.watch import QUICK_JV_CLEANUP_STEP, _cleanup_exec_linked_quick_jv
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# Transport that can return non-JSON HTTP-200 bodies (RecordingTransport always
# json-encodes). Exercises the real BinanceTradeClient parser.
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


def _delete_count(transport: _FlexibleTransport) -> int:
    return sum(1 for c in transport.calls if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder")


def _algo_get_count(transport: _FlexibleTransport) -> int:
    return sum(1 for c in transport.calls if c[0] == "GET" and c[1] == "/fapi/v1/openAlgoOrders")


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


def _intent_orders(intent_id: str) -> list[dict[str, Any]]:
    return [
        {
            "symbol": "BTCUSDT",
            "algoId": 901,
            "clientAlgoId": client_order_id(intent_id, "sl"),
            "reduceOnly": True,
        },
        {
            "symbol": "BTCUSDT",
            "algoId": 902,
            "clientAlgoId": client_order_id(intent_id, "tp"),
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


def _cleanup_handlers(
    *,
    algo_body: Any,
) -> dict[tuple[str, str], Any]:
    return {
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/openAlgoOrders"): (200, algo_body),
        ("DELETE", "/fapi/v1/algoOrder"): (200, {"code": "200", "msg": "success"}),
    }


def _marker_rows(conn: Any, intent_id: str) -> list[Any]:
    return conn.execute(
        "SELECT step FROM assist_exec_log WHERE intent_id=? AND step=?",
        (intent_id, QUICK_JV_CLEANUP_STEP),
    ).fetchall()


# ---------------------------------------------------------------------------
# (a)+(b)+(c)+(d) — malformed → retry → marker once → idempotent; owner untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_body",
    [
        "algo-not-json-at-all",
        {"code": 0, "msg": "not a list"},
    ],
)
def test_r10b_malformed_then_retry_cancels_marker_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_body: Any
) -> None:
    """Intent-scoped cleanup: malformed 200 → no marker; healthy next pass cancels + marks."""
    conn = open_db(tmp_path / "r10b_retry.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    intent_a = "aei_r10bA00123"
    _seed_done_quick_intent(conn, intent_id=intent_a, ts=ts)

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")

    # --- pass (a): malformed openAlgoOrders → no cancel, no marker ---
    transport_a = _FlexibleTransport(_cleanup_handlers(algo_body=bad_body))
    trade_a = _client(transport_a)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade_a, None))

    cancelled_a = _cleanup_exec_linked_quick_jv(conn, positions=[], quick_info={"closed": []}, now_ms=ts)
    assert cancelled_a == []
    assert _delete_count(transport_a) == 0
    assert _algo_get_count(transport_a) >= 1
    assert _marker_rows(conn, intent_a) == []

    # Direct contract: intent_id path returns not-confirmed on malformed.
    transport_direct = _FlexibleTransport(_cleanup_handlers(algo_body=bad_body))
    ids_d, ok_d = cancel_jv_orders_for_symbol(_client(transport_direct), "BTCUSDT", intent_id=intent_a)
    assert ids_d == []
    assert ok_d is False
    assert _delete_count(transport_direct) == 0

    # --- pass (b): healthy list → cancel this intent's SL/TP only; marker once ---
    transport_b = _FlexibleTransport(_cleanup_handlers(algo_body=_intent_orders(intent_a)))
    trade_b = _client(transport_b)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade_b, None))

    cancelled_b = _cleanup_exec_linked_quick_jv(
        conn, positions=[], quick_info={"closed": []}, now_ms=ts + 1_000
    )
    cancelled_ids = sorted(c["algo_id"] for c in cancelled_b)
    assert cancelled_ids == ["901", "902"], cancelled_b
    assert _delete_count(transport_b) == 2
    # Owner + other-intent algo ids must never appear in DELETE params.
    deleted_algo_ids = {
        str(c[2].get("algoId"))
        for c in transport_b.calls
        if c[0] == "DELETE" and c[1] == "/fapi/v1/algoOrder"
    }
    assert deleted_algo_ids == {"901", "902"}
    assert "903" not in deleted_algo_ids
    assert "904" not in deleted_algo_ids
    rows = _marker_rows(conn, intent_a)
    assert len(rows) == 1
    assert rows[0]["step"] == QUICK_JV_CLEANUP_STEP

    # --- pass (c): marker present → zero cancel / zero algo list ---
    transport_c = _FlexibleTransport(_cleanup_handlers(algo_body=_intent_orders(intent_a)))
    trade_c = _client(transport_c)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade_c, None))

    cancelled_c = _cleanup_exec_linked_quick_jv(
        conn, positions=[], quick_info={"closed": []}, now_ms=ts + 2_000
    )
    assert cancelled_c == []
    assert _delete_count(transport_c) == 0
    assert _algo_get_count(transport_c) == 0
    assert len(_marker_rows(conn, intent_a)) == 1
    conn.close()


# ---------------------------------------------------------------------------
# (e) — main callers (no intent_id) unchanged on malformed payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_body",
    [
        "algo-not-json-at-all",
        {"code": 0, "msg": "not a list"},
    ],
)
def test_r10b_main_no_intent_id_malformed_still_ok(bad_body: Any) -> None:
    """Without intent_id, malformed 200 still collapses to [] / cancel_ok=True."""
    transport = _FlexibleTransport(_cleanup_handlers(algo_body=bad_body))
    cancelled, ok = cancel_jv_orders_for_symbol(_client(transport), "BTCUSDT")
    assert cancelled == []
    assert ok is True
    assert _delete_count(transport) == 0
    assert _algo_get_count(transport) == 1
