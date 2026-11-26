"""A0b: coin-first Quick picker (UI only). Fakes/injected lookups only."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_quick_card import _happy_fake, _seed_settings
from tests.test_assist_quick_m1a1_round1 import _run_qk

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.explore import connection_path, explore_db_path
from jarvise_assist.listen import process_update
from jarvise_assist.quick import build_coin_menu_keyboard, render_quick_menu
from jarvise_assist.quick_card import parse_quick_command, quick_mode_tag
from jarvise_assist.quick_config import QUICK_CONFIG, parse_value, set_quick_config
from jarvise_assist.quick_config_ui import _parse_example, handle_awaiting_quick_input
from jarvise_assist.quick_m1a1 import (
    MSG_BAD_BUTTON,
    MSG_DRAFT_EXPIRED,
    QUICK_DRAFT_TTL_MS,
    _stop_step_markup,
    _stop_step_markup_for,
    render_side_picker,
)
from jarvise_assist.quick_regime import quick_side_risk, quick_trend_4h
from jarvise_assist.quick_symbol import validate_quick_symbol
from jarvise_assist.sizing import expected_analysis_bar_ms
from jarvise_assist.store import (
    ensure_settings,
    insert_explore_symbol,
    list_explore_symbols,
    set_awaiting_quick_input,
    write_settings,
)
from jarvise_ingest.db import open_db, upsert_analysis_output

NOW = _ms_bangkok(2026, 11, 20, 12, 0)


def _raise_network(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("network")


@pytest.fixture(autouse=True)
def _forbid_quick_picker_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real Binance/kline entry from this file fails loudly."""
    targets = (
        "jarvise_ingest.providers.binance_klines.fetch_klines",
        "jarvise_assist.futures.public_exchange_symbol",
        "jarvise_assist.explore.fetch_klines",
        "jarvise_assist.listen.fetch_klines",
        "jarvise_assist.listen.public_exchange_symbol",
    )
    for path in targets:
        monkeypatch.setattr(path, _raise_network)


def _perp(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "status": "TRADING",
    }


def _seed_analysis(conn: Any, symbol: str, *, action: str, now_ms: int, fresh: bool = True) -> None:
    ts = expected_analysis_bar_ms(now_ms) if fresh else expected_analysis_bar_ms(now_ms) - 4 * 3_600_000
    upsert_analysis_output(
        conn,
        {
            "analysis_id": f"{symbol}-{ts}-{action}",
            "timestamp": ts,
            "symbol": symbol,
            "timeframe": "4h",
            "regime_state": "trend",
            "confidence_score": 0.8,
            "action": action,
            "invalidation_price": None,
            "size_pct_equity": 1.0,
            "thesis": "t",
        },
    )


