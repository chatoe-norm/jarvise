"""Quick Plan A1 — fix round 2 (reviewer findings + mutation killers)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Reuse fakes / helpers from the A1 suite
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    RecordingTrade,
    _daytime_ms,
    _full_cfg,
    _insert_quoted_card,
    _Pos,
    _quick_preview,
    _seed_quick_settings,
    _seed_settings,
)

from jarvise_assist.execute import (
    TradeClientError,
    client_order_id,
    finish_exec_intent,
    handle_exec_callback,
)
from jarvise_assist.quick_execute import (
    BTN_QUICK_BOT_ENTER,
    quick_entry_proven_unfilled,
    symbol_in_quick_exec_universe,
)
from jarvise_assist.quick_m1a1 import _quoted_markup_for_card, handle_quick_trade_callback
from jarvise_assist.store import (
    claim_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
    now_ms,
    write_settings,
)
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# Part A1 — recovery unresolved emergency close stays unknown (flag OFF)
# ---------------------------------------------------------------------------


class _RecoveryTrade(RecordingTrade):
    """Filled entry present; stop place fails; emergency close never confirms flat."""

    def __init__(self, *, intent_id: str, symbol: str = "BTCUSDT") -> None:
        super().__init__(
            mark=Decimal("100"),
            positions=[_Pos(symbol, "long", "0.05", "100")],
            stop_ok=False,
        )
        self._mkt_cid = client_order_id(intent_id, "mkt")
        self.fills.append(
            {
                "orderId": 777001,
                "status": "FILLED",
                "executedQty": "0.05",
                "avgPrice": "100",
                "clientOrderId": self._mkt_cid,
            }
        )
        self.close_attempts = 0

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        if client_id == self._mkt_cid:
            return {
                "orderId": 777001,
                "status": "FILLED",
                "executedQty": "0.05",
                "avgPrice": "100",
                "clientOrderId": client_id,
            }
        return super().query_order(symbol=symbol, client_id=client_id)

    def query_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        # Clear missing → NOT_PROTECTED (not UNVERIFIABLE) so recovery reaches emergency close.
        raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)

    def find_algo_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any] | None:
        return None

    def market_order(
        self, *, symbol: str, side: str, qty: Decimal, client_id: str, reduce_only: bool = False
    ) -> dict[str, Any]:
        if reduce_only:
            self.close_attempts += 1
            self.calls.append(
                (
                    "market_order",
                    {
                        "symbol": symbol,
                        "side": side,
                        "qty": qty,
                        "client_id": client_id,
                        "reduce_only": True,
                    },
                )
            )
            raise TradeClientError("close refused", http_status=400, binance_code=-2019)
        return super().market_order(
            symbol=symbol, side=side, qty=qty, client_id=client_id, reduce_only=reduce_only
        )


def test_a1_recovery_unresolved_emergency_stays_unknown_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quick-origin recovery: unconfirmed emergency close must stay unknown (flag off)."""
    from jarvise_assist.execute import _recover_entry_intent

    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "a1_rec.db")
    _seed_settings(conn)
    ts = now_ms()
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
    finish_exec_intent(conn, iid, status="unknown", result_text="u", expect_status="preview")
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = _RecoveryTrade(intent_id=iid)
    _recover_entry_intent(
        conn,
        trade,
        intent,
        now_ms_value=ts + 1_000,
        send=None,
        sleep=lambda _s: None,
    )
    row = get_exec_intent(conn, iid)
    assert row is not None
    assert str(row["status"]) == "unknown", row
    assert trade.close_attempts >= 1
    conn.close()


# ---------------------------------------------------------------------------
# Part A2 — recovered entries tag via recover_market_query + orderId-only fills
# ---------------------------------------------------------------------------


