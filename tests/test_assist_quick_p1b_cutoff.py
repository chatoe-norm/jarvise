"""Quick P1b: 23:30 cutoff alerts and D8 entry block."""

from __future__ import annotations

import ast
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_quick_card import _happy_fake, _seed_settings
from tests.test_assist_quick_m1a1_round1 import _flow_to_quoted, _run_qk

from jarvise_assist.futures import FuturesKeyUnsafeError, FuturesPermissionError
from jarvise_assist.quick_card import REFUSAL, handle_quick_register_callback, handle_quick_text
from jarvise_assist.quick_cutoff import (
    cutoff_block_text,
    in_entry_block,
    maybe_send_cutoff_alerts,
)
from jarvise_assist.quick_errors import e_cutoff_block
from jarvise_assist.quick_m1a1 import handle_quick_draft_text
from jarvise_assist.signals import is_night_hold
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db

T_232959 = _ms_bangkok(2026, 11, 23, 23, 29) + 59_000
T_233000 = _ms_bangkok(2026, 11, 23, 23, 30)
T_233100 = _ms_bangkok(2026, 11, 23, 23, 31)
T_233500 = _ms_bangkok(2026, 11, 23, 23, 35)
T_234210 = _ms_bangkok(2026, 11, 23, 23, 42) + 10_000
T_235500 = _ms_bangkok(2026, 11, 23, 23, 55)
T_235959 = _ms_bangkok(2026, 11, 23, 23, 59) + 59_000
T_000000 = _ms_bangkok(2026, 11, 24, 0, 0)
T_NEXT_233000 = _ms_bangkok(2026, 11, 24, 23, 30)

# Owner-approved / source-pinned Thai copy (item 11). Spec and source agree.
STATIC_REFUSAL = "❌ หลัง 23:30 ไม่ลงทะเบียน quick ใหม่ · 👉 เริ่มใหม่ได้หลัง 00:00"
LIVE_CUTOFF_18 = "❌ หลัง 23:30 ไม่ลงทะเบียน quick ใหม่ · cutoff 00:00 อีก 18 นาที · 👉 เริ่มใหม่ได้หลัง 00:00"
TRADE_ALERT_1H05 = (
    "⏰ ใกล้ cutoff 00:00 — ไม้ quick BTCUSDT LONG ยังเปิดอยู่ (ถือมา 1:05) "
    "· บอทไม่ปิดให้ — ถ้าไม่อยากถือข้ามวัน ปิดเองใน Binance"
)
TRADE_ALERT_0 = (
    "⏰ ใกล้ cutoff 00:00 — ไม้ quick BTCUSDT LONG ยังเปิดอยู่ (ถือมา 0:00) "
    "· บอทไม่ปิดให้ — ถ้าไม่อยากถือข้ามวัน ปิดเองใน Binance"
)
REG_ALERT = "⏰ ใกล้ cutoff 00:00 — ลงทะเบียน quick ETHUSDT SHORT ยังไม่เข้า · ไม่ควรเปิดหลังเที่ยงคืน"
REG_DEGRADED = (
    "⏰ ใกล้ cutoff 00:00 — ลงทะเบียน quick ETHUSDT SHORT "
    "ยืนยันสถานะไม่ได้ (อ่าน Binance ไม่ได้) · เช็กใน Binance ว่ามีไม้เปิดอยู่ไหม"
)


def _insert_trade(
    conn: Any,
    *,
    tid: str = "t1",
    rid: str = "r1",
    sym: str = "BTCUSDT",
    side: str = "long",
    status: str = "open",
    entry_time_ms: int | None = None,
    cutoff_day: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, status, entry_time_ms,
            updated_at_ms, cutoff_notice_day
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tid, rid, sym, side, status, entry_time_ms, T_233000, cutoff_day),
    )
    conn.commit()