def _walk_callbacks(markup: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for row in markup.get("inline_keyboard") or []:
        for btn in row:
            out.append(str(btn["callback_data"]))
    return out


@pytest.fixture
def conn(tmp_path: Any) -> Any:
    c = open_db(tmp_path / "coin.db")
    ensure_settings(c)
    _seed_settings(c)
    yield c
    c.close()


# --- regime -----------------------------------------------------------------


def test_quick_trend_core_fresh_long_short_flat_stale(conn: Any) -> None:
    assert quick_trend_4h(conn, "BTCUSDT", now_ms=NOW) == "unknown"
    _seed_analysis(conn, "BTCUSDT", action="long", now_ms=NOW)
    assert quick_trend_4h(conn, "BTCUSDT", now_ms=NOW) == "up"
    _seed_analysis(conn, "ETHUSDT", action="short", now_ms=NOW)
    assert quick_trend_4h(conn, "ETHUSDT", now_ms=NOW) == "down"
    _seed_analysis(conn, "SOLUSDT", action="flat", now_ms=NOW)
    assert quick_trend_4h(conn, "SOLUSDT", now_ms=NOW) == "unknown"
    _seed_analysis(conn, "BNBUSDT", action="long", now_ms=NOW, fresh=False)
    assert quick_trend_4h(conn, "BNBUSDT", now_ms=NOW) == "unknown"


def test_quick_trend_non_universe_unknown(conn: Any) -> None:
    assert quick_trend_4h(conn, "PEPEUSDT", now_ms=NOW) == "unknown"


def test_quick_trend_explore_fresh_long_via_injected_conn(conn: Any, tmp_path: Any) -> None:
    """Explore-coin trend reads the injected explore DB, not the main ledger."""
    insert_explore_symbol(conn, "LINKUSDT", NOW)
    explore = open_db(tmp_path / "explore-trend.db")
    try:
        _seed_analysis(explore, "LINKUSDT", action="long", now_ms=NOW)
        assert quick_trend_4h(conn, "LINKUSDT", now_ms=NOW, explore_conn=explore) == "up"
        fake = _happy_fake()
        text, _ = render_side_picker(conn, "LINKUSDT", now_ms=NOW, futures=fake, explore_conn=explore)
        assert "4h: ขาขึ้น" in text
    finally:
        explore.close()


def test_quick_side_risk_uses_live_eff(conn: Any) -> None:
    set_quick_config(conn, "quick_risk_pct", "0.75", source="t", now_ms=NOW)
    set_quick_config(conn, "quick_counter_risk_pct", "0.3", source="t", now_ms=NOW + 1)
    set_quick_config(conn, "quick_risk_cap_pct", "0.5", source="t", now_ms=NOW + 2)
    from jarvise_assist.quick_config import effective_quick_values, get_quick_config

    eff = effective_quick_values(get_quick_config(conn))
    # risk_used = min(0.75, 0.5) = 0.5; counter_used = min(0.3, 0.5) = 0.3
    is_c, risk = quick_side_risk("up", "long", eff)
    assert is_c is False
    assert risk == Decimal("0.5")
    is_c, risk = quick_side_risk("up", "short", eff)
    assert is_c is True
    assert risk == Decimal("0.3")
    is_c, risk = quick_side_risk("unknown", "long", eff)
    assert is_c is True
    assert risk == Decimal("0.3")


# --- menu grid --------------------------------------------------------------


def test_menu_core_order_explore_other_close_busy(conn: Any) -> None:
    insert_explore_symbol(conn, "LINKUSDT", NOW)
    insert_explore_symbol(conn, "1000SHIBUSDT", NOW + 1)
    from jarvise_assist.quick import create_quick_registration

    create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=1.0,
        now_ms=NOW,
    )
    text, kb = render_quick_menu(conn, now_ms=NOW)
    rows = kb["inline_keyboard"]
    # 7 core in 3+3+1
    assert [b["text"] for b in rows[0]] == ["⏳ BTC", "ETH", "SOL"]
    assert rows[0][0]["callback_data"] == "qk:bz:BTCUSDT"
    assert [b["callback_data"] for b in rows[1]] == [
        "qk:cs:BNBUSDT",
        "qk:cs:ADAUSDT",
        "qk:cs:DOGEUSDT",
    ]
    assert rows[2][0]["callback_data"] == "qk:cs:XRPUSDT"
    # explore
    explore_row = rows[3]
    assert explore_row[0]["text"] == "🧪 LINK"
    assert explore_row[0]["callback_data"] == "qk:cs:LINKUSDT"
    assert explore_row[1]["callback_data"] == "qk:cs:1000SHIBUSDT"
    assert any(b["callback_data"] == "qk:ot" for row in rows for b in row)
    assert rows[-1] == [{"text": "✖️ ปิด", "callback_data": "qk:mx"}]
    assert list(ASSIST_SYMBOLS) == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "ADAUSDT",
        "DOGEUSDT",
        "XRPUSDT",
    ]
    del text