def test_a2_exec_linked_tag_via_recover_market_query_order_id_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Log step recover_market_query + fill with only orderId must tag the registration."""
    from jarvise_assist.execute import client_order_id as cid_fn
    from jarvise_assist.quick import create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "a2_tag.db")
    _seed_settings(conn)
    created = 1_700_000_200_000
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.1", "entry": "100", "stop": "99"},
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    mkt_cid = cid_fn(iid, "mkt")
    # Only recovery query logged the order id — no 'market' / 'recovery' step.
    insert_exec_log(
        conn,
        intent_id=iid,
        step="recover_market_query",
        endpoint="/fapi/v1/order",
        client_order_id=mkt_cid,
        order_or_algo_id="555666",
        http_status=200,
        created_at_ms=created,
    )
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.1,
        now_ms=created,
        exec_intent_id=iid,
    )
    assert rid is not None

    notices: list[str] = []

    def _fills(_c: Any, symbol: str, **_k: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": 9,
                "orderId": 555666,
                # No clientOrderId — attribution must use orderId from recover_market_query.
                "qty": "0.1",
                "price": "100",
                "time": created + 1_000,
                "side": "BUY",
                "commission": "0",
                "commissionAsset": "USDT",
                "realizedPnl": "0",
            }
        ]

    import jarvise_assist.quick as qmod

    orig = qmod.fetch_all_user_trades
    qmod.fetch_all_user_trades = _fills  # type: ignore[assignment]
    try:
        info = sync_quick_trades(
            conn, object(), now_ms=created + 5_000, send=lambda t, **_: notices.append(t)
        )
    finally:
        qmod.fetch_all_user_trades = orig  # type: ignore[assignment]
    assert int(info.get("tagged") or 0) == 1
    st = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert st is not None and str(st["status"]) == "tagged"
    conn.close()


# ---------------------------------------------------------------------------
# Part A3 — clear market rejection expires registration immediately
# ---------------------------------------------------------------------------


class _ClearRejectTrade(RecordingTrade):
    def market_order(
        self, *, symbol: str, side: str, qty: Decimal, client_id: str, reduce_only: bool = False
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "market_order",
                {
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "client_id": client_id,
                    "reduce_only": reduce_only,
                },
            )
        )
        raise TradeClientError("Insufficient margin", http_status=400, binance_code=-2019)


def test_a3_clear_entry_rejection_expires_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clear market rejection → registration expired; symbol free for a new bot entry."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "a3_exp.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.05)
    preview = _quick_preview(card_id=card)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    trade = _ClearRejectTrade(mark=Decimal("100"))
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is False
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    assert str(intent["status"]) == "failed"
    reg = conn.execute(
        "SELECT status FROM assist_quick_registration WHERE exec_intent_id=?", (iid,)
    ).fetchone()
    assert reg is not None and str(reg["status"]) == "expired"
    # Symbol free again for bot (no open Quick / in-flight).
    from jarvise_assist.quick_execute import symbol_busy_for_bot

    assert symbol_busy_for_bot(conn, "BTCUSDT") is None
    conn.close()


# ---------------------------------------------------------------------------
# Part A4 — testnet chase must refuse movement beyond limit
# ---------------------------------------------------------------------------


def test_a4_testnet_chase_refuses_beyond_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_quick_entry

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "a4_chase.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    # Preview entry/stop at 100/99 (R=1). chase_r=0.3 → refuse when mark > 100.3
    preview = _quick_preview()
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
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100.5"), balance=Decimal("10000"))
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1_000) == "chase"
    conn.close()


# ---------------------------------------------------------------------------
# Part B — mutation killers
# ---------------------------------------------------------------------------


def _ey_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )


def _mutating_calls(trade: RecordingTrade) -> list[str]:
    return [
        c[0]
        for c in trade.calls
        if c[0] in {"market_order", "set_isolated", "set_leverage", "place_algo_stop"}
    ]


def _insert_ready_intent(
    conn: Any,
    *,
    side: str = "long",
    qty: str = "0.05",
    preview_extra: dict[str, Any] | None = None,
    ts: int | None = None,
) -> tuple[str, int, str]:
    ts_v = ts if ts is not None else _daytime_ms()
    card = _insert_quoted_card(conn, side=side, now_ms_value=ts_v, qty=float(qty))
    preview = _quick_preview(side=side, qty=qty, card_id=card)
    if preview_extra:
        preview.update(preview_extra)
        if "cfg" in preview_extra:
            preview["cfg"] = {**_full_cfg(), **preview_extra["cfg"]}
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol=str(preview.get("symbol") or "BTCUSDT"),
        side=side,
        preview=preview,
        created_at_ms=ts_v,
        expires_at_ms=ts_v + 30_000,
    )
    return iid, ts_v, card


def test_b_guard_flag_off_zero_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "b_flag.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    iid, ts, _ = _insert_ready_intent(conn)
    trade = RecordingTrade(mark=Decimal("100"))
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is False
    assert _mutating_calls(trade) == []
    conn.close()


@pytest.mark.parametrize("reason", ["streak", "week_loss", "day_loss"])
def test_b_guard_pause_any_reason_zero_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / f"b_pause_{reason}.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    write_settings(conn, paused_until_ms=ts + 3_600_000, pause_reason=reason)
    iid, _, _ = _insert_ready_intent(conn, ts=ts)
    trade = RecordingTrade(mark=Decimal("100"))
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is False
    assert out.get("reason") == "paused"
    assert _mutating_calls(trade) == []
    conn.close()


def test_b_guard_bot_night_zero_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "b_night.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    tz = timezone(timedelta(hours=7))
    night = datetime(2026, 11, 26, 1, 0, tzinfo=tz)
    ts = int(night.timestamp() * 1000)
    iid, _, _ = _insert_ready_intent(conn, ts=ts)
    trade = RecordingTrade(mark=Decimal("100"))
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is False
    assert out.get("reason") == "night"
    assert _mutating_calls(trade) == []
    conn.close()


def test_b_guard_frozen_night_union_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Frozen night still blocks even when live window was shortened."""
    from jarvise_assist.execute import _confirm_guards_quick_entry

    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "b_fnight.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    # Live config: shorter night ending 06:30; frozen still ends 07:30.
    from jarvise_assist.quick_config import set_quick_config

    ts_day = _daytime_ms()
    set_quick_config(conn, "quick_night_end", "06:30", source="test", now_ms=ts_day)
    tz = timezone(timedelta(hours=7))
    # 07:00 is free under live 06:30 end, but blocked under frozen 07:30.
    at = datetime(2026, 11, 26, 7, 0, tzinfo=tz)
    ts = int(at.timestamp() * 1000)
    preview = _quick_preview()
    preview["cfg"] = _full_cfg(quick_night_start="23:30", quick_night_end="07:30")
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
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100"))
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1) == "night"
    conn.close()