def _insert_reg(
    conn: Any,
    *,
    rid: str = "reg1",
    sym: str = "ETHUSDT",
    side: str = "short",
    status: str = "open",
    expires_at_ms: int,
    cutoff_day: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, updated_at_ms, cutoff_notice_day
        ) VALUES (?, ?, ?, 100, 90, 0.01, ?, ?, ?, ?, ?)
        """,
        (rid, sym, side, T_233000 - 60_000, expires_at_ms, status, T_233000, cutoff_day),
    )
    conn.commit()


def _trade_alerts(sent: list[str]) -> list[str]:
    prefix = "⏰ ใกล้ cutoff 00:00 — ไม้ quick "
    return [m for m in sent if m.startswith(prefix)]


def _reg_alerts(sent: list[str]) -> list[str]:
    prefix = "⏰ ใกล้ cutoff 00:00 — ลงทะเบียน quick "
    return [m for m in sent if m.startswith(prefix)]


def _no_buttons(**kwargs: Any) -> None:
    markup = kwargs.get("reply_markup")
    assert markup is None or markup == {} or not (markup or {}).get("inline_keyboard")


def test_in_entry_block_edges() -> None:
    assert not in_entry_block(T_232959)
    assert in_entry_block(T_233000)
    assert in_entry_block(T_235959)
    assert not in_entry_block(T_000000)


def test_cutoff_block_live_text() -> None:
    text = cutoff_block_text(T_234210)
    assert text == LIVE_CUTOFF_18
    assert text == e_cutoff_block(minutes=18)
    assert "ลองใหม่" not in text
    assert len(text) <= 200


def test_refusal_cutoff_static() -> None:
    assert REFUSAL["cutoff_block"] == STATIC_REFUSAL
    assert "{m}" not in REFUSAL["cutoff_block"]


def test_owner_approved_thai_copy_exact(tmp_path: Path) -> None:
    """Item 11: full-string equality for every owner-facing cutoff string."""
    assert REFUSAL["cutoff_block"] == STATIC_REFUSAL
    assert e_cutoff_block(minutes=18) == LIVE_CUTOFF_18
    assert cutoff_block_text(T_234210) == LIVE_CUTOFF_18
    assert cutoff_block_text(T_233000) == e_cutoff_block(minutes=30)

    conn = open_db(tmp_path / "copy.db")
    entry_ms = T_233100 - 65 * 60_000
    _insert_trade(conn, entry_time_ms=entry_ms)
    _insert_reg(conn, expires_at_ms=T_233500)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=send, sync_ok=True)
    assert sent == [TRADE_ALERT_1H05, REG_ALERT]

    conn2 = open_db(tmp_path / "copy2.db")
    _insert_reg(conn2, expires_at_ms=T_233000 - 1)
    sent2: list[str] = []

    def send2(msg: str, **_k: Any) -> bool:
        sent2.append(msg)
        return True

    maybe_send_cutoff_alerts(conn2, now_ms=T_233100, send=send2, sync_ok=False)
    assert sent2 == [REG_DEGRADED]
    conn.close()
    conn2.close()


def test_maybe_send_outside_window_noop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "o.db")
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    assert maybe_send_cutoff_alerts(conn, now_ms=T_232959, send=send, sync_ok=True) == {}
    assert sent == []
    conn.close()


def test_maybe_send_outside_window_with_open_rows(tmp_path: Path) -> None:
    """Item 3: open trade+reg at 23:29:59 and 00:00:00 — no send, no row change."""
    conn = open_db(tmp_path / "win.db")
    _insert_trade(conn, entry_time_ms=T_233000 - 60_000)
    _insert_reg(conn, expires_at_ms=T_233500)
    before_trade = conn.execute(
        "SELECT id, status, cutoff_notice_day, entry_time_ms FROM assist_quick_trade"
    ).fetchall()
    before_reg = conn.execute(
        "SELECT id, status, cutoff_notice_day, expires_at_ms FROM assist_quick_registration"
    ).fetchall()
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    for when in (T_232959, T_000000):
        changes_before = conn.total_changes
        assert maybe_send_cutoff_alerts(conn, now_ms=when, send=send, sync_ok=True) == {}
        assert maybe_send_cutoff_alerts(conn, now_ms=when, send=send, sync_ok=False) == {}
        assert conn.total_changes == changes_before
    assert sent == []
    after_trade = conn.execute(
        "SELECT id, status, cutoff_notice_day, entry_time_ms FROM assist_quick_trade"
    ).fetchall()
    after_reg = conn.execute(
        "SELECT id, status, cutoff_notice_day, expires_at_ms FROM assist_quick_registration"
    ).fetchall()
    assert [dict(r) for r in after_trade] == [dict(r) for r in before_trade]
    assert [dict(r) for r in after_reg] == [dict(r) for r in before_reg]
    conn.close()


def test_alerts_sync_ok(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ok.db")
    entry_ms = T_233000 - 45 * 60_000
    _insert_trade(conn, entry_time_ms=entry_ms)
    _insert_reg(conn, expires_at_ms=T_233500)
    # expired reg — should skip
    _insert_reg(conn, rid="reg2", expires_at_ms=T_233000 - 1)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    counts = maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=send, sync_ok=True)
    assert len(sent) == 2
    assert sent[0] == (
        "⏰ ใกล้ cutoff 00:00 — ไม้ quick BTCUSDT LONG ยังเปิดอยู่ (ถือมา 0:46) "
        "· บอทไม่ปิดให้ — ถ้าไม่อยากถือข้ามวัน ปิดเองใน Binance"
    )
    assert sent[1] == REG_ALERT
    assert counts.get("trades_sent") == 1
    assert counts.get("regs_sent") == 1
    conn.close()


def test_alerts_sync_ok_false_degraded(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "bad.db")
    _insert_reg(conn, expires_at_ms=T_233000 - 5_000)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=send, sync_ok=False)
    assert sent == [REG_DEGRADED]
    conn.close()


def test_alerts_expires_at_equals_now_skipped_when_sync_ok(tmp_path: Path) -> None:
    """Item 6: expires_at_ms == now_ms → no reg alert when sync_ok=True."""
    conn = open_db(tmp_path / "eq.db")
    _insert_reg(conn, expires_at_ms=T_233100)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    counts = maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=send, sync_ok=True)
    assert sent == []
    assert counts.get("regs_sent") is None
    conn.close()


def test_hold_time_null_and_negative_clamp(tmp_path: Path) -> None:
    """Item 6: NULL entry and future entry → 0:00."""
    conn = open_db(tmp_path / "hold.db")
    _insert_trade(conn, tid="tn", rid="rn", entry_time_ms=None)
    _insert_trade(conn, tid="tf", rid="rf", entry_time_ms=T_233100 + 60_000)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=send, sync_ok=True)
    assert sent == [TRADE_ALERT_0, TRADE_ALERT_0]
    conn.close()


@pytest.mark.parametrize(
    "send_mode",
    ["false", "none", "raise"],
    ids=["send_false", "send_none", "send_raises"],
)
def test_send_false_counts_as_failed(tmp_path: Path, send_mode: str) -> None:
    """Item 6: send False/None/raise → *_failed, claim kept, no exception."""
    conn = open_db(tmp_path / f"ffc-{send_mode}.db")
    _insert_trade(conn)
    _insert_reg(conn, expires_at_ms=T_233500)

    def send(msg: str, **_k: Any) -> Any:
        if send_mode == "false":
            return False
        if send_mode == "none":
            return None
        raise RuntimeError("telegram down")

    counts = maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=send, sync_ok=True)
    assert counts == {"trades_failed": 1, "regs_failed": 1}
    trade = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='t1'").fetchone()
    reg = conn.execute("SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'").fetchone()
    assert trade["cutoff_notice_day"] == "2026-11-23"
    assert reg["cutoff_notice_day"] == "2026-11-23"
    conn.close()


def test_alerts_no_duplicate_same_day(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "dup.db")
    _insert_trade(conn)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=send, sync_ok=True)
    maybe_send_cutoff_alerts(conn, now_ms=T_233500, send=send, sync_ok=True)
    assert len(sent) == 1
    conn.close()


def test_cutoff_notice_once_per_bangkok_day_registration(tmp_path: Path) -> None:
    """Item 10: open registration notified once per Bangkok day (both sync paths)."""

    def _once_per_day(db_name: str, *, sync_ok: bool) -> None:
        conn = open_db(tmp_path / db_name)
        _insert_reg(conn, expires_at_ms=T_NEXT_233000 + 60_000)
        sent: list[str] = []

        def send(msg: str, **_k: Any) -> bool:
            sent.append(msg)
            return True

        for when in (T_233000, T_233500, T_235500):
            maybe_send_cutoff_alerts(conn, now_ms=when, send=send, sync_ok=sync_ok)
        assert len(sent) == 1
        row = conn.execute(
            "SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'"
        ).fetchone()
        assert row["cutoff_notice_day"] == "2026-11-23"
        maybe_send_cutoff_alerts(conn, now_ms=T_NEXT_233000, send=send, sync_ok=sync_ok)
        assert len(sent) == 2
        row2 = conn.execute(
            "SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'"
        ).fetchone()
        assert row2["cutoff_notice_day"] == "2026-11-24"
        conn.close()

    _once_per_day("regday-ok.db", sync_ok=True)
    _once_per_day("regday-bad.db", sync_ok=False)


def test_cutoff_notice_once_per_bangkok_day_trade(tmp_path: Path) -> None:
    """Item 10: open trade notified once per Bangkok day across watch ticks."""
    conn = open_db(tmp_path / "tradeday.db")
    _insert_trade(conn, entry_time_ms=T_233000 - 60_000)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    for when in (T_233000, T_233500, T_235500):
        maybe_send_cutoff_alerts(conn, now_ms=when, send=send, sync_ok=True)
    assert len(sent) == 1
    row = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='t1'").fetchone()
    assert row["cutoff_notice_day"] == "2026-11-23"
    maybe_send_cutoff_alerts(conn, now_ms=T_NEXT_233000, send=send, sync_ok=True)
    assert len(sent) == 2
    row2 = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='t1'").fetchone()
    assert row2["cutoff_notice_day"] == "2026-11-24"
    conn.close()


def test_alerts_next_day(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "nd.db")
    _insert_trade(conn, cutoff_day="2026-11-23")
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    maybe_send_cutoff_alerts(conn, now_ms=T_NEXT_233000, send=send, sync_ok=True)
    assert len(sent) == 1
    conn.close()


def test_closed_trade_and_expired_status_skipped(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "skip.db")
    _insert_trade(conn, status="closed")
    _insert_reg(conn, status="expired", expires_at_ms=T_233500)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=send, sync_ok=False)
    assert sent == []
    conn.close()


def test_send_failure_still_claims(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "sf.db")
    _insert_trade(conn)

    def boom(msg: str, **_k: Any) -> bool:
        raise RuntimeError("telegram down")

    maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=boom, sync_ok=True)
    row = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade").fetchone()
    assert row["cutoff_notice_day"] == "2026-11-23"


def test_send_false_still_claims(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ff.db")
    _insert_trade(conn, tid="t2", rid="r2")

    def nope(msg: str, **_k: Any) -> bool:
        return False

    maybe_send_cutoff_alerts(conn, now_ms=T_233100, send=nope, sync_ok=True)
    row = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='t2'").fetchone()
    assert row["cutoff_notice_day"] == "2026-11-23"
    conn.close()


def test_watch_no_auth_sends_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "na.db")
    _seed_settings(conn)
    _insert_reg(conn, expires_at_ms=T_233500)
    monkeypatch.setattr("jarvise_assist.watch.build_futures_client", lambda: None)
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    out = run_assist_watch(conn, futures=None, now_ms_value=T_233100, send=capture)
    assert out["error"] == "no_binance_auth"
    assert _reg_alerts(sent) == [REG_DEGRADED]
    conn.close()


def test_watch_night_hold_still_alerts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "nh.db")
    assert is_night_hold(now_ms=T_233100)
    _insert_trade(conn)
    _insert_reg(conn, expires_at_ms=T_233500)
    _insert_reg(conn, rid="reg2", expires_at_ms=T_233100 - 1)
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {},
    )
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    out = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_233100, send=capture)
    assert out["ok"] is True
    assert sent == [TRADE_ALERT_0, REG_ALERT]
    assert out["quick_cutoff"] == {"trades_sent": 1, "regs_sent": 1}
    conn.close()


def test_watch_sync_closes_trade_no_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "cls.db")
    _seed_settings(conn)
    _insert_trade(conn)

    def close_sync(c: Any, _client: Any, *, now_ms: int, send: Any) -> dict[str, Any]:
        c.execute("UPDATE assist_quick_trade SET status='closed' WHERE status='open'")
        c.commit()
        return {"closed": 1}

    monkeypatch.setattr("jarvise_assist.watch.sync_quick_trades", close_sync)
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_233100, send=capture)
    assert sent == []
    conn.close()


def test_watch_sync_raises_degraded_reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "sr.db")
    _seed_settings(conn)
    _insert_reg(conn, expires_at_ms=T_233500)

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("sync fail")

    monkeypatch.setattr("jarvise_assist.watch.sync_quick_trades", boom)
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_233100, send=capture)
    assert _reg_alerts(sent) == [REG_DEGRADED]
    conn.close()


def test_watch_build_client_raises_after_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "br.db")
    _insert_trade(conn)
    monkeypatch.setattr(
        "jarvise_assist.watch.build_futures_client",
        lambda: (_ for _ in ()).throw(FileNotFoundError("key")),
    )

    def capture(msg: str, **_k: Any) -> bool:
        return True

    with pytest.raises(FileNotFoundError):
        run_assist_watch(conn, futures=None, now_ms_value=T_233100, send=capture)
    row = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade").fetchone()
    assert row["cutoff_notice_day"] == "2026-11-23"
    conn.close()


def _seed_trade_and_reg(conn: Any) -> None:
    _seed_settings(conn)
    _insert_trade(conn, tid="tw", rid="rw", entry_time_ms=T_233000 - 60_000)
    _insert_reg(conn, expires_at_ms=T_233500)


def _assert_one_trade_and_degraded_reg(sent: list[str], *, hold: str = "0:02") -> None:
    trades = _trade_alerts(sent)
    regs = _reg_alerts(sent)
    assert len(trades) == 1
    assert trades[0] == (
        f"⏰ ใกล้ cutoff 00:00 — ไม้ quick BTCUSDT LONG ยังเปิดอยู่ (ถือมา {hold}) "
        "· บอทไม่ปิดให้ — ถ้าไม่อยากถือข้ามวัน ปิดเองใน Binance"
    )
    assert regs == [REG_DEGRADED]


def test_watch_assert_key_safe_network_raises(tmp_path: Path) -> None:
    """Item 2: key-probe RuntimeError — cutoff then re-raise."""
    conn = open_db(tmp_path / "ks.db")
    _seed_trade_and_reg(conn)
    fake = FakeFutures(key_error=RuntimeError("network"))
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    with pytest.raises(RuntimeError, match="network"):
        run_assist_watch(conn, futures=fake, now_ms_value=T_233100, send=capture)
    _assert_one_trade_and_degraded_reg(sent)
    row = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='tw'").fetchone()
    assert row["cutoff_notice_day"] == "2026-11-23"
    conn.close()


def test_watch_ensure_settings_raises_sends_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SHOULD 5: ensure_settings raise → degraded cutoff then re-raise."""
    conn = open_db(tmp_path / "es.db")
    _seed_trade_and_reg(conn)
    monkeypatch.setattr(
        "jarvise_assist.watch.ensure_settings",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("settings")),
    )
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    with pytest.raises(RuntimeError, match="settings"):
        run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_233100, send=capture)
    _assert_one_trade_and_degraded_reg(sent)
    trade = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='tw'").fetchone()
    reg = conn.execute("SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'").fetchone()
    assert trade["cutoff_notice_day"] == "2026-11-23"
    assert reg["cutoff_notice_day"] == "2026-11-23"
    conn.close()


