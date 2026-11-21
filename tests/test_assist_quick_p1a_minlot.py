"""Quick P1a — min-lot / fit-margin offers, fmt_pct, and same-side cap."""

from __future__ import annotations

import ast
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
    _registration_refusal_text,
    create_card_from_typed_command,
)
from jarvise_assist.quick_minlot import (
    SizeOfferResult,
    fmt_pct,
    min_lot_stop_max,
    size_offer,
)
from jarvise_ingest.db import open_db

SRC = Path(__file__).resolve().parents[1] / "src" / "jarvise_assist"
_QUICK_FORBIDDEN_IMPORTS = (
    "jarvise_assist.execute",
    "jarvise_trade",
    "redis",
    "jarvise_notify.quiet",
    "jarvise_assist.futures",
    "jarvise_assist.messages",
)

BTC_MARK = Decimal("62100")
BTC_STOP_SHORT = Decimal("62300")
BTC_WALLET = Decimal("120")
BTC_AVAILABLE = Decimal("43.32")
BTC_STEP = Decimal("0.001")
BTC_MIN_QTY = Decimal("0.001")
BTC_MIN_NOTIONAL = Decimal("100")
BTC_RISK_QTY = Decimal("0.006")


def _btc_short_offer(**kw: Any) -> SizeOfferResult:
    defaults: dict[str, Any] = dict(
        mark=BTC_MARK,
        stop=BTC_STOP_SHORT,
        wallet_balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        min_qty=BTC_MIN_QTY,
        min_notional=BTC_MIN_NOTIONAL,
        step=BTC_STEP,
        risk_qty=BTC_RISK_QTY,
    )
    defaults.update(kw)
    return size_offer(**defaults)


def _assert_module_forbids(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(
                    alias.name == b or alias.name.startswith(b + ".") for b in _QUICK_FORBIDDEN_IMPORTS
                ), alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not any(mod == b or mod.startswith(b + ".") for b in _QUICK_FORBIDDEN_IMPORTS), mod


@pytest.mark.parametrize(
    ("case_id", "kwargs", "expect"),
    [
        pytest.param(
            "today_btc_margin",
            dict(
                mark=BTC_MARK,
                stop=BTC_STOP_SHORT,
                wallet_balance=BTC_WALLET,
                available=BTC_AVAILABLE,
                min_qty=BTC_MIN_QTY,
                min_notional=BTC_MIN_NOTIONAL,
                step=BTC_STEP,
                risk_qty=BTC_RISK_QTY,
            ),
            dict(
                reason=None,
                kind="fit_margin",
                bound="margin",
                qty=Decimal("0.003"),
                leverage=5,
                margin=Decimal("37.260"),
                real_risk_pct=Decimal("0.6863000"),
            ),
            id="today_btc_margin",
        ),
        pytest.param(
            "min_notional_dom",
            dict(
                mark=BTC_MARK,
                stop=BTC_STOP_SHORT,
                wallet_balance=BTC_WALLET,
                available=BTC_AVAILABLE,
                min_qty=BTC_MIN_QTY,
                min_notional=BTC_MIN_NOTIONAL,
                step=BTC_STEP,
                risk_qty=Decimal("0"),
            ),
            dict(
                reason=None,
                kind="min_lot",
                bound="min_lot",
                qty=Decimal("0.002"),
                leverage=3,
                margin=Decimal("41.400"),
                real_risk_pct=Decimal("0.4575333333333333333333333333"),
            ),
            id="min_notional_dom",
        ),
        pytest.param(
            "risk_1pct_bound",
            dict(
                mark=BTC_MARK,
                stop=BTC_STOP_SHORT,
                wallet_balance=BTC_WALLET,
                available=Decimal("200"),
                min_qty=BTC_MIN_QTY,
                min_notional=BTC_MIN_NOTIONAL,
                step=BTC_STEP,
                risk_qty=Decimal("0.1"),
            ),
            dict(
                reason=None,
                kind="fit_margin",
                bound="risk_1pct",
                qty=Decimal("0.004"),
                leverage=2,
                margin=Decimal("124.200"),
                real_risk_pct=Decimal("0.9150666666666666666666666667"),
            ),
            id="risk_1pct_bound",
        ),
        pytest.param(
            "risk_bound",
            dict(
                mark=Decimal("100"),
                stop=Decimal("95"),
                wallet_balance=Decimal("1000"),
                available=Decimal("500"),
                min_qty=Decimal("0.01"),
                min_notional=Decimal("5"),
                step=Decimal("0.01"),
                risk_qty=Decimal("1"),
            ),
            dict(
                reason=None,
                kind="fit_margin",
                bound="risk",
                qty=Decimal("1"),
                leverage=1,
                margin=Decimal("100"),
                real_risk_pct=Decimal("0.51200"),
            ),
            id="risk_bound",
        ),
        pytest.param(
            "boundary_1pct",
            dict(
                mark=Decimal("100"),
                stop=Decimal("99"),
                wallet_balance=Decimal("100"),
                available=Decimal("500"),
                min_qty=Decimal("0.01"),
                min_notional=Decimal("5"),
                step=Decimal("0.01"),
                risk_qty=Decimal("1"),
                max_real_risk_pct=Decimal("1"),
            ),
            dict(
                reason=None,
                kind="fit_margin",
                bound="risk_1pct",
                qty=Decimal("0.89"),
                leverage=1,
                margin=Decimal("89.00"),
                real_risk_pct=Decimal("0.996800"),
            ),
            id="boundary_1pct",
        ),
        pytest.param(
            "must1_d1",
            dict(
                mark=BTC_MARK,
                stop=Decimal("62440"),
                wallet_balance=BTC_WALLET,
                available=Decimal("43.5"),
                min_qty=BTC_MIN_QTY,
                min_notional=BTC_MIN_NOTIONAL,
                step=Decimal("0.0001"),
                risk_qty=Decimal("0.0035"),
            ),
            dict(
                reason=None,
                kind="fit_margin",
                bound="risk_1pct",
                qty=Decimal("0.0028"),
                leverage=5,
                margin=Decimal("34.7760"),
                real_risk_pct=Decimal("0.9672133333333333333333333333"),
                q_min=Decimal("0.0017"),
                q_fit=Decimal("0.0034"),
                q_1pct=Decimal("0.0028"),
            ),
            id="must1_d1",
        ),
        pytest.param(
            "min_qty_dom",
            dict(
                mark=Decimal("100"),
                stop=Decimal("101"),
                wallet_balance=Decimal("1000"),
                available=Decimal("50"),
                min_qty=Decimal("0.5"),
                min_notional=Decimal("5"),
                step=Decimal("0.1"),
                risk_qty=Decimal("0"),
            ),
            dict(
                reason=None,
                kind="min_lot",
                bound="min_lot",
                qty=Decimal("0.5"),
                leverage=2,
                margin=Decimal("25.0"),
                real_risk_pct=Decimal("0.05600"),
            ),
            id="min_qty_dom",
        ),
    ],
)
def test_size_offer_matrix(case_id: str, kwargs: dict[str, Any], expect: dict[str, Any]) -> None:
    del case_id
    result = size_offer(**kwargs)
    assert result.reason == expect["reason"]
    assert result.offer is not None
    offer = result.offer
    assert offer.kind == expect["kind"]
    assert offer.bound == expect["bound"]
    assert offer.qty == expect["qty"]
    assert offer.leverage == expect["leverage"]
    assert offer.margin == expect["margin"]
    assert offer.real_risk_pct == expect["real_risk_pct"]
    assert result.candidate == offer
    if "q_min" in expect:
        assert result.q_min == expect["q_min"]
        assert result.q_fit == expect["q_fit"]
        assert result.q_1pct == expect["q_1pct"]


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
        ).reason
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
        ).reason
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
        ).reason
        == "leverage_over_5x"
    )