def test_b_guard_universe_core_and_explore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_quick_entry
    from jarvise_assist.quick_config import set_quick_config
    from jarvise_assist.store import insert_explore_symbol

    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "b_uni.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    set_quick_config(conn, "quick_exec_universe", "core", source="test", now_ms=ts)
    # core: non-core coin refused
    preview = _quick_preview()
    preview["cfg"] = _full_cfg(quick_exec_universe="core")
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="PEPEUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100"))
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1) == "universe"

    # core_explore: explore coin accepted by helper
    insert_explore_symbol(conn, "PEPEUSDT", ts)
    assert symbol_in_quick_exec_universe(conn, "PEPEUSDT", universe="core_explore") is True
    assert symbol_in_quick_exec_universe(conn, "PEPEUSDT", universe="core") is False
    # core_explore with a non-explore non-core coin refused
    assert symbol_in_quick_exec_universe(conn, "ZZZUSDT", universe="core_explore") is False
    conn.close()


def test_b_guard_same_side_cap_uses_min_frozen_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_quick_entry
    from jarvise_assist.quick import create_quick_registration
    from jarvise_assist.quick_config import set_quick_config

    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "b_ss.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    # Live cap 3, frozen cap 1 → min=1. One open long → block.
    set_quick_config(conn, "quick_same_side_cap", 3, source="test", now_ms=ts)
    create_quick_registration(
        conn,
        symbol="ETHUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.01,
        now_ms=ts,
    )
    preview = _quick_preview()
    preview["cfg"] = _full_cfg(quick_same_side_cap=1)
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
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100"))
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1) == "same_side_cap"
    # If mutant used max(frozen=1, live=3)=3 with only 1 open, would pass — ensure still blocked.
    conn.close()