def test_watch_maybe_auto_resume_raises_still_alerts_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SHOULD 5: maybe_auto_resume raise → log+continue; healthy cutoff via real sync_ok."""
    conn = open_db(tmp_path / "ar.db")
    _seed_trade_and_reg(conn)
    called = {"n": 0}

    def boom_resume(*_a: Any, **_k: Any) -> None:
        called["n"] += 1
        raise RuntimeError("resume")

    monkeypatch.setattr("jarvise_assist.watch.maybe_auto_resume", boom_resume)
    monkeypatch.setattr("jarvise_assist.watch.sync_quick_trades", lambda *_a, **_k: {})
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    out = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_233100, send=capture)
    assert out["ok"] is True
    assert called["n"] == 1
    trades = _trade_alerts(sent)
    regs = _reg_alerts(sent)
    assert len(trades) == 1
    assert "ถือมา 0:02" in trades[0]
    assert regs == [REG_ALERT]
    assert REG_DEGRADED not in sent
    trade = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='tw'").fetchone()
    reg = conn.execute("SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'").fetchone()
    assert trade["cutoff_notice_day"] == "2026-11-23"
    assert reg["cutoff_notice_day"] == "2026-11-23"
    conn.close()


def test_watch_ensure_settings_raises_outside_window_no_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SHOULD 5: ensure_settings raise outside window — no claim, no cutoff text."""
    conn = open_db(tmp_path / "eo.db")
    _seed_trade_and_reg(conn)
    monkeypatch.setattr(
        "jarvise_assist.watch.ensure_settings",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("settings")),
    )
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    with pytest.raises(RuntimeError, match="settings"):
        run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_232959, send=capture)
    assert _trade_alerts(sent) == []
    assert _reg_alerts(sent) == []
    trade = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='tw'").fetchone()
    reg = conn.execute("SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'").fetchone()
    assert trade["cutoff_notice_day"] is None
    assert reg["cutoff_notice_day"] is None
    conn.close()