def test_size_offer_e1a_q_fit_below_min() -> None:
    result = size_offer(
        mark=Decimal("100"),
        stop=Decimal("102"),
        wallet_balance=Decimal("100"),
        available=Decimal("2"),
        min_qty=Decimal("0.1"),
        min_notional=Decimal("5"),
        step=Decimal("0.01"),
        risk_qty=Decimal("0"),
    )
    assert result.reason == "leverage_over_5x"
    assert result.offer is None
    assert result.q_fit == Decimal("0.09")
    assert result.q_min == Decimal("0.1")
    assert result.need_margin == Decimal("2.053061224489795918367346939")
    assert result.candidate is not None
    assert result.candidate.leverage == 6


def test_size_offer_e1b_zero_available() -> None:
    result = size_offer(
        mark=Decimal("100"),
        stop=Decimal("98"),
        wallet_balance=Decimal("100"),
        available=Decimal("0"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("5"),
        step=Decimal("0.01"),
        risk_qty=Decimal("0"),
    )
    assert result.reason == "leverage_over_5x"
    assert result.offer is None
    assert result.candidate is None
    assert result.q_fit is None
    assert result.q_1pct == Decimal("0.47")


def test_size_offer_risk_over_1pct() -> None:
    result = size_offer(
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
    assert result.offer is None
    assert result.reason == "risk_over_1pct"
    assert result.candidate is not None
    assert result.candidate.kind == "min_lot"
    assert result.candidate.real_risk_pct == Decimal("2.1200")


def test_size_offer_leverage_exactly_5_today() -> None:
    result = _btc_short_offer()
    assert result.offer is not None
    assert result.offer.leverage == 5


def test_fmt_pct_modes() -> None:
    assert fmt_pct(Decimal("0.6863000")) == "0.69"
    assert fmt_pct(Decimal("0.9150666666666666666666666667")) == "0.92"
    assert fmt_pct(Decimal("0.8800"), mode="floor") == "0.88"
    assert fmt_pct(Decimal("80.1200000"), mode="failed_limit", limit=Decimal("1")) == "80.12"


def test_fmt_pct_bug3_failed_limit_golden() -> None:
    """Half-up can show 1.00% while risk still exceeds the 1% ceiling copy."""
    over = Decimal("1.002")
    assert fmt_pct(over, mode="half_up") == "1.00"
    assert fmt_pct(over, mode="failed_limit", limit=Decimal("1")) == "1.01"
    edge = Decimal("1.004")
    assert fmt_pct(edge, mode="half_up") == "1.00"
    assert fmt_pct(edge, mode="failed_limit", limit=Decimal("1")) == "1.01"
    assert fmt_pct(Decimal("1.005"), mode="failed_limit", limit=Decimal("1")) == "1.01"


def test_min_lot_stop_max_golden() -> None:
    result = size_offer(
        mark=Decimal("50000"),
        stop=Decimal("10000"),
        wallet_balance=Decimal("100"),
        available=Decimal("500"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("100"),
        step=Decimal("0.001"),
        risk_qty=Decimal("0"),
    )
    cand = result.candidate
    assert cand is not None
    dist, pct, price = min_lot_stop_max(
        candidate=cand,
        mark=Decimal("50000"),
        stop=Decimal("10000"),
        wallet_balance=Decimal("100"),
        tick=Decimal("0.1"),
    )
    assert dist == Decimal("440.0000")
    assert pct == Decimal("0.8800")
    assert price == Decimal("49560.0")
    assert fmt_pct(pct, mode="floor") == "0.88"
    assert fmt_pct(cand.real_risk_pct, mode="failed_limit", limit=Decimal("1")) == "80.12"


def test_card_today_btc_fit_margin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        min_notional={"BTCUSDT": BTC_MIN_NOTIONAL},
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
    assert "risk จริง: −0.82 USDT = 0.69%" in card["text"]
    assert "ตั้งใน Binance เอง" in card["text"]
    conn.close()


def test_card_must1_risk_1pct_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "m1.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = QuickFake(
        balance=BTC_WALLET,
        available=Decimal("43.5"),
        marks={"BTCUSDT": BTC_MARK},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": Decimal("0.0001")},
        min_qty={"BTCUSDT": BTC_MIN_QTY},
        min_notional={"BTCUSDT": BTC_MIN_NOTIONAL},
    )
    card = build_quick_card(
        conn,
        fake,
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62440"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert card["ok"] is True
    assert card["size_mode"] == "fit_margin"
    assert card["qty"] == Decimal("0.0028")
    assert "ใหญ่สุดที่ risk จริงรวมค่าธรรมเนียม ≤ 1%" in card["text"]
    assert "0.97%" in card["text"]
    assert "margin ≈ 34.78" in card["text"]
    legacy = build_quick_card(
        conn,
        fake,
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62440"),
        now_ms=NOW,
        allow_offer=False,
    )
    assert legacy["ok"] is True
    assert legacy.get("size_mode", "risk") == "risk"
    assert legacy["qty"] == Decimal("0.0035")
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
    assert card["qty"] == Decimal("10.1")
    assert "risk จริง: −0.53 USDT = 0.53%" in card["text"]
    assert "ตั้งใน Binance เอง" in card["text"]
    conn.close()


def test_card_1pct_refuses_min_lot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "r1.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
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
    assert "80.12%" in card["text"]
    assert "ไม้ขั้นต่ำรับ stop ได้ไกลสุด ≈ 0.88% (49560.0)" in card["text"]
    conn.close()


def test_registration_refusal_same_side_cap_text() -> None:
    text = _registration_refusal_text("same_side_cap")
    assert text == ("ไม่ลงทะเบียน — ฝั่งนี้เปิดอยู่ครบเพดานแล้ว (เพดานฝั่งละ 2) · ปิดหรือให้ทะเบียนหมดอายุก่อน")


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
    assert sent[0]["text"] == same_side_cap_text("long", 2)
    assert "LONG" in sent[0]["text"]
    row = conn.execute(
        "SELECT status, registration_id FROM assist_quick_card WHERE id=?",
        (cid,),
    ).fetchone()
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
        min_notional={"BTCUSDT": BTC_MIN_NOTIONAL},
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
    _assert_module_forbids(SRC / "quick_minlot.py")
    _assert_module_forbids(SRC / "quick_cap.py")


def test_quick_modules_ast_safety_negative_self_test(tmp_path: Path) -> None:
    bad = tmp_path / "bad_quick_module.py"
    bad.write_text("from jarvise_assist.execute import x\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="jarvise_assist.execute"):
        _assert_module_forbids(bad)


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