def test_b_guard_open_position_refuses_before_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "b_pos.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    iid, ts, _ = _insert_ready_intent(conn)
    trade = RecordingTrade(mark=Decimal("100"), positions=[_Pos("BTCUSDT")])
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is False
    assert out.get("reason") == "open_position"
    assert _mutating_calls(trade) == []
    conn.close()


@pytest.mark.parametrize("side", ["long", "short"])
def test_b_guard_stop_buffer_and_fee_r(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str) -> None:
    from jarvise_assist.execute import _confirm_guards_quick_entry

    _ey_env(monkeypatch)
    conn = open_db(tmp_path / f"b_buf_{side}.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    # Tight stop vs mark → stop_buffer (defaults: 0.1% / 2 ticks)
    preview = _quick_preview(side=side)
    if side == "long":
        preview["stop"] = "99.95"
        preview["entry"] = "100"
    else:
        preview["stop"] = "100.05"
        preview["entry"] = "100"
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side=side,
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100"))
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1) == "stop_buffer"

    # Fee-in-R with default cfg: r=0.3 → fee_r = 0.0012*100/0.3 = 0.4 > 0.30 max
    preview2 = _quick_preview(side=side)
    if side == "long":
        preview2["stop"] = "99.7"
        preview2["entry"] = "100"
        preview2["t1"] = "100.3"
        preview2["target"] = "100.3"
    else:
        preview2["stop"] = "100.3"
        preview2["entry"] = "100"
        preview2["t1"] = "99.7"
        preview2["target"] = "99.7"
    iid2 = insert_exec_intent(
        conn,
        kind="entry",
        symbol="ETHUSDT",
        side=side,
        preview=preview2,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid2)
    intent2 = get_exec_intent(conn, iid2)
    assert intent2 is not None
    assert _confirm_guards_quick_entry(conn, intent2, trade, now_ms_value=ts + 1) == "fee_r"
    conn.close()


def test_b_guard_qty_min_not_max_when_balance_allows_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qty = min(resized, preview) — mutant max would send more than preview."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "b_qty.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    # Tiny preview qty; huge balance would size much larger.
    iid, ts, _ = _insert_ready_intent(conn, qty="0.05")
    trade = RecordingTrade(mark=Decimal("100"), balance=Decimal("1_000_000"), fill_qty=Decimal("0.05"))
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is True, out
    mkts = [c for c in trade.calls if c[0] == "market_order" and not c[1].get("reduce_only")]
    assert len(mkts) == 1
    assert mkts[0][1]["qty"] <= Decimal("0.05")
    conn.close()


def test_b_guard_budget_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_quick_entry

    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "b_bud.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    monkeypatch.setattr(
        "jarvise_assist.execute.real_stop_loss_usdt",
        lambda **_: Decimal("999"),
    )
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
    claim_exec_intent(conn, iid)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100"), balance=Decimal("10000"))
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1) == "budget_exceeded"
    conn.close()