def test_all_menu_and_side_callbacks_within_64(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    long_syms = [
        "1000000BOBUSDT",
        "1000SHIBUSDT",
        "1000PEPEUSDT",
        "1000FLOKIUSDT",
        "1000BONKUSDT",
        "1000RATSUSDT",
        "1000SATSUSDT",
        "1000WHYUSDT",
        "1000XUSDT",
        "1000CATUSDT",
    ]
    for i, sym in enumerate(long_syms):
        insert_explore_symbol(conn, sym, NOW + i)
    kb = build_coin_menu_keyboard(conn)
    for cb in _walk_callbacks(kb):
        assert len(cb.encode("utf-8")) <= 64, cb
    _, full = render_quick_menu(conn, now_ms=NOW)
    for cb in _walk_callbacks(full):
        assert len(cb.encode("utf-8")) <= 64, cb
    fake = _happy_fake()
    for sym in ("BTCUSDT", "1000000BOBUSDT", "1000SHIBUSDT"):
        if sym not in ASSIST_SYMBOLS and sym not in {r[0] for r in []}:
            pass
        text, markup = render_side_picker(conn, sym, now_ms=NOW, futures=fake)
        for cb in _walk_callbacks(markup):
            assert len(cb.encode("utf-8")) <= 64, cb
        del text


# --- side picker ------------------------------------------------------------


def test_side_picker_trend_labels_and_counter(conn: Any) -> None:
    fake = _happy_fake()
    _seed_analysis(conn, "BTCUSDT", action="long", now_ms=NOW)
    text, markup = render_side_picker(conn, "BTCUSDT", now_ms=NOW, futures=fake)
    assert "4h: ขาขึ้น" in text
    assert quick_mode_tag() in text
    assert "mark 100" in text or "mark 100.00000000" in text or "mark 100" in text.replace(".00000000", "")
    buttons = [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert any(t.startswith("🟢 LONG") and "สวนเทรนด์" not in t for t in buttons)
    assert any("🔴 SHORT" in t and "สวนเทรนด์" in t for t in buttons)
    assert "การ์ดบันทึกอย่างเดียวคิดขนาดที่ risk" in text

    _seed_analysis(conn, "ETHUSDT", action="short", now_ms=NOW)
    text2, markup2 = render_side_picker(conn, "ETHUSDT", now_ms=NOW, futures=fake)
    assert "4h: ขาลง" in text2
    buttons2 = [b["text"] for row in markup2["inline_keyboard"] for b in row]
    assert any("🟢 LONG" in t and "สวนเทรนด์" in t for t in buttons2)

    text3, markup3 = render_side_picker(conn, "SOLUSDT", now_ms=NOW, futures=fake)
    assert "4h: ไม่ทราบ" in text3
    buttons3 = [b["text"] for row in markup3["inline_keyboard"] for b in row]
    assert all("สวนเทรนด์" in t for t in buttons3 if "LONG" in t or "SHORT" in t)


def test_side_picker_counter_label_tracks_config(conn: Any) -> None:
    fake = _happy_fake()
    set_quick_config(conn, "quick_counter_risk_pct", "0.3", source="t", now_ms=NOW)
    set_quick_config(conn, "quick_risk_pct", "0.75", source="t", now_ms=NOW + 1)
    text, markup = render_side_picker(conn, "ADAUSDT", now_ms=NOW, futures=fake)
    assert "risk 0.75%" in text or "risk 0.75" in text
    joined = " ".join(b["text"] for row in markup["inline_keyboard"] for b in row)
    assert "risk 0.3%" in joined
    # Cap clamp: counter above risk_used
    set_quick_config(conn, "quick_risk_cap_pct", "0.4", source="t", now_ms=NOW + 2)
    set_quick_config(conn, "quick_counter_risk_pct", "0.35", source="t", now_ms=NOW + 3)
    # risk_used=min(0.75,0.4)=0.4; counter_used=min(0.35,0.4)=0.35
    text2, markup2 = render_side_picker(conn, "ADAUSDT", now_ms=NOW, futures=fake)
    assert "risk 0.4%" in text2
    joined2 = " ".join(b["text"] for row in markup2["inline_keyboard"] for b in row)
    assert "risk 0.35%" in joined2


def test_universe_lines(conn: Any) -> None:
    fake = _happy_fake()
    # core: no universe line (sizing clarification may still mention A1)
    text, markup = render_side_picker(conn, "BTCUSDT", now_ms=NOW, futures=fake)
    assert "🧪 เหรียญทดลอง" not in text
    assert "ได้เฉพาะ 7 เหรียญหลัก" not in text
    assert not any("qk:ea:" in b["callback_data"] for row in markup["inline_keyboard"] for b in row)

    insert_explore_symbol(conn, "LINKUSDT", NOW)
    text_e, _ = render_side_picker(conn, "LINKUSDT", now_ms=NOW, futures=fake)
    assert "🧪 เหรียญทดลอง" in text_e

    set_quick_config(conn, "quick_exec_universe", "core_explore", source="t", now_ms=NOW)
    text_o, markup_o = render_side_picker(conn, "PEPEUSDT", now_ms=NOW, futures=fake)
    assert "7 เหรียญหลัก + เหรียญทดลอง" in text_o
    assert any(b["callback_data"] == "qk:ea:PEPEUSDT" for row in markup_o["inline_keyboard"] for b in row)

    set_quick_config(conn, "quick_exec_universe", "core", source="t", now_ms=NOW + 1)
    text_c, markup_c = render_side_picker(conn, "PEPEUSDT", now_ms=NOW, futures=fake)
    assert "7 เหรียญหลัก ·" in text_c
    assert not any("qk:ea:" in b["callback_data"] for row in markup_c["inline_keyboard"] for b in row)

    set_quick_config(conn, "quick_exec_universe", "any", source="t", now_ms=NOW + 2)
    set_quick_config(conn, "quick_counter_risk_pct", "0.25", source="t", now_ms=NOW + 3)
    text_a, _ = render_side_picker(conn, "PEPEUSDT", now_ms=NOW, futures=fake)
    assert "ไม่ทราบเทรนด์" in text_a
    assert "0.25%" in text_a


def test_qk_ea_happy_and_reject(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    fake._exchange_rows["WIFUSDT"] = _perp("WIFUSDT")

    def lookup(sym: str) -> dict[str, Any] | None:
        return _perp(sym) if sym.endswith("USDT") else None

    def fetch(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        del limit
        return [
            {
                "symbol": symbol,
                "timeframe": interval,
                "timestamp": NOW - 4 * 3_600_000,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "volume": 10.0,
            }
        ]

    sent, answers, out, edits = _run_qk(
        conn,
        "qk:ea:WIFUSDT",
        fake,
        now_ms=NOW,
        explore_lookup=lookup,
        explore_fetch=fetch,
    )
    assert out.get("step") == "pick_side"
    assert list_explore_symbols(conn) == ["WIFUSDT"]
    explore = open_db(explore_db_path(connection_path(conn)))
    try:
        n = explore.execute(
            "SELECT COUNT(*) AS n FROM market_technicals WHERE symbol=?",
            ("WIFUSDT",),
        ).fetchone()["n"]
        assert int(n) > 0
    finally:
        explore.close()
    assert any("WIF" in (s.get("text") or "") or "ทดลอง" in (s.get("text") or "") for s in sent)
    assert any("WIF" in (e.get("text") or "") for e in edits) or out.get("step") == "pick_side"

    sent2, _, out2, _ = _run_qk(
        conn,
        "qk:ea:BTCUSDT",
        fake,
        now_ms=NOW,
        explore_lookup=lookup,
        explore_fetch=fetch,
    )
    assert out2.get("ok") is False or out2.get("explore", {}).get("ok") is False
    del answers, sent2


# --- 5-part sd --------------------------------------------------------------


def test_qk_sd_five_part_creates_pick_stop(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    issued = NOW // 1000
    sent, answers, out, edits = _run_qk(conn, f"qk:sd:l:BTCUSDT:{issued}", fake, now_ms=NOW)
    assert out.get("step") == "pick_stop"
    row = conn.execute("SELECT * FROM assist_quick_card WHERE id=?", (out["card_id"],)).fetchone()
    assert row["status"] == "pick_stop"
    assert row["symbol"] == "BTCUSDT"
    assert row["side"] == "long"
    assert "STOP" in edits[0]["text"] or "stop" in edits[0]["text"].lower() or "Quick" in edits[0]["text"]
    del sent, answers


def test_qk_sd_five_part_stale_future_bad_busy(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    issued = NOW // 1000
    _, answers, out, _ = _run_qk(
        conn, f"qk:sd:l:BTCUSDT:{issued - (QUICK_DRAFT_TTL_MS // 1000) - 1}", fake, now_ms=NOW
    )
    assert out.get("expired") is True
    assert answers[-1] == MSG_DRAFT_EXPIRED

    _, answers2, out2, _ = _run_qk(conn, f"qk:sd:l:BTCUSDT:{issued + 120}", fake, now_ms=NOW)
    assert out2.get("ok") is False
    assert answers2[-1] == MSG_BAD_BUTTON

    _, answers3, out3, _ = _run_qk(conn, "qk:sd:l:BTCUSDT:nope", fake, now_ms=NOW)
    assert out3.get("ok") is False
    assert answers3[-1] == MSG_BAD_BUTTON

    from jarvise_assist.quick import create_quick_registration

    create_quick_registration(
        conn,
        symbol="ETHUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=1.0,
        now_ms=NOW,
    )
    _, answers4, out4, _ = _run_qk(conn, f"qk:sd:s:ETHUSDT:{issued}", fake, now_ms=NOW)
    assert out4.get("busy") is True
    assert "ETH" in (answers4[-1] or "")


def test_qk_sd_six_part_junk_no_state_change(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """qk:sd with >5 parts must reject without cancelling drafts or clearing awaits."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    issued = NOW // 1000

    # Seed a draft that must survive, plus a cfg await.
    _, _, draft_out, _ = _run_qk(conn, "qk:sd:l", fake, now_ms=NOW)
    draft_id = draft_out["card_id"]
    set_awaiting_quick_input(conn, "cfg:quick_risk_pct", until_ms=NOW + 300_000)

    _, answers, out, _ = _run_qk(conn, f"qk:sd:l:BTCUSDT:{issued}:junk", fake, now_ms=NOW)
    assert out.get("ok") is False
    assert answers[-1] == MSG_BAD_BUTTON
    assert ensure_settings(conn)["awaiting_quick_input"] == "cfg:quick_risk_pct"
    row = conn.execute("SELECT status FROM assist_quick_card WHERE id=?", (draft_id,)).fetchone()
    assert row["status"] == "pick_symbol"
    n = conn.execute("SELECT COUNT(*) AS n FROM assist_quick_card WHERE status='pick_stop'").fetchone()["n"]
    assert int(n) == 0


def test_qk_sd_five_part_clears_other_awaits(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid 5-part qk:sd clears explore / risk / symbol awaits (mutually exclusive seeds)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    issued = NOW // 1000

    write_settings(conn, awaiting_explore=True)
    _, _, out1, _ = _run_qk(conn, f"qk:sd:l:BTCUSDT:{issued}", fake, now_ms=NOW)
    assert out1.get("step") == "pick_stop"
    assert ensure_settings(conn)["awaiting_explore"] is False

    write_settings(conn, awaiting_risk="confirm", pending_risk_pct=1.0)
    _, _, out2, _ = _run_qk(conn, f"qk:sd:s:ETHUSDT:{issued}", fake, now_ms=NOW)
    assert out2.get("step") == "pick_stop"
    assert ensure_settings(conn)["awaiting_risk"] is None

    set_awaiting_quick_input(conn, "symbol", until_ms=NOW + 300_000)
    _, _, out3, _ = _run_qk(conn, f"qk:sd:l:SOLUSDT:{issued}", fake, now_ms=NOW)
    assert out3.get("step") == "pick_stop"
    assert ensure_settings(conn)["awaiting_quick_input"] is None


def test_side_picker_has_back_and_cancel_row(conn: Any) -> None:
    fake = _happy_fake()
    _, markup = render_side_picker(conn, "BTCUSDT", now_ms=NOW, futures=fake)
    flat = [(b["text"], b["callback_data"]) for row in markup["inline_keyboard"] for b in row]
    assert ("↩️ เลือกเหรียญใหม่", "qk:bk") in flat
    assert ("✖️ ยกเลิก", "qk:cx") in flat


def test_qk_sd_five_part_stop_step_uses_configured_presets(
    conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    set_quick_config(conn, "quick_stop_presets", "0.2,0.4", source="t", now_ms=NOW)
    issued = NOW // 1000
    _, _, out, edits = _run_qk(conn, f"qk:sd:l:BTCUSDT:{issued}", fake, now_ms=NOW)
    assert out.get("step") == "pick_stop"
    assert edits
    texts = [b["text"] for b in edits[-1]["reply_markup"]["inline_keyboard"][0]]
    assert texts == ["0.2%", "0.4%"]


# --- parity -----------------------------------------------------------------


def test_parity_typed_legacy_coin_first(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    issued = NOW // 1000

    def _fresh() -> Any:
        c = open_db(tmp_path / f"p-{__import__('uuid').uuid4().hex[:8]}.db")
        ensure_settings(c)
        _seed_settings(c)
        return c

    # (a) typed
    conn_a = _fresh()
    sent_a: list[dict[str, Any]] = []

    def send_a(t: str, **kw: Any) -> int:
        sent_a.append({"text": t, **kw})
        return 10

    process_update(
        conn_a,
        {"message": {"chat": {"id": 1}, "text": "q BTCUSDT long 0.5%"}},
        send=send_a,
        futures=fake,
        now_ms_value=NOW,
    )
    assert sent_a
    text_a = sent_a[0]["text"]
    mk_a = sent_a[0]["reply_markup"]
    conn_a.close()

    # (b) legacy stepper
    conn_b = _fresh()
    _run_qk(conn_b, "qk:sd:l", fake, now_ms=NOW)
    cid = str(conn_b.execute("SELECT id FROM assist_quick_card WHERE status='pick_symbol'").fetchone()["id"])
    _run_qk(conn_b, f"qk:sy:{cid}:BTCUSDT", fake, now_ms=NOW)
    sent_b, _, out_b, edits_b = _run_qk(conn_b, f"qk:sl:{cid}:0.5", fake, now_ms=NOW)
    text_b = edits_b[-1]["text"] if edits_b else sent_b[-1]["text"]
    mk_b = edits_b[-1].get("reply_markup") or sent_b[-1].get("reply_markup")
    conn_b.close()

    # (c) coin-first
    conn_c = _fresh()
    _run_qk(conn_c, f"qk:sd:l:BTCUSDT:{issued}", fake, now_ms=NOW)
    cid_c = str(conn_c.execute("SELECT id FROM assist_quick_card WHERE status='pick_stop'").fetchone()["id"])
    sent_c, _, out_c, edits_c = _run_qk(conn_c, f"qk:sl:{cid_c}:0.5", fake, now_ms=NOW)
    text_c = edits_c[-1]["text"] if edits_c else sent_c[-1]["text"]
    mk_c = edits_c[-1].get("reply_markup") or sent_c[-1].get("reply_markup")
    conn_c.close()

    assert text_a == text_b == text_c

    def _norm_markup(mk: dict[str, Any]) -> dict[str, Any]:
        """Card ids are random; compare button shape with ids blanked."""
        import re

        rows = []
        for row in mk["inline_keyboard"]:
            rows.append(
                [
                    {
                        "text": b["text"],
                        "callback_data": re.sub(
                            r"(qk:(?:rg|rf|x):)[0-9a-f]+",
                            r"\1CID",
                            b["callback_data"],
                        ),
                    }
                    for b in row
                ]
            )
        return {"inline_keyboard": rows}

    assert _norm_markup(mk_a) == _norm_markup(mk_b) == _norm_markup(mk_c)
    assert out_b.get("quoted") or out_b.get("ok")
    assert out_c.get("quoted") or out_c.get("ok")


# --- stop presets -----------------------------------------------------------


def test_stop_presets_default_and_custom(conn: Any) -> None:
    default_mk = _stop_step_markup("abcd1234")
    assert default_mk == _stop_step_markup_for(conn, "abcd1234")
    assert [b["text"] for b in default_mk["inline_keyboard"][0]] == ["0.3%", "0.5%", "1%"]
    assert [b["callback_data"] for b in default_mk["inline_keyboard"][0]] == [
        "qk:sl:abcd1234:0.3",
        "qk:sl:abcd1234:0.5",
        "qk:sl:abcd1234:1",
    ]
    set_quick_config(conn, "quick_stop_presets", "0.2,0.4,0.8,1.5", source="t", now_ms=NOW)
    custom = _stop_step_markup_for(conn, "abcd1234")
    assert [b["text"] for b in custom["inline_keyboard"][0]] == ["0.2%", "0.4%", "0.8%", "1.5%"]


def test_removed_preset_rejected_stale(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    issued = NOW // 1000
    _run_qk(conn, f"qk:sd:l:BTCUSDT:{issued}", fake, now_ms=NOW)
    cid = str(conn.execute("SELECT id FROM assist_quick_card WHERE status='pick_stop'").fetchone()["id"])
    set_quick_config(conn, "quick_stop_presets", "0.3,0.5", source="t", now_ms=NOW)
    _, answers, out, _ = _run_qk(conn, f"qk:sl:{cid}:1", fake, now_ms=NOW)
    assert out.get("stale") is True
    assert answers[-1] == MSG_BAD_BUTTON


# --- typed symbol -----------------------------------------------------------


def test_validate_quick_symbol_paths() -> None:
    def lookup(sym: str) -> dict[str, Any] | None:
        if sym == "WIFUSDT":
            return _perp(sym)
        if sym == "1000PEPEUSDT":
            return _perp(sym)
        if sym == "SPOTUSDT":
            return {
                "symbol": sym,
                "contractType": "CURRENT_QUARTER",
                "quoteAsset": "USDT",
                "status": "TRADING",
            }
        return None

    assert validate_quick_symbol("wif", lookup=lookup).outcome == "ok"
    assert validate_quick_symbol("WIFUSDT", lookup=lookup).symbol == "WIFUSDT"
    hint = validate_quick_symbol("PEPE", lookup=lookup)
    assert hint.outcome == "hint_1000"
    assert hint.hint_symbol == "1000PEPEUSDT"
    bad = validate_quick_symbol("!!!", lookup=lookup)
    assert bad.outcome == "format"
    nf = validate_quick_symbol("ZZZUSDT", lookup=lookup)
    assert nf.outcome == "not_found"
    cr = validate_quick_symbol("SPOTUSDT", lookup=lookup)
    assert cr.outcome == "contract"

    def boom(_sym: str) -> dict[str, Any] | None:
        raise RuntimeError("net")

    assert validate_quick_symbol("WIF", lookup=boom).outcome == "lookup_error"

    def first_none_fallback_raises(sym: str) -> dict[str, Any] | None:
        if sym == "PEPEUSDT":
            return None
        if sym == "1000PEPEUSDT":
            raise RuntimeError("net-fallback")
        return None

    fallback_err = validate_quick_symbol("PEPE", lookup=first_none_fallback_raises)
    assert fallback_err.outcome == "lookup_error"
    assert fallback_err.message == "อ่าน Binance ไม่ได้ — ลองพิมพ์ใหม่"


def test_awaiting_symbol_flow(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    fake._exchange_rows["WIFUSDT"] = _perp("WIFUSDT")
    fake._exchange_rows["1000PEPEUSDT"] = _perp("1000PEPEUSDT")

    def lookup(sym: str) -> dict[str, Any] | None:
        return fake.exchange_symbol(sym)

    set_awaiting_quick_input(conn, "symbol", until_ms=NOW + 300_000)
    sent: list[dict[str, Any]] = []
    out = handle_awaiting_quick_input(
        conn,
        "wif",
        send=lambda t, **kw: sent.append({"text": t, **kw}),
        now_ms=NOW,
        lookup=lookup,
        futures=fake,
    )
    assert out is not None and out.get("step") == "pick_side"
    assert ensure_settings(conn)["awaiting_quick_input"] is None

    set_awaiting_quick_input(conn, "symbol", until_ms=NOW + 300_000)
    out2 = handle_awaiting_quick_input(
        conn,
        "PEPE",
        send=lambda t, **kw: sent.append({"text": t, **kw}),
        now_ms=NOW,
        lookup=lookup,
        futures=fake,
    )
    assert out2 is not None and out2.get("hint") == "1000PEPEUSDT"
    assert ensure_settings(conn)["awaiting_quick_input"] == "symbol"

    set_awaiting_quick_input(conn, "symbol", until_ms=NOW + 300_000)
    out3 = handle_awaiting_quick_input(
        conn,
        "NOPEUSDT",
        send=lambda t, **kw: sent.append({"text": t, **kw}),
        now_ms=NOW,
        lookup=lookup,
        futures=fake,
    )
    assert out3 is not None and out3.get("outcome") == "not_found"
    assert ensure_settings(conn)["awaiting_quick_input"] == "symbol"


def test_awaiting_symbol_ttl_and_clears(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    set_awaiting_quick_input(conn, "symbol", until_ms=NOW - 1)
    out = handle_awaiting_quick_input(
        conn, "wif", send=lambda *_a, **_k: None, now_ms=NOW, lookup=lambda s: _perp(s), futures=fake
    )
    assert out is None
    assert ensure_settings(conn)["awaiting_quick_input"] is None

    set_awaiting_quick_input(conn, "symbol", until_ms=NOW + 300_000)
    process_update(
        conn,
        {"message": {"chat": {"id": 1}, "text": "⚡ Quick"}},
        send=lambda *_a, **_k: 1,
        futures=fake,
        now_ms_value=NOW,
    )
    # menu label clears awaiting
    assert ensure_settings(conn)["awaiting_quick_input"] is None

    set_awaiting_quick_input(conn, "symbol", until_ms=NOW + 300_000)
    fake._exchange_rows.setdefault("BTCUSDT", _perp("BTCUSDT"))
    process_update(
        conn,
        {"message": {"chat": {"id": 1}, "text": "q BTCUSDT long 0.5%"}},
        send=lambda *_a, **_k: 1,
        futures=fake,
        now_ms_value=NOW,
    )
    assert ensure_settings(conn)["awaiting_quick_input"] is None


def test_cfg_await_path_unchanged(conn: Any) -> None:
    set_awaiting_quick_input(conn, "cfg:quick_risk_pct", until_ms=NOW + 300_000)
    sent: list[str] = []
    out = handle_awaiting_quick_input(conn, "0.6", send=lambda t, **_k: sent.append(t), now_ms=NOW)
    assert out is not None
    assert ensure_settings(conn)["awaiting_quick_input"] is None


# --- bk / cx / mx / bz ------------------------------------------------------


def test_bk_cx_mx_bz(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    from jarvise_assist.quick import create_quick_registration

    create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=1.0,
        now_ms=NOW,
    )
    _, answers, out, _ = _run_qk(conn, "qk:bz:BTCUSDT", fake, now_ms=NOW)
    assert out.get("busy")
    assert "BTC" in (answers[-1] or "")

    _, _, out_cs, _ = _run_qk(conn, "qk:cs:BTCUSDT", fake, now_ms=NOW)
    assert out_cs.get("busy")

    set_awaiting_quick_input(conn, "symbol", until_ms=NOW + 300_000)
    sent, _, out_cx, edits = _run_qk(conn, "qk:cx", fake, now_ms=NOW)
    assert out_cx.get("cancelled")
    assert edits[-1]["text"] == "ยกเลิกแล้ว"
    assert ensure_settings(conn)["awaiting_quick_input"] is None

    _, _, out_mx, edits_mx = _run_qk(conn, "qk:mx", fake, now_ms=NOW)
    assert out_mx.get("closed")
    assert edits_mx[-1]["reply_markup"] == {"inline_keyboard": []}

    _, _, out_bk, edits_bk = _run_qk(conn, "qk:bk", fake, now_ms=NOW)
    assert out_bk.get("step") == "coin_menu"
    assert any(
        b["callback_data"] == "qk:cs:ETHUSDT"
        for row in edits_bk[-1]["reply_markup"]["inline_keyboard"]
        for b in row
    )
    del sent


def test_qk_cx_bk_cs_preserve_cfg_await(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale side-picker Cancel/Back/coin must not wipe a settings cfg: await."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()

    set_awaiting_quick_input(conn, "cfg:quick_risk_pct", until_ms=NOW + 300_000)
    _run_qk(conn, "qk:cx", fake, now_ms=NOW)
    assert ensure_settings(conn)["awaiting_quick_input"] == "cfg:quick_risk_pct"

    set_awaiting_quick_input(conn, "cfg:quick_risk_pct", until_ms=NOW + 300_000)
    _run_qk(conn, "qk:bk", fake, now_ms=NOW)
    assert ensure_settings(conn)["awaiting_quick_input"] == "cfg:quick_risk_pct"

    set_awaiting_quick_input(conn, "cfg:quick_risk_pct", until_ms=NOW + 300_000)
    _run_qk(conn, "qk:cs:BTCUSDT", fake, now_ms=NOW)
    assert ensure_settings(conn)["awaiting_quick_input"] == "cfg:quick_risk_pct"


def test_qk_mx_no_send_fallback_on_edit_fail(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    sent, answers, out, edits = _run_qk(
        conn,
        "qk:mx",
        fake,
        now_ms=NOW,
        edit_returns=False,
        message_text="⚡ Quick · menu",
    )
    assert out.get("closed") is True
    assert sent == []
    assert answers[-1] is None
    assert edits  # edit was attempted
    assert edits[-1]["reply_markup"] == {"inline_keyboard": []}


def test_typed_symbol_lookup_error_via_process_update(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod Quick lookup must surface lookup_error, not 'coin not found'."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)

    class _BoomFutures:
        def exchange_symbol(self, symbol: str) -> dict[str, Any] | None:
            raise RuntimeError(f"boom:{symbol}")

        def mark_price(self, symbol: str) -> Decimal:
            del symbol
            return Decimal("1")

    set_awaiting_quick_input(conn, "symbol", until_ms=NOW + 300_000)
    sent: list[str] = []
    out = process_update(
        conn,
        {"message": {"chat": {"id": 1}, "text": "WIF"}},
        send=lambda t, **_k: sent.append(t),
        futures=_BoomFutures(),
        now_ms_value=NOW,
    )
    assert out is not None
    assert out.get("awaiting") == "symbol"
    assert out.get("outcome") == "lookup_error"
    assert sent[-1] == "อ่าน Binance ไม่ได้ — ลองพิมพ์ใหม่"
    assert ensure_settings(conn)["awaiting_quick_input"] == "symbol"


def test_qk_ot_sets_await(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    _, _, out, edits = _run_qk(conn, "qk:ot", fake, now_ms=NOW)
    assert out.get("awaiting") == "symbol"
    assert ensure_settings(conn)["awaiting_quick_input"] == "symbol"
    assert "PEPE" in edits[-1]["text"]


def test_parse_example_round_trips_every_registry_entry() -> None:
    for entry in QUICK_CONFIG:
        example = _parse_example(entry)
        parsed, err = parse_value(entry.key, example)
        assert err is None, (entry.key, example, err)
        assert parsed is not None


def test_legacy_three_part_sd_still_works(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    _forbid_trade_client(monkeypatch)
    fake = _happy_fake()
    _, _, out, _ = _run_qk(conn, "qk:sd:l", fake, now_ms=NOW)
    assert out.get("step") == "pick_symbol"
    assert parse_quick_command("q BTC long 0.5%") is not None
