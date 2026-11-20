"""Quick P1a — min-lot / fit-margin offers and same-side cap."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_execute import _forbid_trade_client
from tests.test_assist_quick_card import (
    QuickFake,
    _happy_fake,
    _seed_settings,
)
from tests.test_assist_quick_guards import _insert_quick_trade
from tests.test_assist_quick_m1a1_round1 import NOW, _run_qk

from jarvise_assist.quick import write_quick_settings
from jarvise_assist.quick_cap import same_side_cap_reached, same_side_cap_text
from jarvise_assist.quick_card import build_quick_card
from jarvise_assist.quick_m1a1 import (
    MSG_SIZE_CHANGED,
    _quoted_markup_for,
    create_card_from_typed_command,
)
from jarvise_assist.quick_minlot import SizeOffer, size_offer
from jarvise_ingest.db import open_db

BTC_MARK = Decimal("62100")
BTC_STOP_SHORT = Decimal("62300")
BTC_WALLET = Decimal("120")
BTC_AVAILABLE = Decimal("43.32")
BTC_STEP = Decimal("0.001")
BTC_MIN_QTY = Decimal("0.001")
BTC_MIN_NOTIONAL_100 = Decimal("100")
BTC_MIN_NOTIONAL_5 = Decimal("5")
BTC_RISK_QTY = Decimal("0.006")


def _offer_btc_short(**kw: Any) -> tuple[SizeOffer | None, str | None]:
    defaults = dict(
        mark=BTC_MARK,
        stop=BTC_STOP_SHORT,
        wallet_balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        min_qty=BTC_MIN_QTY,
        min_notional=BTC_MIN_NOTIONAL_100,
        step=BTC_STEP,
        risk_qty=BTC_RISK_QTY,
    )
    defaults.update(kw)
    return size_offer(**defaults)


def test_size_offer_fit_margin_btc_like() -> None:
    offer, reason = _offer_btc_short()
    assert reason is None
    assert offer is not None
    assert offer.kind == "fit_margin"
    assert offer.qty == Decimal("0.003")
    assert offer.leverage == 5
    assert offer.margin == pytest.approx(Decimal("37.26"), rel=1e-3)
    assert offer.real_risk_pct == pytest.approx(Decimal("0.69"), abs=Decimal("0.02"))


def test_size_offer_min_notional_dominates() -> None:
    offer, reason = _offer_btc_short(min_notional=BTC_MIN_NOTIONAL_100, risk_qty=Decimal("0"))
    assert offer is not None and offer.kind == "min_lot"
    assert offer.qty >= Decimal("0.002")


def test_size_offer_min_qty_dominates() -> None:
    offer, reason = size_offer(
        mark=Decimal("100"),
        stop=Decimal("101"),
        wallet_balance=Decimal("1000"),
        available=Decimal("50"),
        min_qty=Decimal("0.5"),
        min_notional=Decimal("5"),
        step=Decimal("0.1"),
        risk_qty=Decimal("0"),
    )
    assert offer is not None
    assert offer.qty == Decimal("0.5")


def test_size_offer_risk_at_ceiling() -> None:
    wallet = Decimal("100")
    mark = Decimal("100")
    stop = Decimal("99")
    step = Decimal("0.01")
    min_qty = Decimal("0.01")
    min_notional = Decimal("5")
    offer, _ = size_offer(
        mark=mark,
        stop=stop,
        wallet_balance=wallet,
        available=Decimal("500"),
        min_qty=min_qty,
        min_notional=min_notional,
        step=step,
        risk_qty=Decimal("0"),
        max_real_risk_pct=Decimal("1"),
    )
    assert offer is not None
    assert offer.real_risk_pct <= Decimal("1")


def test_size_offer_risk_over_1pct() -> None:
    offer, reason = size_offer(
        mark=Decimal("100"),
        stop=Decimal("98"),
        wallet_balance=Decimal("100"),
        available=Decimal("500"),
        min_qty=Decimal("1"),
        min_notional=Decimal("5"),
        step=Decimal("1"),
        risk_qty=Decimal("0"),
        max_real_risk_pct=Decimal("1"),
    )
    assert offer is None
    assert reason == "risk_over_1pct"


def test_size_offer_leverage_exactly_5() -> None:
    offer, reason = _offer_btc_short()
    assert offer is not None and offer.leverage == 5


def test_size_offer_guards() -> None:
    assert (
        size_offer(
            mark=Decimal("0"),
            stop=Decimal("1"),
            wallet_balance=Decimal("1"),
            available=Decimal("1"),
            min_qty=Decimal("1"),
            min_notional=Decimal("1"),
            step=Decimal("1"),
            risk_qty=Decimal("0"),
        )[1]
        == "bad_input"
    )
    assert (
        size_offer(
            mark=Decimal("1"),
            stop=Decimal("2"),
            wallet_balance=Decimal("0"),
            available=Decimal("1"),
            min_qty=Decimal("1"),
            min_notional=Decimal("1"),
            step=Decimal("1"),
            risk_qty=Decimal("0"),
        )[1]
        == "no_balance"
    )
    assert (
        size_offer(
            mark=Decimal("1"),
            stop=Decimal("2"),
            wallet_balance=Decimal("100"),
            available=Decimal("0"),
            min_qty=Decimal("1"),
            min_notional=Decimal("1"),
            step=Decimal("1"),
            risk_qty=Decimal("0"),
        )[1]
        == "leverage_over_5x"
    )


def test_card_fit_margin_offer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "fm.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = QuickFake(
        balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        marks={"BTCUSDT": BTC_MARK},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": BTC_STEP},
        min_qty={"BTCUSDT": BTC_MIN_QTY},
        min_notional={"BTCUSDT": BTC_MIN_NOTIONAL_100},
    )
    card = build_quick_card(
        conn,
        fake,
        symbol="BTCUSDT",
        side="short",
        stop=BTC_STOP_SHORT,
        now_ms=NOW,
        allow_offer=True,
    )
    assert card["ok"] is True
    assert card["size_mode"] == "fit_margin"
    assert card["qty"] == Decimal("0.003")
    assert "ตั้งใน Binance เอง" in card["text"]
    conn.close()


def test_card_min_lot_offer_pins_risk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "ml.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
    fake = _happy_fake(
        balance=Decimal("100"),
        marks={"SUIUSDT": Decimal("2.0")},
        min_notional={"SUIUSDT": Decimal("5")},
        steps={"SUIUSDT": Decimal("0.1")},
        min_qty={"SUIUSDT": Decimal("10.1")},
    )
    card = build_quick_card(
        conn,
        fake,
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.95"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert card["ok"] is True
    assert card["size_mode"] == "min_lot"
    assert "ตั้งใน Binance เอง" in card["text"]
    conn.close()


def test_card_1pct_refuses_min_lot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "r1.db")
    _seed_settings(conn)
    fake = QuickFake(
        balance=Decimal("100"),
        available=Decimal("500"),
        marks={"BTCUSDT": Decimal("50000")},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": Decimal("0.001")},
        min_qty={"BTCUSDT": Decimal("0.001")},
        min_notional={"BTCUSDT": Decimal("100")},
    )
    card = build_quick_card(
        conn,
        fake,
        symbol="BTCUSDT",
        side="long",
        stop=Decimal("10000"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert card["ok"] is False
    assert card["reason"] == "risk_over_1pct"
    conn.close()


def test_same_side_cap_helpers() -> None:
    assert same_side_cap_reached(2)
    assert not same_side_cap_reached(1)
    assert "2" in same_side_cap_text("long", 2)


def test_same_side_cap_qk_rg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "cap.db")
    _seed_settings(conn)
    write_quick_settings(conn, cap_pct=10.0, risk_pct=0.5)
    _insert_quick_trade(conn, symbol="ETHUSDT", side="long", now_ms=NOW)
    _insert_quick_trade(conn, symbol="SOLUSDT", side="long", now_ms=NOW)
    fake = _happy_fake()
    parsed = {"symbol": "SUIUSDT", "side": "long", "stop": Decimal("1.994")}
    cid, card = create_card_from_typed_command(conn, fake, parsed, now_ms=NOW)
    assert cid and card["ok"]
    sent, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("reason") == "same_side_cap"
    assert len(sent) == 1
    assert "LONG" in sent[0]["text"]
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert row["registration_id"] is None
    conn.close()


def test_typed_offer_register_qty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "reg.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = QuickFake(
        balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        marks={"BTCUSDT": BTC_MARK},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": BTC_STEP},
        min_qty={"BTCUSDT": BTC_MIN_QTY},
        min_notional={"BTCUSDT": BTC_MIN_NOTIONAL_100},
    )
    parsed = {"symbol": "BTCUSDT", "side": "short", "stop": BTC_STOP_SHORT}
    cid, card = create_card_from_typed_command(conn, fake, parsed, now_ms=NOW)
    assert card["size_mode"] == "fit_margin"
    markup = _quoted_markup_for(cid, card["size_mode"])
    assert markup["inline_keyboard"][0][0]["text"] == "⚡ ยืนยันขนาดที่เสนอ"
    sent, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("ok") is True
    reg = conn.execute(
        """
        SELECT r.qty, c.size_mode FROM assist_quick_registration r
        JOIN assist_quick_card c ON c.registration_id = r.id WHERE c.id=?
        """,
        (cid,),
    ).fetchone()
    assert Decimal(str(reg["qty"])) == card["qty"]
    row = conn.execute("SELECT size_mode FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["size_mode"] == "fit_margin"
    conn.close()


def test_qk_rg_size_changed_on_mode_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "drift.db")
    _seed_settings(conn)
    fake_full = _happy_fake()
    parsed = {"symbol": "SUIUSDT", "side": "long", "stop": Decimal("1.994")}
    cid, card = create_card_from_typed_command(conn, fake_full, parsed, now_ms=NOW)
    assert card.get("size_mode", "risk") == "risk"
    conn.execute(
        "UPDATE assist_quick_card SET size_mode='fit_margin', quote_qty=? WHERE id=?",
        (float(card["qty"]) * 2, cid),
    )
    conn.commit()
    sent, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake_full)
    assert sent[0]["text"] == MSG_SIZE_CHANGED
    assert out.get("reason") == "size_changed"
    conn.close()


def test_callback_bytes_offer_paths() -> None:
    cid = "abcd1234"
    for mode in ("risk", "min_lot", "fit_margin"):
        m = _quoted_markup_for(cid, mode)
        for row in m["inline_keyboard"]:
            for btn in row:
                assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_quick_modules_ast_safety() -> None:
    banned = {"jarvise_trade", "redis", "execute"}
    for mod_name in ("jarvise_assist.quick_minlot", "jarvise_assist.quick_cap"):
        mod = __import__(mod_name, fromlist=["x"])
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in banned
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in banned


def test_legacy_qr_still_refuses_margin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "qr.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
    card = build_quick_card(
        conn,
        _happy_fake(available=Decimal("0.01")),
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.994"),
        now_ms=NOW,
        allow_offer=False,
    )
    assert card["reason"] == "margin_cap"
    conn.close()