def test_b_guard_leverage_clamped_to_5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When notional/balance would need >5×, set_leverage must still be ≤5."""
    from jarvise_assist.quick_config import set_quick_config
    from jarvise_assist.sizing import SignalCandidate

    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "b_lev.db")
    _seed_settings(conn)
    _seed_quick_settings(conn, risk=2.0, cap=5.0)
    ts = _daytime_ms()
    set_quick_config(conn, "quick_risk_pct", "2.0", source="test", now_ms=ts)
    set_quick_config(conn, "quick_risk_cap_pct", "5", source="test", now_ms=ts)
    set_quick_config(conn, "quick_max_fee_r", "0.5", source="test", now_ms=ts)

    # Pass through the pre-shared leverage so the min(uncapped, lev_cap) clamp is observable.
    def _passthrough(admitted: list[Any], **_k: Any) -> tuple[list[Any], list[Any]]:
        return list(admitted), []

    monkeypatch.setattr("jarvise_assist.execute.assign_shared_leverage", _passthrough)

    card = _insert_quoted_card(conn, now_ms_value=ts, qty=100.0, stop=99.7)
    preview = _quick_preview(qty="100.0", card_id=card)
    preview["stop"] = "99.7"
    preview["entry"] = "100"
    preview["t1"] = "100.3"
    preview["target"] = "100.3"
    preview["risk_pct_used"] = "2.0"
    preview["cfg"] = _full_cfg(
        quick_risk_pct=Decimal("2.0"),
        quick_risk_cap_pct=Decimal("5"),
        risk_used="2.0",
        quick_max_fee_r=Decimal("0.5"),
    )
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    # Small balance → after fee sizing notional/balance still > 5 (uncapped ≥ 6).
    trade = RecordingTrade(
        mark=Decimal("100"),
        balance=Decimal("100"),
        available=Decimal("100"),
        fill_qty=Decimal("100"),
    )
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is True, out
    lev_calls = [c for c in trade.calls if c[0] == "set_leverage"]
    assert lev_calls, trade.calls
    assert all(int(c[1]["leverage"]) <= 5 for c in lev_calls)
    assert max(int(c[1]["leverage"]) for c in lev_calls) == 5
    del SignalCandidate
    conn.close()


@pytest.mark.parametrize("side,tp1_r", [("long", "1.0"), ("long", "1.5"), ("short", "1.0"), ("short", "1.5")])
def test_b_tp1_exact_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str, tp1_r: str) -> None:
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / f"b_tp1_{side}_{tp1_r}.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, side=side, now_ms_value=ts, qty=0.2)
    preview = _quick_preview(side=side, qty="0.2", card_id=card, tp1pct="50")
    preview["cfg"] = _full_cfg(quick_tp1_r=Decimal(tp1_r), quick_tp1_close_pct=Decimal("50"))
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side=side,
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    avg = Decimal("100")
    stop = Decimal(str(preview["stop"]))
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.2"), fill_avg=avg)
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is True, out
    tps = [c for c in trade.calls if c[0] == "place_algo_stop" and c[1]["order_type"] == "TAKE_PROFIT_MARKET"]
    assert len(tps) == 1
    r_dist = abs(avg - stop)
    expected = avg + Decimal(tp1_r) * r_dist if side == "long" else avg - Decimal(tp1_r) * r_dist
    # tick round to 0.01
    expected = expected.quantize(Decimal("0.01"))
    assert tps[0][1]["trigger"] == expected
    conn.close()


def test_b_proven_unfilled_false_with_fill_evidence(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "b_proven.db")
    _seed_settings(conn)
    ts = now_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, iid, status="failed", result_text="เข้าไม้ไม่สำเร็จ", expect_status="preview")
    # Fill evidence (stop) must force False even with clear-fail text
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="1",
        http_status=200,
        created_at_ms=ts,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="stop",
        endpoint="/fapi/v1/algoOrder",
        client_order_id=client_order_id(iid, "sl0"),
        order_or_algo_id="2",
        http_status=200,
        created_at_ms=ts + 1,
    )
    assert quick_entry_proven_unfilled(conn, iid) is False
    # emergency_close evidence too
    iid2 = insert_exec_intent(
        conn,
        kind="entry",
        symbol="ETHUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    finish_exec_intent(conn, iid2, status="failed", result_text="x", expect_status="preview")
    insert_exec_log(
        conn,
        intent_id=iid2,
        step="emergency_close",
        endpoint="/fapi/v1/order",
        http_status=200,
        created_at_ms=ts,
    )
    assert quick_entry_proven_unfilled(conn, iid2) is False
    conn.close()


def test_b_real_trades_excludes_quick_order_id_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quick fill matched only by orderId (empty clientOrderId) must not enter assist_real_trade."""
    from jarvise_assist.real_trades import build_real_rows

    conn = open_db(tmp_path / "b_oid.db")
    _seed_settings(conn)
    ts = now_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "entry": "100", "stop": "99", "qty": "0.1", "loss_at_stop": "0.1"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="424242",
        http_status=200,
        created_at_ms=ts,
    )

    class _Client:
        def all_orders(self, symbol: str, **_k: Any) -> list[dict[str, Any]]:
            if symbol != "BTCUSDT":
                return []
            return [
                {
                    "symbol": symbol,
                    "orderId": 424242,
                    "clientOrderId": "",  # no cid — only orderId ties to Quick
                    "side": "BUY",
                    "status": "FILLED",
                    "time": ts,
                    "updateTime": ts,
                }
            ]

        def user_trades(self, symbol: str, **_k: Any) -> list[dict[str, Any]]:
            if symbol != "BTCUSDT":
                return []
            return [
                {
                    "id": 1,
                    "orderId": 424242,
                    "symbol": symbol,
                    "side": "BUY",
                    "price": "100",
                    "qty": "0.1",
                    "time": ts,
                    "commission": "0",
                    "commissionAsset": "USDT",
                }
            ]

        def income(self, **_k: Any) -> list[Any]:
            return []

    monkeypatch.setattr(
        "jarvise_assist.real_trades.fetch_all_orders",
        lambda client, symbol, **k: client.all_orders(symbol),
    )
    monkeypatch.setattr(
        "jarvise_assist.real_trades.fetch_all_user_trades",
        lambda client, symbol, **k: client.user_trades(symbol),
    )
    monkeypatch.setattr("jarvise_assist.real_trades.fetch_all_income", lambda *_a, **_k: [])
    rows = build_real_rows(conn, _Client(), since_ms=ts - 1, until_ms=ts + 100)
    assert rows == []
    assert conn.execute("SELECT COUNT(*) FROM assist_real_trade").fetchone()[0] == 0
    conn.close()