def test_watch_ensure_settings_raise_then_later_tick_heals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SHOULD 5: failed first emit leaves day unclaimed; later tick still alerts."""
    conn = open_db(tmp_path / "heal.db")
    _seed_trade_and_reg(conn)
    # Keep reg unexpired at T_233500 so the healthy second tick still alerts it.
    conn.execute(
        "UPDATE assist_quick_registration SET expires_at_ms=? WHERE id='reg1'",
        (T_235500,),
    )
    conn.commit()
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    with monkeypatch.context() as m:
        m.setattr(
            "jarvise_assist.watch.ensure_settings",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("settings")),
        )
        m.setattr(
            "jarvise_assist.watch.maybe_send_cutoff_alerts",
            lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
        )
        with pytest.raises(RuntimeError, match="settings"):
            run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_233100, send=capture)

    assert sent == []
    trade = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='tw'").fetchone()
    reg = conn.execute("SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'").fetchone()
    assert trade["cutoff_notice_day"] is None
    assert reg["cutoff_notice_day"] is None

    monkeypatch.setattr("jarvise_assist.watch.sync_quick_trades", lambda *_a, **_k: {})
    out = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_233500, send=capture)
    assert out["ok"] is True
    trades = _trade_alerts(sent)
    regs = _reg_alerts(sent)
    assert len(trades) == 1
    assert regs == [REG_ALERT]
    trade = conn.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='tw'").fetchone()
    reg = conn.execute("SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'").fetchone()
    assert trade["cutoff_notice_day"] == "2026-11-23"
    assert reg["cutoff_notice_day"] == "2026-11-23"
    conn.close()


def test_watch_key_unsafe_returns_with_cutoff(tmp_path: Path) -> None:
    """Item 2: key-unsafe early return — one trade alert + degraded reg."""
    conn = open_db(tmp_path / "ku.db")
    _seed_trade_and_reg(conn)
    fake = FakeFutures(key_error=FuturesKeyUnsafeError("unsafe"))
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    out = run_assist_watch(conn, futures=fake, now_ms_value=T_233100, send=capture)
    assert not out.get("ok", True)
    assert out.get("error") == "key_unsafe"
    _assert_one_trade_and_degraded_reg(sent)
    assert out.get("quick_cutoff", {}).get("trades_sent") == 1
    assert out.get("quick_cutoff", {}).get("regs_sent") == 1
    conn.close()


def test_watch_permission_error_cutoff(tmp_path: Path) -> None:
    """Item 2: FuturesPermissionError — one trade alert + degraded reg."""
    conn = open_db(tmp_path / "pe.db")
    _seed_trade_and_reg(conn)
    fake = FakeFutures(permission_on_balance=FuturesPermissionError("code=-2015"))
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    out = run_assist_watch(conn, futures=fake, now_ms_value=T_233100, send=capture)
    assert out.get("error") == "permission_error"
    _assert_one_trade_and_degraded_reg(sent)
    assert out.get("quick_cutoff", {}).get("trades_sent") == 1
    assert out.get("quick_cutoff", {}).get("regs_sent") == 1
    conn.close()


def test_watch_futures_down_at_2330_cutoff(tmp_path: Path) -> None:
    """Item 2 MUST: Binance unreachable at 23:30 — cutoff still alerts."""
    conn = open_db(tmp_path / "fd.db")
    _seed_trade_and_reg(conn)
    fake = FakeFutures(read_error=RuntimeError("binance unreachable"))
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    out = run_assist_watch(conn, futures=fake, now_ms_value=T_233000, send=capture)
    assert out.get("error") == "futures_unavailable"
    trades = _trade_alerts(sent)
    regs = _reg_alerts(sent)
    assert len(trades) == 1
    assert trades[0] == (
        "⏰ ใกล้ cutoff 00:00 — ไม้ quick BTCUSDT LONG ยังเปิดอยู่ (ถือมา 0:01) "
        "· บอทไม่ปิดให้ — ถ้าไม่อยากถือข้ามวัน ปิดเองใน Binance"
    )
    assert regs == [REG_DEGRADED]
    assert out.get("quick_cutoff", {}).get("trades_sent") == 1
    assert out.get("quick_cutoff", {}).get("regs_sent") == 1
    conn.close()


def test_typed_q_cutoff_no_db_no_binance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 5: typed q refusal — no reply_markup, no rows, no Binance."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "tq.db")
    _seed_settings(conn)
    fake = _happy_fake()
    sent: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> int:
        sent.append({"text": text, **kwargs})
        return 1

    before = conn.total_changes
    handle_quick_text(
        conn,
        "q BTC short 0.4%",
        futures=fake,
        send=send,
        now_ms=T_233000,
    )
    assert conn.total_changes == before
    assert len(sent) == 1
    assert sent[0]["text"] == cutoff_block_text(T_233000)
    _no_buttons(**{k: v for k, v in sent[0].items() if k != "text"})
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_card").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_trade").fetchone()[0] == 0
    assert fake.calls == []
    conn.close()


def test_qk_rg_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 5: qk:rg refusal — answer(None) once, no buttons, no rows."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "rg.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake, now_ms=T_232959 - 50_000)
    card_before = conn.execute(
        "SELECT updated_at_ms, message_id, stop_kind, stop_value, status FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
    fake.calls.clear()
    before = conn.total_changes
    sent, answers, out, _ed = _run_qk(conn, f"qk:rg:{cid}", fake, now_ms=T_233000 + 5_000)
    assert conn.total_changes == before
    assert out.get("reason") == "cutoff_block"
    assert len(sent) == 1
    assert sent[0]["text"] == cutoff_block_text(T_233000 + 5_000)
    _no_buttons(**{k: v for k, v in sent[0].items() if k != "text"})
    assert answers == [None]
    row = conn.execute(
        "SELECT updated_at_ms, message_id, stop_kind, stop_value, status FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
    assert row["status"] == "quoted"
    assert row["updated_at_ms"] == card_before["updated_at_ms"]
    assert row["message_id"] == card_before["message_id"]
    assert row["stop_kind"] == card_before["stop_kind"]
    assert row["stop_value"] == card_before["stop_value"]
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_trade").fetchone()[0] == 0
    assert fake.calls == []
    conn.close()


def test_legacy_qr_cutoff(tmp_path: Path) -> None:
    """Item 5: legacy qr: refusal — no reply_markup."""
    conn = open_db(tmp_path / "qr.db")
    _seed_settings(conn)
    fake = _happy_fake()
    sent: list[dict[str, Any]] = []

    def send(text: str, **kwargs: Any) -> int:
        sent.append({"text": text, **kwargs})
        return 1

    created = T_233000 // 1000
    before = conn.total_changes
    handle_quick_register_callback(
        conn,
        f"qr:BTCUSDT:s:90000:{created}",
        futures=fake,
        send=send,
        now_ms=T_233100,
    )
    assert conn.total_changes == before
    assert len(sent) == 1
    assert sent[0]["text"] == cutoff_block_text(T_233100)
    _no_buttons(**{k: v for k, v in sent[0].items() if k != "text"})
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_trade").fetchone()[0] == 0
    assert fake.calls == []
    conn.close()


def test_qk_rg_after_midnight_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 1: quote at 00:00:00, qk:rg at 00:00:05 → registered."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "mid.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake, now_ms=T_000000)
    sent, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake, now_ms=T_000000 + 5_000)
    assert out.get("ok") is True
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 1
    row = conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "registered"
    del sent, answers
    conn.close()


def test_qk_sl_cutoff_no_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "sl.db")
    _seed_settings(conn)
    fake = _happy_fake()
    _run_qk(conn, "qk:sd:l", fake, now_ms=T_232959)
    row = conn.execute(
        "SELECT id FROM assist_quick_card WHERE status='pick_symbol' ORDER BY created_at_ms DESC"
    ).fetchone()
    cid = str(row["id"])
    _run_qk(conn, f"qk:sy:{cid}:BTCUSDT", fake, now_ms=T_232959)
    before = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, status, message_id FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
    fake.calls.clear()
    sent, _, out, edits = _run_qk(conn, f"qk:sl:{cid}:0.5", fake, now_ms=T_233100)
    after = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, status, message_id FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
    assert out.get("reason") == "cutoff_block"
    assert dict(before) == dict(after)
    assert fake.calls == []
    assert sent
    assert edits == []
    conn.close()


def test_qk_rf_cutoff_edit_fails_no_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 7 MUST: qk:rf at cutoff with edit failing — send only, no DB/Binance."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "rf.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake, now_ms=T_232959 - 50_000)
    before = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, status, message_id FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
    fake.calls.clear()
    changes_before = conn.total_changes
    sent, answers, out, edits = _run_qk(
        conn,
        f"qk:rf:{cid}",
        fake,
        now_ms=T_233100,
        edit_returns=False,
    )
    after = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, status, message_id FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
    assert out.get("reason") == "cutoff_block"
    assert out.get("ok") is False
    assert dict(before) == dict(after)
    assert before["message_id"] == after["message_id"]
    assert edits == []
    assert fake.calls == []
    assert conn.total_changes == changes_before
    assert len(sent) == 1
    assert sent[0]["text"] == cutoff_block_text(T_233100)
    assert answers == [None]
    conn.close()


def test_draft_pct_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 4: draft % at 23:30 — exact return, fields unchanged, no edit/Binance."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "dp.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"BTCUSDT": Decimal("100")})
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, stop_kind, stop_value, status,
            created_at_ms, updated_at_ms, message_id
        ) VALUES ('dc1', 'short', 'BTCUSDT', 'price', '130', 'pick_stop', ?, ?, 1)
        """,
        (T_232959, T_232959),
    )
    conn.commit()
    before = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, message_id FROM assist_quick_card WHERE id='dc1'"
    ).fetchone()
    fake.calls.clear()
    sent: list[str] = []
    edits: list[Any] = []

    def send(t: str, **_k: Any) -> int:
        sent.append(t)
        return 2

    def edit(mid: int, text: str, **_k: Any) -> bool:
        edits.append((mid, text))
        return True

    out = handle_quick_draft_text(
        conn,
        "0.4%",
        futures=fake,
        send=send,
        edit=edit,
        now_ms=T_233100,
    )
    assert out == {"handled": True, "rejected": True}
    after = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, message_id FROM assist_quick_card WHERE id='dc1'"
    ).fetchone()
    assert dict(after) == dict(before)
    assert edits == []
    assert fake.calls == []
    assert sent == [cutoff_block_text(T_233100)]
    conn.close()


