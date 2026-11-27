"""Quick Plan A1 — fix round 10 (ey: exception fallback expires proven-unfilled Quick reg)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    RecordingTrade,
    _daytime_ms,
    _insert_quoted_card,
    _quick_preview,
    _seed_quick_settings,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round2 import _ey_env  # noqa: E402

from jarvise_assist.execute import (
    MSG_BINANCE_NOT_SENT,
    MSG_OUTCOME_UNKNOWN,
    handle_exec_callback,
)
from jarvise_assist.store import get_exec_intent, insert_exec_intent
from jarvise_ingest.db import open_db


def _fail_step(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    import jarvise_assist.execute as ex
    import jarvise_assist.store as store_mod

    orig_insert = store_mod.insert_exec_log

    def _flaky(conn2: Any, **kwargs: Any) -> None:
        if kwargs.get("step") == step:
            raise RuntimeError(f"db down on {step}")
        return orig_insert(conn2, **kwargs)

    monkeypatch.setattr(store_mod, "insert_exec_log", _flaky)
    monkeypatch.setattr(ex, "insert_exec_log", _flaky)


def _quick_live_ey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_name: str,
    fail_step: str | None = None,
    boom_after_fill: bool = False,
) -> tuple[Any, str, RecordingTrade, list[str], dict[str, Any]]:
    """Run live Quick ey: through handle_exec_callback; return conn, iid, trade, sent, out."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / db_name)
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
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))
    sent: list[str] = []

    if fail_step is not None:
        _fail_step(monkeypatch, fail_step)

    if boom_after_fill:
        import jarvise_assist.execute as ex

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("post-send sequence boom")

        monkeypatch.setattr(ex, "_place_and_verify_stop", _boom)

    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda t, **_k: sent.append(str(t)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    return conn, iid, trade, sent, out


def test_r10_quick_position_baseline_fail_expires_reg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) position_baseline audit raises → no market, failed, registration expired, send once."""
    conn, iid, trade, sent, out = _quick_live_ey(
        tmp_path, monkeypatch, db_name="r10_a.db", fail_step="position_baseline"
    )
    assert out.get("reason") == "handler_error"
    assert not any(c[0] == "market_order" for c in trade.calls)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    assert intent["status"] == "failed"
    assert MSG_BINANCE_NOT_SENT in str(intent.get("result_text") or "")
    reg = conn.execute(
        "SELECT status FROM assist_quick_registration WHERE exec_intent_id=?",
        (iid,),
    ).fetchone()
    assert reg is not None
    assert str(reg["status"]) == "expired"
    assert len(sent) == 1
    conn.close()


def test_r10_quick_margin_type_fail_expires_reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) marginType audit raises → no market, failed, registration expired."""
    conn, iid, trade, sent, out = _quick_live_ey(
        tmp_path, monkeypatch, db_name="r10_b.db", fail_step="marginType"
    )
    assert out.get("reason") == "handler_error"
    assert not any(c[0] == "market_order" for c in trade.calls)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    assert intent["status"] == "failed"
    reg = conn.execute(
        "SELECT status FROM assist_quick_registration WHERE exec_intent_id=?",
        (iid,),
    ).fetchone()
    assert reg is not None
    assert str(reg["status"]) == "expired"
    assert len(sent) == 1
    conn.close()


def test_r10_quick_post_market_attempt_keeps_reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) Failure after market_attempt → unknown; registration stays open."""
    conn, iid, trade, sent, out = _quick_live_ey(
        tmp_path, monkeypatch, db_name="r10_c.db", boom_after_fill=True
    )
    assert out.get("reason") == "handler_error"
    assert any(c[0] == "market_order" for c in trade.calls)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    assert intent["status"] == "unknown"
    assert str(intent.get("result_text") or "") == MSG_OUTCOME_UNKNOWN
    attempt = conn.execute(
        "SELECT 1 FROM assist_exec_log WHERE intent_id=? AND step='market_attempt'",
        (iid,),
    ).fetchone()
    assert attempt is not None
    reg = conn.execute(
        "SELECT status FROM assist_quick_registration WHERE exec_intent_id=?",
        (iid,),
    ).fetchone()
    assert reg is not None
    assert str(reg["status"]) == "open"
    assert len(sent) == 1
    conn.close()


def test_r10_main_path_baseline_fail_no_reg_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) Main-path pre-market audit failure → failed; no Quick registration side effects."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    import jarvise_assist.execute as ex

    monkeypatch.setattr(ex, "_stale_card_levels", lambda *_a, **_k: None)
    monkeypatch.setattr(ex, "_confirm_guards_entry", lambda *_a, **_k: None)

    conn = open_db(tmp_path / "r10_d.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "signal_id": "sig_r10_main",
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
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))
    sent: list[str] = []
    _fail_step(monkeypatch, "position_baseline")

    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda t, **_k: sent.append(str(t)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("reason") == "handler_error"
    assert not any(c[0] == "market_order" for c in trade.calls)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    assert intent["status"] == "failed"
    assert MSG_BINANCE_NOT_SENT in str(intent.get("result_text") or "")
    regs = conn.execute("SELECT COUNT(*) AS n FROM assist_quick_registration").fetchone()
    assert regs is not None and int(regs["n"]) == 0
    assert len(sent) == 1
    conn.close()


def test_r10_expire_before_telegram_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(e) Expiry runs before Telegram send (send sees registration already expired)."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "r10_e.db")
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
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))
    _fail_step(monkeypatch, "position_baseline")

    seen_at_send: list[str] = []

    def _send(text: str, **_k: Any) -> bool:
        row = conn.execute(
            "SELECT status FROM assist_quick_registration WHERE exec_intent_id=?",
            (iid,),
        ).fetchone()
        seen_at_send.append(str(row["status"]) if row is not None else "missing")
        return True

    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=_send,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("reason") == "handler_error"
    assert seen_at_send == ["expired"]
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    assert intent["status"] == "failed"
    conn.close()