def test_b_sync_tag_notice_exec_linked_vs_record_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.quick import create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "b_tag.db")
    _seed_settings(conn)
    created = 1_700_000_300_000
    notices: list[str] = []

    def _send(text: str, **_k: Any) -> None:
        notices.append(text)

    import jarvise_assist.quick as qmod

    def _fill_factory(order_id: int, t: int) -> Any:
        def _fills(_c: Any, symbol: str, **_k: Any) -> list[dict[str, Any]]:
            return [
                {
                    "id": order_id,
                    "orderId": order_id,
                    "qty": "0.1",
                    "price": "100",
                    "time": t,
                    "side": "BUY",
                    "commission": "0",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "clientOrderId": f"jv-x-{order_id}-mkt",
                }
            ]

        return _fills

    # Exec-linked: suppress notice
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="111",
        http_status=200,
        created_at_ms=created,
    )
    rid_ex, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.1,
        now_ms=created,
        exec_intent_id=iid,
    )
    assert rid_ex is not None
    orig = qmod.fetch_all_user_trades
    qmod.fetch_all_user_trades = _fill_factory(111, created + 10)  # type: ignore[assignment]
    try:
        sync_quick_trades(conn, object(), now_ms=created + 5_000, send=_send)
    finally:
        qmod.fetch_all_user_trades = orig  # type: ignore[assignment]
    assert not any("จับไม้ quick ได้แล้ว" in n for n in notices)

    # Record-only: notice sent
    notices.clear()
    rid_ro, _ = create_quick_registration(
        conn,
        symbol="ETHUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.1,
        now_ms=created + 10_000,
    )
    assert rid_ro is not None
    qmod.fetch_all_user_trades = _fill_factory(222, created + 10_100)  # type: ignore[assignment]
    try:
        sync_quick_trades(conn, object(), now_ms=created + 15_000, send=_send)
    finally:
        qmod.fetch_all_user_trades = orig  # type: ignore[assignment]
    assert any("จับไม้ quick ได้แล้ว" in n for n in notices)
    conn.close()


