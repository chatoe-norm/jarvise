"""Display-only 🔎 coin-trend ranking (no admission / order-path changes)."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import FakeFutures, _ms_bangkok, _seed_analysis

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.keyboard import BTN_ANYTHING
from jarvise_assist.menu import handle_menu_text, render_anything_now, render_coin_trend
from jarvise_assist.signals import scan_signal_candidates
from jarvise_assist.sizing import (
    MAX_OPEN_IDEAS,
    ExistingIdea,
    SignalCandidate,
    admit_signals,
    portfolio_risk_cap_pct,
    size_signal,
)
from jarvise_assist.store import ensure_settings, insert_signal, write_settings
from jarvise_assist.trend_view import (
    _fit_telegram,
    _utf16_len,
    explain_risk_rejections,
)
from jarvise_ingest.db import open_db

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_RAW_REASON_KEYS = re.compile(
    r"\b(risk_cap|slot_cap|risk_budget|no_backfill|mark:\w+|size_failed|"
    r"below_min_notional|open_position|low_confidence|stale_analysis)\b"
)


def _seed_fresh(
    conn: Any, *, symbol: str, action: str, confidence: float, invalidation: float, now: int
) -> None:
    _seed_analysis(
        conn,
        symbol=symbol,
        action=action,
        confidence=confidence,
        invalidation=invalidation,
        ts=now - 60_000,
    )


def test_coin_trend_held_btc_risk_budget_ranking(tmp_path: Path) -> None:
    """BTC held (~3%+ risk) + six shorts at risk_pct 3 → one ranking message, no markup."""
    conn = open_db(tmp_path / "trend_held.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=3.0, enabled=True)
    now = _ms_bangkok(2026, 11, 17, 12, 0)
    # BTC long held: qty×|entry−stop| = 30.01 → 3.001% of 1000 (displays 3.0%).
    btc_pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        update_time_ms=now,
    )
    btc_stop = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "stopPrice": "69.99",
        "reduceOnly": True,
        "positionSide": "BOTH",
    }
    _seed_fresh(conn, symbol="BTCUSDT", action="long", confidence=0.72, invalidation=69.99, now=now)
    shorts = (
        ("ETHUSDT", 0.90, Decimal("2000"), 2060.0),
        ("SOLUSDT", 0.85, Decimal("100"), 103.0),
        ("BNBUSDT", 0.80, Decimal("500"), 515.0),
        ("ADAUSDT", 0.75, Decimal("0.40"), 0.412),
        ("DOGEUSDT", 0.70, Decimal("0.10"), 0.103),
        ("XRPUSDT", 0.65, Decimal("0.50"), 0.515),
    )
    marks: dict[str, Decimal] = {"BTCUSDT": Decimal("100")}
    steps: dict[str, Decimal] = {"BTCUSDT": Decimal("0.001")}
    for sym, conf, mark, inv in shorts:
        _seed_fresh(conn, symbol=sym, action="short", confidence=conf, invalidation=inv, now=now)
        marks[sym] = mark
        steps[sym] = Decimal("0.001") if sym in {"ETHUSDT", "SOLUSDT", "BNBUSDT"} else Decimal("1")

    before_sig = conn.execute("SELECT COUNT(*) FROM assist_signal").fetchone()[0]
    before_track = conn.execute("SELECT COUNT(*) FROM assist_track").fetchone()[0]
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[btc_pos],
        orders=[btc_stop],
        marks=marks,
        steps=steps,
    )
    messages = render_coin_trend(conn, futures=fake, now_ms=now)
    assert len(messages) == 1
    text, markup = messages[0]
    print("TEST1_RENDERED:\n", text)
    assert markup is None
    assert "🔎 เทรนด์เหรียญ" in text
    assert "ความมั่นใจไม่ใช่โอกาสชนะ" in text
    lines = [ln for ln in text.splitlines() if ln[:1].isdigit()]
    assert len(lines) == 7
    # Confidence order: ETH 0.90 … XRP 0.65, BTC held 0.72 sits between ADA and DOGE.
    order = [ln.split()[1] for ln in lines]
    assert order == [
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "ADAUSDT",
        "BTCUSDT",
        "DOGEUSDT",
        "XRPUSDT",
    ]
    top = lines[0]
    assert "เกิน 6%" in top
    assert "ใช้อยู่ 3.0%" in top
    assert "ไม้นี้ 3.0%" in top
    btc_line = next(ln for ln in lines if "BTCUSDT" in ln)
    assert "ถืออยู่แล้ว" in btc_line
    assert conn.execute("SELECT COUNT(*) FROM assist_signal").fetchone()[0] == before_sig
    assert conn.execute("SELECT COUNT(*) FROM assist_track").fetchone()[0] == before_track
    assert _RAW_REASON_KEYS.search(text) is None
    assert _DEVANAGARI.search(text) is None
    conn.close()


def test_explain_risk_rejections_kinds() -> None:
    balance = Decimal("1000")
    # 31 + ~30 > 60 (cap 6%) so the top candidate fails risk_budget.
    existing = [ExistingIdea(symbol="BTCUSDT", side="long", risk_usdt=Decimal("31"))]
    hi = size_signal(
        symbol="ETHUSDT",
        side="short",
        entry=Decimal("100"),
        stop=Decimal("103"),
        balance=balance,
        risk_pct=Decimal("3"),
        step_size=Decimal("1"),
    )
    mid = size_signal(
        symbol="SOLUSDT",
        side="short",
        entry=Decimal("100"),
        stop=Decimal("103"),
        balance=balance,
        risk_pct=Decimal("3"),
        step_size=Decimal("1"),
    )
    # Smaller later candidate that would fit the remaining budget alone.
    lo = size_signal(
        symbol="ADAUSDT",
        side="short",
        entry=Decimal("100"),
        stop=Decimal("101"),
        balance=balance,
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert hi and mid and lo
    candidates = [
        SignalCandidate(sized=hi, confidence=0.9, symbol_rank=1),
        SignalCandidate(sized=mid, confidence=0.8, symbol_rank=2),
        SignalCandidate(sized=lo, confidence=0.7, symbol_rank=4),
    ]
    cap = portfolio_risk_cap_pct(Decimal("3"))
    _admitted, rejected = admit_signals(
        existing=existing,
        candidates=candidates,
        balance=balance,
        max_risk_pct=cap,
    )
    # 31 + ~30 > 60 → ETH/SOL risk_budget; ADA (~1%) would fit → no_backfill.
    explains = explain_risk_rejections(existing, candidates, rejected, balance, max_risk_pct=cap)
    assert explains["ETHUSDT"].kind == "risk_budget"
    assert explains["SOLUSDT"].kind == "risk_budget"
    assert explains["ADAUSDT"].kind == "no_backfill"
    assert float(explains["ETHUSDT"].cap_pct) == 6.0

    # Slot cap: two existing → any new idea is slot_cap.
    two = [
        ExistingIdea(symbol="BTCUSDT", side="long", risk_usdt=Decimal("1")),
        ExistingIdea(symbol="ETHUSDT", side="short", risk_usdt=Decimal("1")),
    ]
    _a2, rej2 = admit_signals(existing=two, candidates=candidates[:1], balance=balance, max_risk_pct=cap)
    expl2 = explain_risk_rejections(two, candidates[:1], rej2, balance, max_risk_pct=cap)
    assert expl2["ETHUSDT"].kind == "slot_cap"
    assert expl2["ETHUSDT"].max_ideas == MAX_OPEN_IDEAS

    # Balance 0 → budget 0 → risk_budget.
    _a0, rej0 = admit_signals(existing=[], candidates=candidates[:1], balance=Decimal("0"), max_risk_pct=cap)
    expl0 = explain_risk_rejections([], candidates[:1], rej0, Decimal("0"), max_risk_pct=cap)
    assert expl0["ETHUSDT"].kind == "risk_budget"


def test_scan_parity_candidates_existing(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "parity.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=2.0)
    now = _ms_bangkok(2026, 11, 17, 12, 0)
    for sym, conf, inv in (
        ("ADAUSDT", 0.80, 0.42),
        ("DOGEUSDT", 0.70, 0.21),
        ("XRPUSDT", 0.60, 2.6),
    ):
        _seed_fresh(conn, symbol=sym, action="short", confidence=conf, invalidation=inv, now=now)
    fake = FakeFutures(
        balance=Decimal("100"),
        marks={
            "ADAUSDT": Decimal("0.40"),
            "DOGEUSDT": Decimal("0.20"),
            "XRPUSDT": Decimal("2.5"),
        },
        steps={"ADAUSDT": Decimal("1"), "DOGEUSDT": Decimal("1"), "XRPUSDT": Decimal("1")},
        open_symbols={"BTCUSDT"},
        positions=[
            FuturesPosition(
                symbol="BTCUSDT",
                position_side="BOTH",
                position_amt=Decimal("0.01"),
                entry_price=Decimal("100"),
                mark_price=Decimal("100"),
                update_time_ms=now,
            )
        ],
        orders=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "stopPrice": "90",
                "reduceOnly": True,
                "positionSide": "BOTH",
            }
        ],
    )
    scan = scan_signal_candidates(conn, futures=fake, now_ms=now, persist=False, on_demand=True)
    assert "candidates" in scan and "existing" in scan
    admitted_syms = [c.sized.symbol for c in scan["admitted"]]
    rejected_syms = [c.sized.symbol for c in scan["rejected"]]
    skipped = list(scan["skipped"])
    _a, rejected = admit_signals(
        existing=scan["existing"],
        candidates=scan["candidates"],
        balance=Decimal(str(scan["balance_usdt"])),
        max_risk_pct=portfolio_risk_cap_pct(Decimal("2")),
    )
    assert [c.sized.symbol for c in rejected] == rejected_syms
    # Re-scan must be stable for admission sets.
    scan2 = scan_signal_candidates(conn, futures=fake, now_ms=now, persist=False, on_demand=True)
    assert [c.sized.symbol for c in scan2["admitted"]] == admitted_syms
    assert [c.sized.symbol for c in scan2["rejected"]] == rejected_syms
    assert [(s["symbol"], s["reason"]) for s in scan2["skipped"]] == [
        (s["symbol"], s["reason"]) for s in skipped
    ]
    conn.close()


def test_one_admitted_two_messages_parity(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "two_msg.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=1.0, enabled=True)
    now = _ms_bangkok(2026, 11, 17, 12, 0)
    # One open idea slot used → only one new admit; two more rejected.
    btc = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.01"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        update_time_ms=now,
    )
    orders = [
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "stopPrice": "99",
            "reduceOnly": True,
            "positionSide": "BOTH",
        }
    ]
    _seed_fresh(conn, symbol="BTCUSDT", action="long", confidence=0.60, invalidation=99.0, now=now)
    for sym, conf, _mark, inv, _step in (
        ("ETHUSDT", 0.90, Decimal("2000"), 1940.0, Decimal("0.001")),
        ("SOLUSDT", 0.80, Decimal("100"), 97.0, Decimal("0.01")),
        ("ADAUSDT", 0.70, Decimal("0.40"), 0.388, Decimal("1")),
    ):
        _seed_fresh(conn, symbol=sym, action="long", confidence=conf, invalidation=inv, now=now)
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[btc],
        orders=orders,
        marks={
            "BTCUSDT": Decimal("100"),
            "ETHUSDT": Decimal("2000"),
            "SOLUSDT": Decimal("100"),
            "ADAUSDT": Decimal("0.40"),
        },
        steps={
            "ETHUSDT": Decimal("0.001"),
            "SOLUSDT": Decimal("0.01"),
            "ADAUSDT": Decimal("1"),
        },
    )
    messages = render_coin_trend(conn, futures=fake, now_ms=now)
    assert len(messages) == 2
    ranking, rank_markup = messages[0]
    ideas, idea_markup = messages[1]
    print("TEST4_RANKING:\n", ranking)
    print("TEST4_IDEAS:\n", ideas)
    assert rank_markup is None
    assert "ETHUSDT" in ranking
    old_text, old_markup = render_anything_now(conn, futures=fake, now_ms=now)
    assert ideas == old_text
    assert idea_markup == old_markup
    # Buttons only for admitted symbols (ETH); never for blocked SOL/ADA.
    if idea_markup is not None:
        blob = str(idea_markup)
        assert "ETHUSDT" in blob
        assert "SOLUSDT" not in blob
        assert "ADAUSDT" not in blob
    assert _RAW_REASON_KEYS.search(ranking) is None
    assert _DEVANAGARI.search(ranking + ideas) is None
    conn.close()


def test_row_status_chase_requote_below_min(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "status_rows.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=1.0, enabled=True)
    now = _ms_bangkok(2026, 11, 17, 12, 0)
    # ADA admitted but chase → status without entry button path.
    _seed_fresh(conn, symbol="ADAUSDT", action="short", confidence=0.85, invalidation=0.42, now=now)
    sized = size_signal(
        symbol="ADAUSDT",
        side="short",
        entry=Decimal("0.40"),
        stop=Decimal("0.42"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=now - 3_600_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 3_600_000,
    )
    # Short chase: mark < entry - 0.3R.
    fake = FakeFutures(
        balance=Decimal("1000"),
        marks={"ADAUSDT": Decimal("0.39")},
        steps={"ADAUSDT": Decimal("1")},
    )
    messages = render_coin_trend(conn, futures=fake, now_ms=now)
    ranking = messages[0][0]
    assert "ราคาวิ่งไปแล้ว" in ranking or (len(messages) > 1 and "ราคาวิ่งไปแล้ว" in messages[1][0])

    # Requote cap zero on a fresh DB.
    conn2 = open_db(tmp_path / "status_requote.db")
    ensure_settings(conn2)
    write_settings(conn2, risk_pct=1.0, enabled=True)
    _seed_fresh(conn2, symbol="DOGEUSDT", action="short", confidence=0.80, invalidation=0.12, now=now)
    first = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.10"),
        stop=Decimal("0.12"),
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("1"),
    )
    assert first is not None
    insert_signal(
        conn2,
        first,
        status="sent",
        created_at_ms=now - 3_600_000,
        expires_at_ms=now + 3_600_000,
        sent_at_ms=now - 3_600_000,
    )

    def _cap_zero(sized_arg: Any, **_kwargs: Any) -> tuple[Any, str]:
        return None, "requote_cap_zero"

    monkeypatch.setattr("jarvise_assist.menu.cap_requote_qty", _cap_zero)
    fake2 = FakeFutures(
        balance=Decimal("1000"),
        marks={"DOGEUSDT": Decimal("0.10")},
        steps={"DOGEUSDT": Decimal("1")},
    )
    msgs2 = render_coin_trend(conn2, futures=fake2, now_ms=now)
    blob2 = "\n".join(m[0] for m in msgs2)
    assert "จำกัดตามไอเดียแรก" in blob2

    # below_min_notional after margin shrink: high min_notional + tiny available.
    conn3 = open_db(tmp_path / "status_min.db")
    ensure_settings(conn3)
    write_settings(conn3, risk_pct=1.0, enabled=True)
    _seed_fresh(conn3, symbol="XRPUSDT", action="short", confidence=0.78, invalidation=0.52, now=now)
    fake3 = FakeFutures(
        balance=Decimal("1000"),
        available=Decimal("0.01"),
        marks={"XRPUSDT": Decimal("0.50")},
        steps={"XRPUSDT": Decimal("1")},
        min_notional={"XRPUSDT": Decimal("100000")},
    )
    msgs3 = render_coin_trend(conn3, futures=fake3, now_ms=now)
    blob3 = "\n".join(m[0] for m in msgs3)
    # Either margin or below-min Thai label — never the raw key.
    assert "below_min_notional" not in blob3
    assert "ไม้เล็กกว่าขั้นต่ำ" in blob3 or "margin ไม่พอ" in blob3 or "ไม่มีแนวโน้ม" in blob3
    conn.close()
    conn2.close()
    conn3.close()


def test_equal_confidence_universe_order(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "eq_conf.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=1.0, enabled=True)
    now = _ms_bangkok(2026, 11, 17, 12, 0)
    # Same confidence; universe order ETH before SOL before ADA.
    for sym, inv, _mark, _step in (
        ("ADAUSDT", 0.388, Decimal("0.40"), Decimal("1")),
        ("ETHUSDT", 1940.0, Decimal("2000"), Decimal("0.001")),
        ("SOLUSDT", 97.0, Decimal("100"), Decimal("0.01")),
    ):
        _seed_fresh(conn, symbol=sym, action="long", confidence=0.80, invalidation=inv, now=now)
    fake = FakeFutures(
        balance=Decimal("1000"),
        marks={"ETHUSDT": Decimal("2000"), "SOLUSDT": Decimal("100"), "ADAUSDT": Decimal("0.40")},
        steps={"ETHUSDT": Decimal("0.001"), "SOLUSDT": Decimal("0.01"), "ADAUSDT": Decimal("1")},
    )
    ranking = render_coin_trend(conn, futures=fake, now_ms=now)[0][0]
    lines = [ln for ln in ranking.splitlines() if ln[:1].isdigit()]
    order = [ln.split()[1] for ln in lines]
    assert order == ["ETHUSDT", "SOLUSDT", "ADAUSDT"]
    conn.close()


def test_fit_telegram_and_length_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = open_db(tmp_path / "fit.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=1.0, enabled=True)
    now = _ms_bangkok(2026, 11, 17, 12, 0)
    monkeypatch.setattr("jarvise_assist.trend_view.MAX_OPEN_IDEAS", 4)
    monkeypatch.setattr(
        "jarvise_assist.trend_view.portfolio_risk_cap_pct",
        lambda _r: Decimal("8"),
    )
    real_admit = admit_signals

    def _admit_wide(
        *,
        existing: list[ExistingIdea],
        candidates: list[SignalCandidate],
        balance: Decimal,
        max_ideas: int = MAX_OPEN_IDEAS,
        max_risk_pct: Decimal,
    ) -> tuple[list[SignalCandidate], list[SignalCandidate]]:
        return real_admit(
            existing=existing,
            candidates=candidates,
            balance=balance,
            max_ideas=4,
            max_risk_pct=Decimal("8"),
        )

    monkeypatch.setattr("jarvise_assist.signals.admit_signals", _admit_wide)

    marks: dict[str, Decimal] = {}
    steps: dict[str, Decimal] = {}
    for i, sym in enumerate(ASSIST_SYMBOLS):
        # Distinct confidences; wide stops so re-quote lines stay long when admitted.
        inv = 90.0 if sym == "BTCUSDT" else (0.5 + i * 0.01)
        mark = Decimal("100") if sym == "BTCUSDT" else Decimal(str(0.4 + i * 0.01))
        _seed_fresh(conn, symbol=sym, action="long", confidence=0.90 - i * 0.01, invalidation=inv, now=now)
        # Prior sent idea → re-quote line on the idea message.
        sized = size_signal(
            symbol=sym,
            side="long",
            entry=mark,
            stop=Decimal(str(inv)),
            balance=Decimal("10000"),
            risk_pct=Decimal("1"),
            step_size=Decimal("0.001")
            if sym in {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
            else Decimal("1"),
        )
        if sized is not None:
            insert_signal(
                conn,
                sized,
                status="sent",
                created_at_ms=now - 3_600_000,
                expires_at_ms=now + 3_600_000,
                sent_at_ms=now - 3_600_000,
            )
        marks[sym] = mark
        steps[sym] = Decimal("0.001") if sym in {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"} else Decimal("1")

    fake = FakeFutures(balance=Decimal("10000"), marks=marks, steps=steps)
    messages = render_coin_trend(conn, futures=fake, now_ms=now)
    for text, _markup in messages:
        assert _utf16_len(text) <= 4096
    # Idea blocks (message 2+) must never be trimmed by _fit_telegram.
    if len(messages) >= 2:
        idea = messages[1][0]
        assert _fit_telegram(idea) == idea
    # Synthetic oversize ranking shrinks.
    fat = "🔎 เทรนด์เหรียญ · เรียงตามความมั่นใจ (สูง→ต่ำ)\nความมั่นใจไม่ใช่โอกาสชนะ · เป้า 2R ทุกไม้\n"
    fat += "\n".join(f"{i}. COIN{i} LONG · มั่นใจ 0.99 · stop ห่าง 1.0% · เสี่ยง 1.0% → x" for i in range(1, 80))
    fat += "\nไม่มีสัญญาณ: Z ไม่มีแนวโน้ม"
    trimmed = _fit_telegram(fat, limit=500)
    assert _utf16_len(trimmed) <= 500
    assert "ไม่มีสัญญาณ:" not in trimmed or _utf16_len(trimmed) <= 500
    conn.close()


def test_handle_menu_sends_each_message(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "menu_send.db")
    ensure_settings(conn)
    write_settings(conn, risk_pct=1.0, enabled=True)
    now = _ms_bangkok(2026, 11, 17, 12, 0)
    btc = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.01"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        update_time_ms=now,
    )
    _seed_fresh(conn, symbol="BTCUSDT", action="long", confidence=0.60, invalidation=99.0, now=now)
    _seed_fresh(conn, symbol="ETHUSDT", action="long", confidence=0.90, invalidation=1940.0, now=now)
    _seed_fresh(conn, symbol="SOLUSDT", action="long", confidence=0.80, invalidation=97.0, now=now)
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[btc],
        orders=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "stopPrice": "99",
                "reduceOnly": True,
                "positionSide": "BOTH",
            }
        ],
        marks={"ETHUSDT": Decimal("2000"), "SOLUSDT": Decimal("100"), "BTCUSDT": Decimal("100")},
        steps={"ETHUSDT": Decimal("0.001"), "SOLUSDT": Decimal("0.01")},
    )
    sent: list[tuple[str, Any]] = []

    def _send(t: str, **kw: Any) -> bool:
        sent.append((t, kw.get("reply_markup")))
        return True

    result = handle_menu_text(conn, BTN_ANYTHING, futures=fake, now_ms=now, send=_send)
    assert result["handled"] is True
    assert result.get("messages") == len(sent)
    if len(sent) >= 2:
        assert sent[0][1] is None
    conn.close()
