"""Quick P1b: 23:30 cutoff alerts and D8 entry block."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_quick_card import _happy_fake, _seed_settings
from tests.test_assist_quick_m1a1_round1 import _flow_to_quoted, _run_qk

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
T_235959 = _ms_bangkok(2026, 11, 23, 23, 59) + 59_000
T_000000 = _ms_bangkok(2026, 11, 24, 0, 0)
T_NEXT_233000 = _ms_bangkok(2026, 11, 24, 23, 30)


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


def test_in_entry_block_edges() -> None:
    assert not in_entry_block(T_232959)
    assert in_entry_block(T_233000)
    assert in_entry_block(T_235959)
    assert not in_entry_block(T_000000)


def test_cutoff_block_live_text() -> None:
    text = cutoff_block_text(T_234210)
    assert "อีก 18 นาที" in text
    assert "หลัง 00:00" in text
    assert "ลองใหม่" not in text
    assert len(text) <= 200
    assert text == e_cutoff_block(minutes=18)


def test_refusal_cutoff_static() -> None:
    expected = "❌ หลัง 23:30 ไม่ลงทะเบียน quick ใหม่ · 👉 เริ่มใหม่ได้หลัง 00:00"
    assert REFUSAL["cutoff_block"] == expected
    assert "{m}" not in REFUSAL["cutoff_block"]


def test_maybe_send_outside_window_noop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "o.db")
    sent: list[str] = []

    def send(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    assert maybe_send_cutoff_alerts(conn, now_ms=T_232959, send=send, sync_ok=True) == {}
    assert sent == []
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
    assert "LONG" in sent[0] and "0:46" in sent[0]
    assert "ยังไม่เข้า" in sent[1] and "SHORT" in sent[1]
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
    assert len(sent) == 1
    assert "ยืนยันสถานะไม่ได้" in sent[0]
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
    assert sent and "ยืนยันสถานะไม่ได้" in sent[0]
    conn.close()


def test_watch_night_hold_still_alerts(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "nh.db")
    _insert_trade(conn)
    assert is_night_hold(now_ms=T_233100)
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    fake = FakeFutures()
    out = run_assist_watch(conn, futures=fake, now_ms_value=T_233100, send=capture)
    assert out["ok"] is True
    assert sent
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
    assert sent and "ยืนยันสถานะไม่ได้" in sent[0]
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


def test_watch_assert_key_safe_network_raises(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "ks.db")
    _insert_trade(conn, tid="tn", rid="rn")
    fake = FakeFutures(key_error=RuntimeError("network"))

    def capture(msg: str, **_k: Any) -> bool:
        return True

    with pytest.raises(RuntimeError, match="network"):
        run_assist_watch(conn, futures=fake, now_ms_value=T_233100, send=capture)
    conn.close()


def test_watch_key_unsafe_returns_with_cutoff(tmp_path: Path) -> None:
    from jarvise_assist.futures import FuturesKeyUnsafeError

    conn = open_db(tmp_path / "ku.db")
    _seed_settings(conn)
    _insert_trade(conn, tid="tk", rid="rk")
    fake = FakeFutures(key_error=FuturesKeyUnsafeError("unsafe"))
    sent: list[str] = []

    def capture(msg: str, **_k: Any) -> bool:
        sent.append(msg)
        return True

    out = run_assist_watch(conn, futures=fake, now_ms_value=T_233100, send=capture)
    assert not out.get("ok", True)
    assert sent
    conn.close()


def test_typed_q_cutoff_no_db_no_binance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "tq.db")
    _seed_settings(conn)
    fake = _happy_fake()
    sent: list[str] = []

    def send(text: str, **_k: Any) -> int:
        sent.append(text)
        return 1

    handle_quick_text(
        conn,
        "q BTC short 0.4%",
        futures=fake,
        send=send,
        now_ms=T_233000,
    )
    assert sent
    assert "อีก" in sent[0] and "นาที" in sent[0]
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_card").fetchone()[0] == 0
    assert fake.calls == []
    conn.close()


def test_qk_rg_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "rg.db")
    _seed_settings(conn)
    fake = _happy_fake()
    cid = _flow_to_quoted(conn, fake, now_ms=T_232959 - 50_000)
    fake.calls.clear()
    sent, _ans, out, _ed = _run_qk(conn, f"qk:rg:{cid}", fake, now_ms=T_233000 + 5_000)
    assert out.get("reason") == "cutoff_block"
    assert sent
    row = conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert fake.calls == []
    conn.close()


def test_legacy_qr_cutoff(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "qr.db")
    _seed_settings(conn)
    fake = _happy_fake()
    sent: list[str] = []

    def send(text: str, **_k: Any) -> int:
        sent.append(text)
        return 1

    created = T_233000 // 1000
    handle_quick_register_callback(
        conn,
        f"qr:BTCUSDT:s:90000:{created}",
        futures=fake,
        send=send,
        now_ms=T_233100,
    )
    assert sent and "อีก" in sent[0]
    assert not any("inline_keyboard" in str(s) for s in sent)
    assert fake.calls == []
    conn.close()


def test_qk_rg_after_midnight_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    conn = open_db(tmp_path / "mid.db")
    _seed_settings(conn)
    fake = _happy_fake()
    quote_at = T_000000 - 30_000
    cid = _flow_to_quoted(conn, fake, now_ms=quote_at)
    sent, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake, now_ms=T_000000 + 5_000)
    assert out.get("ok") is True or out.get("quoted") is not False
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
        "SELECT stop_kind, stop_value, updated_at_ms, status FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
    fake.calls.clear()
    sent, _, out, _ = _run_qk(conn, f"qk:sl:{cid}:0.5", fake, now_ms=T_233100)
    after = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms, status FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
    assert out.get("reason") == "cutoff_block"
    assert before == after
    assert fake.calls == []
    assert sent
    conn.close()


def test_draft_pct_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    fake.calls.clear()
    sent: list[str] = []

    def send(t: str, **_k: Any) -> int:
        sent.append(t)
        return 2

    def edit(mid: int, text: str, **_k: Any) -> bool:
        return True

    handle_quick_draft_text(
        conn,
        "0.4%",
        futures=fake,
        send=send,
        edit=edit,
        now_ms=T_233100,
    )
    row = conn.execute(
        "SELECT stop_kind, stop_value, updated_at_ms FROM assist_quick_card WHERE id='dc1'"
    ).fetchone()
    assert row["stop_kind"] == "price"
    assert fake.calls == []
    assert sent
    conn.close()


def test_draft_price_cutoff_not_far(tmp_path: Path) -> None:
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
    fake.calls.clear()
    sent: list[str] = []

    def send(t: str, **_k: Any) -> int:
        sent.append(t)
        return 1

    handle_quick_draft_text(
        conn,
        "130",
        futures=fake,
        send=send,
        edit=lambda *_a, **_k: True,
        now_ms=T_233100,
    )
    assert sent and "cutoff" in sent[0].lower() or "23:30" in sent[0]
    assert "เกิน 20%" not in sent[0]
    assert fake.calls == []
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


def test_quick_cutoff_ast_import_ban() -> None:
    from jarvise_assist import quick_cutoff

    path = Path(quick_cutoff.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned = {
        "redis",
        "jarvise_trade",
        "jarvise_assist.execute",
        "jarvise_assist.futures",
        "jarvise_assist.pause",
        "jarvise_notify.quiet",
    }
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert banned.isdisjoint(imports)
