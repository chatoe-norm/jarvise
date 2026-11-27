"""Quick Plan A1 — fix round 11b, Antigravity items.

Missing position reader fails closed (known-orderId branch); qk:ex same-side
refusal shows the configured cap; tp1pct_0 confirm text; half-close note only
when a TP1 order is placed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    RecordingTrade,
    _daytime_ms,
    _insert_quoted_card,
    _seed_quick_settings,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round7 import _seed_unresolved_exec_reg  # noqa: E402
from tests.test_assist_quick_execute_a1_round11 import _reg_status  # noqa: E402

from jarvise_assist.quick import REG_EXPIRE_GRACE_MS, sync_quick_trades
from jarvise_assist.quick_execute import (
    MSG_QUICK_HALF_NOTE,
    MSG_QUICK_NO_TP1,
    MSG_QUICK_TP1_OFF,
    format_quick_confirm_text,
)
from jarvise_assist.quick_m1a1 import handle_quick_trade_callback
from jarvise_ingest.db import open_db


def _reg_expires(conn: Any, rid: int) -> int:
    row = conn.execute("SELECT expires_at_ms FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert row is not None
    return int(row["expires_at_ms"])


class _NoPositionClient:
    """History reads only; no ``position_risk`` and no ``_signed_get``."""

    def user_trades(self, symbol: str, **_k: Any) -> list[dict[str, Any]]:
        return []

    def all_orders(self, symbol: str, **_k: Any) -> list[dict[str, Any]]:
        return []


def test_r11b_ag_known_order_id_no_position_reader_keeps(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r11b_nopos.db")
    _seed_settings(conn)
    created = _daytime_ms()
    _iid, rid, _mkt = _seed_unresolved_exec_reg(conn, created=created, order_id_logged="777001")
    past = _reg_expires(conn, rid) + REG_EXPIRE_GRACE_MS + 1
    notices: list[str] = []
    sync_quick_trades(
        conn,
        _NoPositionClient(),  # type: ignore[arg-type]
        now_ms=past,
        send=lambda text, **_k: notices.append(str(text)) or True,
    )
    assert _reg_status(conn, rid) == "open"
    assert notices == []
    conn.close()


@pytest.mark.parametrize("cap", [1, 3])
def test_r11b_ag_qk_ex_same_side_text_uses_config_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: int
) -> None:
    from jarvise_assist.quick_config import set_quick_config

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    conn = open_db(tmp_path / f"r11b_cap{cap}.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    set_quick_config(conn, "quick_same_side_cap", cap, source="test", now_ms=ts)
    monkeypatch.setattr("jarvise_assist.quick_execute.count_open_same_side", lambda _c, _s: cap)
    monkeypatch.setattr("jarvise_assist.quick_cap.count_open_same_side", lambda _c, _s: cap)
    cid = _insert_quoted_card(conn, now_ms_value=ts, size_mode="risk")
    sent: list[str] = []
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=RecordingTrade(mark=Decimal("100")),
        send=lambda text, **_k: sent.append(str(text)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    assert out.get("reason") == "same_side_cap"
    assert any(f"เพดานฝั่งละ {cap})" in s for s in sent), sent
    assert not any("ไม่ลงทะเบียน" in s for s in sent)
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == 0
    conn.close()


def _plan(**over: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "qty": Decimal("0.1"),
        "entry": Decimal("100"),
        "stop": Decimal("99"),
        "t1": Decimal("101"),
        "tp_half_qty": None,
        "tp_refuse": None,
        "leverage": 3,
        "loss_at_stop_incl_fees": Decimal("0.12"),
        "risk_pct_used": Decimal("0.25"),
    }
    plan.update(over)
    return plan


def _text(plan: dict[str, Any]) -> str:
    return format_quick_confirm_text(plan, symbol="BTCUSDT", side="long", ttl_s=30, tick=Decimal("0.01"))


def test_r11b_ag_confirm_tp1pct_zero_text() -> None:
    text = _text(_plan(tp_refuse="tp1pct_0"))
    assert MSG_QUICK_TP1_OFF in text
    assert MSG_QUICK_NO_TP1 not in text
    assert MSG_QUICK_HALF_NOTE not in text


def test_r11b_ag_confirm_dust_text_no_half_note() -> None:
    text = _text(_plan(tp_refuse="dust_close"))
    assert MSG_QUICK_NO_TP1 in text
    assert MSG_QUICK_TP1_OFF not in text
    assert MSG_QUICK_HALF_NOTE not in text


def test_r11b_ag_confirm_tp_present_keeps_half_note() -> None:
    text = _text(_plan(tp_half_qty=Decimal("0.05")))
    assert "TP1 101" in text
    assert MSG_QUICK_HALF_NOTE in text
    assert MSG_QUICK_NO_TP1 not in text and MSG_QUICK_TP1_OFF not in text