def test_b_cleanup_no_cancel_when_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.watch import _cleanup_exec_linked_quick_jv

    conn = open_db(tmp_path / "b_inflight.db")
    _seed_settings(conn)
    intent_btc = "aei_inflightbtc01"
    trade = RecordingTrade()
    trade._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "701",
            "clientAlgoId": client_order_id(intent_btc, "sl0"),
            "reduceOnly": True,
        }
    ]
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))
    # Insert in-flight exec on the symbol
    ts = now_ms()
    insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    # claim → confirmed = in flight
    row = conn.execute(
        "SELECT id FROM assist_exec_intent WHERE symbol='BTCUSDT' ORDER BY created_at_ms DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    claim_exec_intent(conn, str(row["id"]))
    cancelled = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],
        quick_info={
            "closed": [
                {
                    "symbol": "BTCUSDT",
                    "exec_linked": True,
                    "exec_intent_id": intent_btc,
                    "closed_at_ms": ts - 1_000,
                }
            ]
        },
        now_ms=ts,
    )
    assert cancelled == []
    assert not any(c[0].startswith("cancel") for c in trade.calls)
    conn.close()


def test_b_qk_ex_flag_off_size_mode_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    conn = open_db(tmp_path / "b_qk.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    cid = _insert_quoted_card(conn, now_ms_value=ts, size_mode="risk")
    trade = RecordingTrade(mark=Decimal("100"))
    handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=trade,
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert _mutating_calls(trade) == []
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == 0

    # size_mode != risk
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    cid2 = _insert_quoted_card(conn, symbol="ETHUSDT", now_ms_value=ts + 1, size_mode="min_lot")
    trade2 = RecordingTrade(mark=Decimal("100"))
    handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid2}",
        futures=trade2,
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts + 1,
        message={"message_id": 2, "chat": {"id": 1}},
    )
    assert _mutating_calls(trade2) == []
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent WHERE symbol='ETHUSDT'").fetchone()[0] == 0

    # pause blocks qk:ex
    write_settings(conn, paused_until_ms=ts + 3_600_000, pause_reason="streak")
    cid3 = _insert_quoted_card(conn, symbol="SOLUSDT", now_ms_value=ts + 2, size_mode="risk")
    trade3 = RecordingTrade(mark=Decimal("100"))
    handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid3}",
        futures=trade3,
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts + 2,
        message={"message_id": 3, "chat": {"id": 1}},
    )
    assert _mutating_calls(trade3) == []
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent WHERE symbol='SOLUSDT'").fetchone()[0] == 0
    conn.close()


def test_b_quoted_markup_flag_off_never_bot_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "b_mk.db")
    _seed_settings(conn)
    card = {
        "ok": True,
        "symbol": "BTCUSDT",
        "size_mode": "risk",
        "bot_eligible": True,
    }
    markup = _quoted_markup_for_card(conn, "aqc_core", card)
    flat = json.dumps(markup, ensure_ascii=False)
    assert BTN_QUICK_BOT_ENTER not in flat
    assert "qk:ex:" not in flat
    conn.close()