def test_draft_price_cutoff_not_far(tmp_path: Path) -> None:
    """Item 4 + 9: draft price at 23:30 — exact return; readable assert when sent empty."""
    conn = open_db(tmp_path / "dpr.db")
    _seed_settings(conn)
    fake = _happy_fake(marks={"BTCUSDT": Decimal("100")})
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, stop_kind, stop_value, status,
            created_at_ms, updated_at_ms, message_id
        ) VALUES ('dc2', 'short', 'BTCUSDT', 'pct', '0.5', 'pick_stop', ?, ?, 1)
        """,
        (T_232959, T_232959),
    )
    conn.commit()
    before = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, message_id FROM assist_quick_card WHERE id='dc2'"
    ).fetchone()
    fake.calls.clear()
    sent: list[str] = []
    edits: list[Any] = []

    def send(t: str, **_k: Any) -> int:
        sent.append(t)
        return 1

    def edit(mid: int, text: str, **_k: Any) -> bool:
        edits.append((mid, text))
        return True

    out = handle_quick_draft_text(
        conn,
        "130",
        futures=fake,
        send=send,
        edit=edit,
        now_ms=T_233100,
    )
    assert out == {"handled": True, "rejected": True}
    after = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, message_id FROM assist_quick_card WHERE id='dc2'"
    ).fetchone()
    assert dict(after) == dict(before)
    assert edits == []
    assert fake.calls == []
    assert sent[0] == cutoff_block_text(T_233100)
    assert "เกิน 20%" not in sent[0]
    conn.close()


def test_draft_pct_range_before_cutoff(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "rng.db")
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, stop_kind, stop_value, status,
            created_at_ms, updated_at_ms, message_id
        ) VALUES ('dc3', 'long', 'BTCUSDT', 'pct', '0.5', 'pick_stop', ?, ?, 1)
        """,
        (T_232959, T_232959),
    )
    conn.commit()
    edits: list[str] = []

    def edit(_mid: int, text: str, **_k: Any) -> bool:
        edits.append(text)
        return True

    out = handle_quick_draft_text(
        conn,
        "20%",
        futures=_happy_fake(),
        send=lambda *_a, **_k: 1,
        edit=edit,
        now_ms=T_233100,
    )
    assert out and out.get("rejected")
    assert edits and "ต้องมากกว่า 0" in edits[0]
    row = conn.execute("SELECT stop_value FROM assist_quick_card WHERE id='dc3'").fetchone()
    assert row["stop_value"] == "0.5"
    conn.close()


def test_draft_garbage_returns_none(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "gar.db")
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, side, symbol, stop_kind, stop_value, status,
            created_at_ms, updated_at_ms
        ) VALUES ('dc4', 'long', 'BTCUSDT', 'pct', '0.5', 'pick_stop', ?, ?)
        """,
        (T_232959, T_232959),
    )
    conn.commit()
    assert (
        handle_quick_draft_text(
            conn,
            "hello",
            futures=_happy_fake(),
            send=lambda *_a, **_k: 1,
            edit=lambda *_a, **_k: True,
            now_ms=T_233100,
        )
        is None
    )
    conn.close()


def test_watch_cutoff_failure_does_not_break_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Watch survives cutoff failure: swallow, no quick_cutoff key."""
    conn = open_db(tmp_path / "wcf.db")
    _insert_trade(conn)
    monkeypatch.setattr(
        "jarvise_assist.watch.sync_quick_trades",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        "jarvise_assist.watch.maybe_send_cutoff_alerts",
        lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("cutoff boom")),
    )
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    out = run_assist_watch(conn, futures=FakeFutures(), now_ms_value=T_233100, send=capture)
    assert out["ok"] is True
    assert "quick_cutoff" not in out
    conn.close()


def test_cutoff_claim_durable_across_connections(tmp_path: Path) -> None:
    """Claim UPDATE is committed: second open_db connection sees cutoff_notice_day."""
    path = tmp_path / "claim.db"
    conn = open_db(path)
    _insert_trade(conn)
    _insert_reg(conn, expires_at_ms=T_233500)
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    maybe_send_cutoff_alerts(conn, now_ms=T_233000, send=send, sync_ok=True)
    conn.close()

    conn2 = open_db(path)
    trade = conn2.execute("SELECT cutoff_notice_day FROM assist_quick_trade WHERE id='t1'").fetchone()
    reg = conn2.execute("SELECT cutoff_notice_day FROM assist_quick_registration WHERE id='reg1'").fetchone()
    assert trade["cutoff_notice_day"] == "2026-11-23"
    assert reg["cutoff_notice_day"] == "2026-11-23"
    conn2.close()


def test_quick_cutoff_ast_import_ban() -> None:
    """Item 8: ban from-import aliases and submodules (redis.asyncio, etc.)."""
    from jarvise_assist import quick_cutoff

    path = Path(quick_cutoff.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned_roots = (
        "redis",
        "jarvise_trade",
        "jarvise_assist.execute",
        "jarvise_assist.futures",
        "jarvise_assist.pause",
        "jarvise_notify.quiet",
    )

    def _is_banned(name: str) -> bool:
        for root in banned_roots:
            if name == root or name.startswith(root + "."):
                return True
        return False

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1:
                mod = "jarvise_assist"
                if node.module:
                    mod = f"{mod}.{node.module}"
            else:
                mod = node.module or ""
            if mod:
                found.add(mod)
            for alias in node.names:
                if alias.name == "*":
                    continue
                if mod:
                    found.add(f"{mod}.{alias.name}")
                else:
                    found.add(alias.name)
    banned_hits = {name for name in found if _is_banned(name)}
    assert not banned_hits, f"banned imports in quick_cutoff.py: {banned_hits}"
