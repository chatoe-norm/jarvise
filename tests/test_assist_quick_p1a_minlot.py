"""Quick P1a fix round 3 Step A — size_offer, cards, flows, same-side cap."""

from __future__ import annotations

import ast
import dataclasses
import itertools
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
from tests.test_assist_quick_guards import (
    _insert_quick_registration,
    _insert_quick_trade,
)
from tests.test_assist_quick_m1a1_round1 import NOW, _run_qk

from jarvise_assist.quick import write_quick_settings
from jarvise_assist.quick_cap import same_side_cap_reached, same_side_cap_text
from jarvise_assist.quick_card import (
    REFUSAL,
    _format_size_offer_refusal,
    build_quick_card,
    handle_quick_register_callback,
)
from jarvise_assist.quick_m1a1 import (
    MSG_SIZE_CHANGED,
    _quoted_markup_for,
    _refresh_only_markup,
    _registration_refusal_text,
    create_card_from_typed_command,
)
from jarvise_assist.quick_minlot import (
    SizeOffer,
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
BTC_RISK_QTY = Decimal("0.004")

BUG3_SHORT = (
    "❌ ไม่เปิด — ขั้นต่ำ 0.001 BTC เสี่ยง 2.74 USDT (1.93%) "
    "(ราคา 2.67 + ค่าธรรมเนียม 0.07) เกินเพดาน 1.42 USDT (1% ของ wallet 142) "
    "· stop ห่าง 4.50% กว้างเกินไปสำหรับ Quick "
    "· 👉 ไม้ขั้นต่ำรับ stop ได้ไกลสุด ≈ 2.27% (60678.9)"
)
FEE_FIT_TEXT = (
    "❌ margin ไม่พอแม้ใช้ 5x — ต้องใช้ ≈ 24.36 USDT (รวมค่าธรรมเนียม + กันไว้ 2%) "
    "มี 20 USDT · ขนาดใหญ่สุดที่พอ 0.001 < ขั้นต่ำ 0.002 BTC · 👉 โอนเข้า Futures หรือปิดไม้อื่น"
)


def _today_offer(**kw: Any) -> SizeOfferResult:
    base = dict(
        mark=BTC_MARK,
        stop=BTC_STOP_SHORT,
        wallet_balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        min_qty=BTC_MIN_QTY,
        min_notional=BTC_MIN_NOTIONAL,
        step=BTC_STEP,
        risk_qty=BTC_RISK_QTY,
    )
    base.update(kw)
    return size_offer(**base)


def _btc_fake(**kw: Any) -> QuickFake:
    defaults: dict[str, Any] = dict(
        balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        marks={"BTCUSDT": BTC_MARK},
        ticks={"BTCUSDT": Decimal("0.1")},
    )
    defaults["steps"] = {"BTCUSDT": kw.pop("step", BTC_STEP)}
    defaults.update(
        min_qty={"BTCUSDT": kw.pop("min_qty", BTC_MIN_QTY)},
        min_notional={"BTCUSDT": kw.pop("min_notional", BTC_MIN_NOTIONAL)},
    )
    defaults.update(kw)
    return QuickFake(**defaults)


def _must1_fake(**kw: Any) -> QuickFake:
    kw.setdefault("available", Decimal("43.5"))
    kw.setdefault("step", Decimal("0.0001"))
    kw.setdefault("min_notional", Decimal("100"))
    return _btc_fake(**kw)


def _assert_module_forbids(path: Path) -> None:
    def hit(name: str) -> bool:
        return any(name == b or name.startswith(b + ".") for b in _QUICK_FORBIDDEN_IMPORTS)

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not hit(alias.name), alias.name
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            module = node.module or ""
            if level >= 3:
                raise AssertionError("relative import level >= 3")
            if level == 0:
                base = module
            elif level == 1:
                base = "jarvise_assist" + ("." + module if module else "")
            else:
                base = module or ""
            for alias in node.names:
                candidates: list[str] = []
                if base:
                    candidates.extend([base, f"{base}.{alias.name}"])
                else:
                    candidates.append(alias.name)
                for resolved in candidates:
                    assert not hit(resolved), resolved


def _sui_card_conn(tmp_path: Path, *, risk_pct: float, cap_pct: float = 10.0) -> Any:
    conn = open_db(tmp_path / f"sui_{risk_pct}.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=risk_pct, cap_pct=cap_pct)
    return conn


def _a4_fixture(conn: Any, fake: QuickFake) -> tuple[str, dict[str, Any]]:
    parsed = {"symbol": "SUIUSDT", "side": "long", "stop": Decimal("1.994")}
    return create_card_from_typed_command(conn, fake, parsed, now_ms=NOW)


# --- A1: pure size_offer -------------------------------------------------------


def test_size_offer_today_like_min_notional_100_and_5() -> None:
    for mn, q_min in ((BTC_MIN_NOTIONAL, Decimal("0.002")), (Decimal("5"), Decimal("0.001"))):
        r = _today_offer(min_notional=mn)
        assert r.reason is None
        o = r.offer
        assert o is not None
        assert o.qty == Decimal("0.003")
        assert o.bound == "margin"
        assert o.leverage == 5
        assert o.margin == Decimal("37.26")
        assert o.real_risk_pct == Decimal("0.6863")
        assert r.q_fit == Decimal("0.003")
        assert r.q_1pct == Decimal("0.004")
        assert r.q_min == q_min


def test_size_offer_must1_pins() -> None:
    r = size_offer(
        mark=BTC_MARK,
        stop=Decimal("62440"),
        wallet_balance=BTC_WALLET,
        available=Decimal("43.5"),
        min_qty=BTC_MIN_QTY,
        min_notional=BTC_MIN_NOTIONAL,
        step=Decimal("0.0001"),
        risk_qty=Decimal("0.0035"),
    )
    o = r.offer
    assert o is not None
    assert r.q_min == Decimal("0.0017")
    assert r.q_fit == Decimal("0.0034")
    assert r.q_1pct == Decimal("0.0028")
    assert o.qty == Decimal("0.0028")
    assert o.kind == "fit_margin"
    assert o.bound == "risk_1pct"
    assert o.notional == Decimal("173.88")
    assert o.risk_usdt == Decimal("0.952")
    assert o.fee_usdt == Decimal("0.208656")
    assert o.leverage == 5
    assert o.margin == Decimal("34.776")
    expected_real = (Decimal("0.952") + Decimal("0.208656")) / Decimal("120") * Decimal("100")
    assert o.real_risk_pct == expected_real
    need = (Decimal("173.88") / 5 + Decimal("0.0012") * Decimal("173.88")) / Decimal("0.98")
    assert r.need_margin == need


def test_size_offer_e1a_mark10_margin_bound() -> None:
    r = size_offer(
        mark=Decimal("10"),
        stop=Decimal("10.2004"),
        wallet_balance=Decimal("1000"),
        available=Decimal("50"),
        min_qty=Decimal("0.1"),
        step=Decimal("0.1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("24.9"),
    )
    o = r.offer
    assert o is not None
    assert o.qty == Decimal("24.3")
    assert o.bound == "margin"
    assert o.notional == Decimal("243")
    assert o.leverage == 5
    assert o.margin == Decimal("48.6")
    assert o.real_risk_pct == Decimal("0.516132")


def test_size_offer_e1b_mark10_risk_1pct_bound() -> None:
    r = size_offer(
        mark=Decimal("10"),
        stop=Decimal("10.2004"),
        wallet_balance=Decimal("1000"),
        available=Decimal("100"),
        min_qty=Decimal("0.1"),
        step=Decimal("0.1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("49.9"),
    )
    assert r.q_fit == Decimal("48.7")
    assert r.q_1pct == Decimal("47.0")
    o = r.offer
    assert o is not None
    assert o.qty == Decimal("47.0")
    assert o.bound == "risk_1pct"
    assert o.notional == Decimal("470")
    assert o.leverage == 5
    assert o.margin == Decimal("94")
    assert o.real_risk_pct == Decimal("0.99828")


def test_size_offer_e1b_mark10_risk_half_pct_bound() -> None:
    r = size_offer(
        mark=Decimal("10"),
        stop=Decimal("10.2004"),
        wallet_balance=Decimal("1000"),
        available=Decimal("100"),
        min_qty=Decimal("0.1"),
        step=Decimal("0.1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("49.9"),
        max_real_risk_pct=Decimal("0.5"),
    )
    o = r.offer
    assert o is not None
    assert o.qty == Decimal("23.5")
    assert o.bound == "risk_1pct"
    assert o.real_risk_pct == Decimal("0.49914")


def test_size_offer_q_fit_below_min_leverage_six() -> None:
    """Former test_size_offer_e1a_q_fit_below_min — leverage 6 when q_fit < q_min."""
    r = size_offer(
        mark=Decimal("100"),
        stop=Decimal("102"),
        wallet_balance=Decimal("100"),
        available=Decimal("2"),
        min_qty=Decimal("0.1"),
        min_notional=Decimal("5"),
        step=Decimal("0.01"),
        risk_qty=Decimal("0"),
    )
    assert r.reason == "leverage_over_5x"
    assert r.offer is None
    assert r.q_fit == Decimal("0.09")
    assert r.q_min == Decimal("0.1")
    assert r.need_margin == Decimal("2.053061224489795918367346939")
    assert r.candidate is not None
    assert r.candidate.leverage == 6


def test_size_offer_available_zero_no_candidate() -> None:
    """Former test_size_offer_e1b_zero_available."""
    r = size_offer(
        mark=Decimal("100"),
        stop=Decimal("98"),
        wallet_balance=Decimal("100"),
        available=Decimal("0"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("5"),
        step=Decimal("0.01"),
        risk_qty=Decimal("0"),
    )
    assert r.reason == "leverage_over_5x"
    assert r.offer is None
    assert r.candidate is None
    assert r.q_fit is None
    assert r.q_1pct == Decimal("0.47")


@pytest.mark.parametrize(
    ("case_id", "kwargs", "expect"),
    [
        pytest.param(
            "today_btc_margin",
            dict(min_notional=BTC_MIN_NOTIONAL),
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
            "near_1pct",
            dict(
                mark=Decimal("100"),
                stop=Decimal("99"),
                wallet_balance=Decimal("100"),
                available=Decimal("500"),
                min_qty=Decimal("0.01"),
                min_notional=Decimal("5"),
                step=Decimal("0.01"),
                risk_qty=Decimal("1"),
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
            id="near_1pct",
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
            "min_notional_dom",
            dict(min_notional=BTC_MIN_NOTIONAL, risk_qty=Decimal("0")),
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
    ],
)
def test_size_offer_matrix(case_id: str, kwargs: dict[str, Any], expect: dict[str, Any]) -> None:
    del case_id
    base = dict(
        mark=BTC_MARK,
        stop=BTC_STOP_SHORT,
        wallet_balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        min_qty=BTC_MIN_QTY,
        step=BTC_STEP,
        risk_qty=BTC_RISK_QTY,
        min_notional=BTC_MIN_NOTIONAL,
    )
    base.update(kwargs)
    result = size_offer(**base)
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


def test_size_offer_real_risk_exactly_one_percent() -> None:
    r = size_offer(
        mark=Decimal("100"),
        stop=Decimal("99.12"),
        wallet_balance=Decimal("100"),
        available=Decimal("500"),
        min_qty=Decimal("1"),
        step=Decimal("1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("0"),
    )
    assert r.offer is not None
    assert r.offer.real_risk_pct == Decimal("1.00")


def test_size_offer_real_risk_over_one_percent_min_lot() -> None:
    r = size_offer(
        mark=Decimal("100"),
        stop=Decimal("99.11"),
        wallet_balance=Decimal("100"),
        available=Decimal("500"),
        min_qty=Decimal("1"),
        step=Decimal("1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("0"),
    )
    assert r.offer is None
    assert r.reason == "risk_over_1pct"
    assert r.candidate is not None
    assert r.candidate.kind == "min_lot"
    assert r.candidate.real_risk_pct == Decimal("1.01")


def test_size_offer_leverage_boundary_five_vs_six() -> None:
    base = dict(
        mark=Decimal("100"),
        stop=Decimal("99"),
        wallet_balance=Decimal("1000"),
        min_qty=Decimal("1"),
        step=Decimal("1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("0"),
    )
    ok = size_offer(available=Decimal("21"), **base)
    assert ok.offer is not None
    assert ok.offer.leverage == 5
    bad = size_offer(available=Decimal("20"), **base)
    assert bad.reason == "leverage_over_5x"
    assert bad.candidate is not None
    assert bad.candidate.leverage == 6


def test_size_offer_denom_zero_leverage_six() -> None:
    r = size_offer(
        mark=Decimal("98"),
        stop=Decimal("97"),
        wallet_balance=Decimal("100000"),
        available=Decimal("1.2"),
        min_qty=Decimal("10"),
        step=Decimal("1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("0"),
    )
    assert r.reason == "leverage_over_5x"
    assert r.offer is None
    assert r.candidate is not None
    assert r.candidate.leverage == 6
    assert r.need_margin == Decimal("201.2")


def test_size_offer_fee_inclusive_q_fit_zero() -> None:
    r = size_offer(
        mark=Decimal("100"),
        stop=Decimal("101"),
        wallet_balance=Decimal("1000"),
        available=Decimal("20.5"),
        min_qty=Decimal("1"),
        step=Decimal("1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("5"),
    )
    assert r.q_fit == Decimal("0")
    assert r.reason == "leverage_over_5x"
    assert r.candidate is not None
    assert r.candidate.kind == "min_lot"
    assert r.candidate.qty == Decimal("1")
    assert r.candidate.leverage == 6


def test_size_offer_fee_fit_leverage_seven() -> None:
    r = size_offer(
        mark=Decimal("59330.1"),
        stop=Decimal("59626.8"),
        wallet_balance=Decimal("142"),
        available=Decimal("20"),
        min_qty=Decimal("0.001"),
        step=Decimal("0.001"),
        min_notional=Decimal("100"),
        risk_qty=Decimal("0.004"),
    )
    assert r.q_fit == Decimal("0.001")
    assert r.q_min == Decimal("0.002")
    assert r.q_1pct == Decimal("0.003")
    assert r.reason == "leverage_over_5x"
    c = r.candidate
    assert c is not None
    assert c.qty == Decimal("0.002")
    assert c.notional == Decimal("118.6602")
    assert c.risk_usdt == Decimal("0.5934")
    assert c.fee_usdt == Decimal("0.14239224")
    assert c.leverage == 7
    assert r.need_margin == Decimal("24.36166555102040816326530612")


@pytest.mark.parametrize(
    ("min_notional", "q_min"),
    [(Decimal("5"), Decimal("0.05")), (Decimal("5.01"), Decimal("0.06"))],
)
def test_size_offer_step_ceil_boundaries(min_notional: Decimal, q_min: Decimal) -> None:
    r = size_offer(
        mark=Decimal("100"),
        stop=Decimal("101"),
        wallet_balance=Decimal("1000"),
        available=Decimal("500"),
        min_qty=Decimal("0.01"),
        step=Decimal("0.01"),
        min_notional=min_notional,
        risk_qty=Decimal("0"),
    )
    assert r.q_min == q_min


def test_size_offer_fit_margin_risk_qty_small() -> None:
    r = _today_offer(risk_qty=Decimal("0.002"))
    o = r.offer
    assert o is not None
    assert o.kind == "fit_margin"
    assert o.bound == "risk"
    assert o.qty == Decimal("0.002")


def test_size_offer_qmin_equals_qfit_margin_bound() -> None:
    r = _today_offer(min_notional=Decimal("150"), risk_qty=Decimal("0.006"))
    o = r.offer
    assert r.q_min == Decimal("0.003")
    assert r.q_fit == Decimal("0.003")
    assert o is not None
    assert o.kind == "fit_margin"
    assert o.qty == Decimal("0.003")
    assert o.bound == "margin"


def test_size_offer_risk_qty_at_qmin_is_fit_margin_not_min_lot() -> None:
    r = _today_offer(risk_qty=Decimal("0.002"))
    assert r.q_min == Decimal("0.002")
    assert r.offer is not None
    assert r.offer.kind == "fit_margin"
    assert r.offer.qty == Decimal("0.002")


def test_size_offer_tie_qfit_q1pct_prefers_margin() -> None:
    r = size_offer(
        mark=BTC_MARK,
        stop=BTC_STOP_SHORT,
        wallet_balance=Decimal("100"),
        available=BTC_AVAILABLE,
        min_qty=BTC_MIN_QTY,
        min_notional=BTC_MIN_NOTIONAL,
        step=BTC_STEP,
        risk_qty=BTC_RISK_QTY,
    )
    assert r.q_fit == Decimal("0.003")
    assert r.q_1pct == Decimal("0.003")
    assert r.offer is not None
    assert r.offer.bound == "margin"


_NON_FINITE = [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity")]


@pytest.mark.parametrize("bad", _NON_FINITE)
@pytest.mark.parametrize(
    "field",
    [
        "mark",
        "stop",
        "wallet_balance",
        "available",
        "min_qty",
        "min_notional",
        "step",
        "risk_qty",
        "fee_bps_per_side",
        "max_real_risk_pct",
        "margin_buffer",
    ],
)
def test_size_offer_guards_non_finite(field: str, bad: Decimal) -> None:
    base: dict[str, Any] = dict(
        mark=Decimal("100"),
        stop=Decimal("101"),
        wallet_balance=Decimal("1000"),
        available=Decimal("500"),
        min_qty=Decimal("1"),
        min_notional=Decimal("5"),
        step=Decimal("1"),
        risk_qty=Decimal("1"),
        fee_bps_per_side=Decimal("6"),
        max_real_risk_pct=Decimal("1"),
        margin_buffer=Decimal("0.98"),
    )
    base[field] = bad
    assert size_offer(**base).reason == "bad_input"


def test_size_offer_guards_step_mark_stop_and_negative_risk() -> None:
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
            stop=Decimal("1"),
            wallet_balance=Decimal("1"),
            available=Decimal("1"),
            min_qty=Decimal("1"),
            min_notional=Decimal("1"),
            step=Decimal("0"),
            risk_qty=Decimal("0"),
        ).reason
        == "bad_input"
    )
    assert (
        size_offer(
            mark=Decimal("1"),
            stop=Decimal("2"),
            wallet_balance=Decimal("100"),
            available=Decimal("1"),
            min_qty=Decimal("1"),
            min_notional=Decimal("1"),
            step=Decimal("1"),
            risk_qty=Decimal("-1"),
        ).reason
        == "bad_input"
    )


def test_size_offer_guard_stop_equals_mark() -> None:
    assert (
        size_offer(
            mark=Decimal("100"),
            stop=Decimal("100"),
            wallet_balance=Decimal("1000"),
            available=Decimal("500"),
            min_qty=Decimal("1"),
            min_notional=Decimal("5"),
            step=Decimal("1"),
            risk_qty=Decimal("0"),
        ).reason
        == "bad_input"
    )


def test_size_offer_guards_wallet_zero_and_available_zero() -> None:
    r0 = size_offer(
        mark=Decimal("1"),
        stop=Decimal("2"),
        wallet_balance=Decimal("0"),
        available=Decimal("1"),
        min_qty=Decimal("1"),
        min_notional=Decimal("1"),
        step=Decimal("1"),
        risk_qty=Decimal("0"),
    )
    assert r0.reason == "no_balance"
    assert r0.q_min is None and r0.q_fit is None and r0.q_1pct is None and r0.need_margin is None
    r1 = _today_offer(available=Decimal("0"), risk_qty=Decimal("0"))
    assert r1.reason == "leverage_over_5x"
    assert r1.candidate is None
    assert r1.q_fit is None
    assert r1.q_min is not None


def test_size_offer_guards_float_in_decimal_slot() -> None:
    r = size_offer(
        mark=100.0,  # type: ignore[arg-type]
        stop=Decimal("101"),
        wallet_balance=Decimal("1000"),
        available=Decimal("500"),
        min_qty=Decimal("1"),
        min_notional=Decimal("5"),
        step=Decimal("1"),
        risk_qty=Decimal("1"),
    )
    assert r.reason == "bad_input"


def test_size_offer_property_grid() -> None:
    marks = [Decimal("50"), Decimal("100"), Decimal("200")]
    stops = [Decimal("49"), Decimal("100.5"), Decimal("201")]
    availables = [Decimal("10"), Decimal("50"), Decimal("200")]
    risk_qtys = [Decimal("0"), Decimal("1"), Decimal("5")]
    for mark, stop, av, rq in itertools.product(marks, stops, availables, risk_qtys):
        if stop == mark:
            continue
        r = size_offer(
            mark=mark,
            stop=stop,
            wallet_balance=Decimal("1000"),
            available=av,
            min_qty=Decimal("0.1"),
            min_notional=Decimal("5"),
            step=Decimal("0.1"),
            risk_qty=rq,
        )
        if r.offer is not None and r.offer.kind == "fit_margin":
            o = r.offer
            assert o.real_risk_pct <= Decimal("1")
            assert o.leverage <= 5
            assert r.q_min is not None and o.qty >= r.q_min
            if rq > 0:
                assert o.qty <= rq
        if r.reason == "risk_over_1pct":
            assert r.candidate is not None
            assert r.candidate.kind == "min_lot"


def test_fmt_pct_half_up() -> None:
    assert fmt_pct(Decimal("2")) == "2.00"
    assert fmt_pct(Decimal("4.50006")) == "4.50"
    assert fmt_pct(Decimal("0.6863")) == "0.69"
    assert fmt_pct(Decimal("0.005")) == "0.01"


def test_fmt_pct_failed_limit() -> None:
    assert fmt_pct(Decimal("1.004"), mode="failed_limit", limit=Decimal("1")) == "1.01"
    assert fmt_pct(Decimal("1.006"), mode="failed_limit", limit=Decimal("1")) == "1.01"
    assert fmt_pct(Decimal("0.999"), mode="failed_limit", limit=Decimal("1")) == "1.00"


def test_fmt_pct_floor() -> None:
    assert fmt_pct(Decimal("2.2734"), mode="floor") == "2.27"
    assert fmt_pct(Decimal("2.2799"), mode="floor") == "2.27"


def test_fmt_pct_failed_limit_requires_limit() -> None:
    with pytest.raises(ValueError):
        fmt_pct(Decimal("1"), mode="failed_limit")


def test_min_lot_stop_max_tick_short_and_long() -> None:
    cand = size_offer(
        mark=Decimal("59330.1"),
        stop=Decimal("62000"),
        wallet_balance=Decimal("142"),
        available=Decimal("142"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        step=Decimal("0.001"),
        risk_qty=Decimal("0"),
    ).candidate
    assert cand is not None
    _, _, short_p = min_lot_stop_max(
        candidate=cand,
        mark=Decimal("59330.1"),
        stop=Decimal("62000"),
        wallet_balance=Decimal("142"),
        tick=Decimal("0.1"),
    )
    assert short_p == Decimal("60678.9")
    _, _, long_p = min_lot_stop_max(
        candidate=cand,
        mark=Decimal("59330.1"),
        stop=Decimal("56660.2"),
        wallet_balance=Decimal("142"),
        tick=Decimal("0.1"),
    )
    assert long_p == Decimal("57981.3")


# --- A2: build_quick_card ------------------------------------------------------


def test_card_bug3_short_text_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "n.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = QuickFake(
        balance=Decimal("142"),
        available=Decimal("142"),
        marks={"BTCUSDT": Decimal("59330.1")},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": Decimal("0.001")},
        min_qty={"BTCUSDT": Decimal("0.001")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    card = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("62000"), now_ms=NOW, allow_offer=True
    )
    assert card["text"] == BUG3_SHORT
    assert "ลองใหม่" not in card["text"]
    conn.close()


def test_card_bug3_long_and_wallet_five(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "n2.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    base = dict(
        balance=Decimal("142"),
        available=Decimal("142"),
        marks={"BTCUSDT": Decimal("59330.1")},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": Decimal("0.001")},
        min_qty={"BTCUSDT": Decimal("0.001")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    card = build_quick_card(
        conn,
        QuickFake(**base),
        symbol="BTCUSDT",
        side="long",
        stop=Decimal("56660.2"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert card["text"].endswith("≈ 2.27% (57981.3)")
    card5 = build_quick_card(
        conn,
        QuickFake(
            balance=Decimal("5"),
            available=Decimal("5"),
            **{k: v for k, v in base.items() if k not in {"balance", "available"}},
        ),
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62000"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert card5["text"].endswith("👉 ขั้นต่ำของสัญญาใหญ่เกินสำหรับ wallet นี้")
    card57 = build_quick_card(
        conn,
        QuickFake(**base),
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62040"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert "4.57%" in card57["text"]
    conn.close()


def test_card_stop_max_dist_zero_n2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "n2z.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
    fake = QuickFake(
        balance=Decimal("12"),
        available=Decimal("500"),
        marks={"BTCUSDT": Decimal("100")},
        ticks={"BTCUSDT": Decimal("0.01")},
        steps={"BTCUSDT": Decimal("1")},
        min_qty={"BTCUSDT": Decimal("1")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    card = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("101"), now_ms=NOW, allow_offer=True
    )
    assert card["reason"] == "risk_over_1pct"
    assert "(9.33%)" in card["text"]
    assert card["text"].endswith("👉 ขั้นต่ำของสัญญาใหญ่เกินสำหรับ wallet นี้")
    assert "ลองใหม่" not in card["text"]
    cand = size_offer(
        mark=Decimal("100"),
        stop=Decimal("101"),
        wallet_balance=Decimal("12"),
        available=Decimal("500"),
        min_qty=Decimal("1"),
        step=Decimal("1"),
        min_notional=Decimal("5"),
        risk_qty=Decimal("0"),
        max_real_risk_pct=Decimal("1"),
    ).candidate
    assert cand is not None
    dist, pct, price = min_lot_stop_max(
        candidate=cand,
        mark=Decimal("100"),
        stop=Decimal("101"),
        wallet_balance=Decimal("12"),
        tick=Decimal("0.01"),
    )
    assert dist == Decimal("0")
    assert pct == Decimal("0")
    assert price is None
    conn.close()


def test_card_fee_fit_refusal_o(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "o.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = QuickFake(
        balance=Decimal("142"),
        available=Decimal("20"),
        marks={"BTCUSDT": Decimal("59330.1")},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": Decimal("0.001")},
        min_qty={"BTCUSDT": Decimal("0.001")},
        min_notional={"BTCUSDT": Decimal("100")},
    )
    card = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("59626.8"), now_ms=NOW, allow_offer=True
    )
    assert card["reason"] == "leverage_over_5x"
    assert card["text"] == FEE_FIT_TEXT
    assert "ลองใหม่" not in card["text"]
    conn.close()


MUST1_LEV_LINE = "Leverage ที่ต้องใช้: 5x isolated · margin ≈ 34.78 USDT — ตั้งใน Binance เองก่อนเปิด · บอทไม่เปลี่ยนให้"
MIN_LOT_LEV_LINE = "Leverage ที่ต้องใช้: 1x isolated · margin ≈ 300 USDT — ตั้งใน Binance เองก่อนเปิด · บอทไม่เปลี่ยนให้"
AVAILABLE_ZERO_OFFER_TEXT = "❌ margin ไม่พอ — มีให้ใช้ 0 USDT ใน Futures · 👉 โอน USDT เข้า Futures หรือปิดไม้อื่น"


def test_card_available_zero_refusal_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "x5.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = QuickFake(
        balance=Decimal("120"),
        available=Decimal("0"),
        marks={"BTCUSDT": Decimal("62100")},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": Decimal("0.001")},
        min_qty={"BTCUSDT": Decimal("0.001")},
        min_notional={"BTCUSDT": Decimal("100")},
    )
    offered = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("62300"), now_ms=NOW, allow_offer=True
    )
    assert offered["reason"] == "leverage_over_5x"
    assert offered["text"] == AVAILABLE_ZERO_OFFER_TEXT
    legacy = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("62300"), now_ms=NOW, allow_offer=False
    )
    assert legacy["reason"] == "margin_cap"
    assert legacy["text"] == (
        "❌ margin ไม่พอ — ขนาดตาม risk 0.004 BTC ≈ 248.4 USDT "
        "ต้องใช้ ≈ 49.68 USDT ที่ 5x มี 0 USDT · 👉 พิมพ์ q ใหม่ บอทจะเสนอขนาดที่ margin พอ"
    )
    assert "ลองใหม่" not in "\n".join([offered["text"], legacy["text"]])
    conn.close()


def test_card_must1_offer_and_legacy_p(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "p.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = _must1_fake()
    offered = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("62440"), now_ms=NOW, allow_offer=True
    )
    assert offered["ok"] is True
    assert offered["size_mode"] == "risk"
    assert offered["qty"] == Decimal("0.0028")
    assert "= 0.97%" in offered["text"]
    assert "margin ≈ 34.78" in offered["text"]
    assert "Leverage ที่ต้องใช้: 5x · margin ≈ 34.78 USDT" in offered["text"].splitlines()
    legacy = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("62440"), now_ms=NOW, allow_offer=False
    )
    assert legacy["ok"] is True
    assert legacy.get("size_mode", "risk") == "risk"
    assert legacy["qty"] == Decimal("0.0028")
    assert "ขนาด: 0.0028 BTC ≈ 173.88 USDT" in legacy["text"]
    assert "Leverage ที่ต้องใช้: 5x · margin ≈ 34.78 USDT" in legacy["text"]
    assert "risk จริง: −1.16 USDT = 0.97% ของ Futures 120 USDT (เป้า 1%)" in legacy["text"]
    assert "ตั้งใน Binance" not in legacy["text"]
    assert "ลองใหม่" not in legacy["text"]
    conn.close()


def _sui_fake(available: Decimal, *, wallet: Decimal = Decimal("1000")) -> QuickFake:
    return QuickFake(
        balance=wallet,
        available=available,
        marks={"SUIUSDT": Decimal("10")},
        ticks={"SUIUSDT": Decimal("0.0001")},
        steps={"SUIUSDT": Decimal("0.1")},
        min_qty={"SUIUSDT": Decimal("0.1")},
        min_notional={"SUIUSDT": Decimal("5")},
    )


def test_card_e1_sui_offer_and_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "e1.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
    c_a = build_quick_card(
        conn,
        _sui_fake(Decimal("50")),
        symbol="SUIUSDT",
        side="short",
        stop=Decimal("10.2004"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert c_a["size_mode"] == "risk"
    assert c_a["qty"] == Decimal("23.5")
    write_quick_settings(conn, risk_pct=1.0)
    c_b = build_quick_card(
        conn,
        _sui_fake(Decimal("100")),
        symbol="SUIUSDT",
        side="short",
        stop=Decimal("10.2004"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert c_b["qty"] == Decimal("47.0")
    assert c_b["size_mode"] == "risk"
    assert "= 1.00%" in c_b["text"]
    write_quick_settings(conn, risk_pct=0.5)
    leg_a = build_quick_card(
        conn,
        _sui_fake(Decimal("50")),
        symbol="SUIUSDT",
        side="short",
        stop=Decimal("10.2004"),
        now_ms=NOW,
        allow_offer=False,
    )
    write_quick_settings(conn, risk_pct=1.0)
    leg_b = build_quick_card(
        conn,
        _sui_fake(Decimal("100")),
        symbol="SUIUSDT",
        side="short",
        stop=Decimal("10.2004"),
        now_ms=NOW,
        allow_offer=False,
    )
    assert leg_a["qty"] == Decimal("23.5")
    assert leg_b["qty"] == Decimal("47.0")
    assert "risk จริง: −4.99 USDT = 0.50% ของ Futures 1000 USDT (เป้า 0.5%)" in leg_a["text"]
    assert "risk จริง: −9.98 USDT = 1.00% ของ Futures 1000 USDT (เป้า 1%)" in leg_b["text"]
    assert "ลองใหม่" not in "\n".join([leg_a["text"], leg_b["text"]])
    conn.close()


def test_card_leverage_cap_boundary_q(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "q.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = QuickFake(
        balance=Decimal("142"),
        available=Decimal("142"),
        marks={"BTCUSDT": Decimal("59330.1")},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": Decimal("0.001")},
        min_qty={"BTCUSDT": Decimal("0.001")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    ok = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("59377.6"), now_ms=NOW, allow_offer=True
    )
    assert ok["ok"] is True
    assert ok["qty"] == Decimal("0.011")
    assert ok["size_mode"] == "risk"
    assert int(ok["leverage"]) == 5
    bad = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("59371.7"), now_ms=NOW, allow_offer=True
    )
    assert bad["reason"] == "leverage_cap"
    conn.close()


def test_card_item2_margin_boundary_r(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "r.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
    fake = QuickFake(
        balance=Decimal("200"),
        available=Decimal("20.12"),
        marks={"BTCUSDT": Decimal("98")},
        ticks={"BTCUSDT": Decimal("0.01")},
        steps={"BTCUSDT": Decimal("1")},
        min_qty={"BTCUSDT": Decimal("1")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    ok = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="long", stop=Decimal("97"), now_ms=NOW, allow_offer=True
    )
    assert ok["ok"] is True
    assert ok["size_mode"] == "min_lot"
    assert ok["qty"] == Decimal("1")
    fake.available = Decimal("20.11")
    bad = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="long", stop=Decimal("97"), now_ms=NOW, allow_offer=True
    )
    assert bad["reason"] == "leverage_over_5x"
    conn.close()


def test_card_real_risk_percent_pins_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "s.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.25)
    fake = QuickFake(
        balance=Decimal("600"),
        available=Decimal("500"),
        marks={"BTCUSDT": Decimal("100")},
        ticks={"BTCUSDT": Decimal("0.01")},
        steps={"BTCUSDT": Decimal("1")},
        min_qty={"BTCUSDT": Decimal("3")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    c1 = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="long", stop=Decimal("99.12"), now_ms=NOW, allow_offer=True
    )
    assert c1["size_mode"] == "min_lot"
    assert "risk จริง: −3 USDT = 0.50% ของ Futures 600 USDT" in c1["text"]
    assert MIN_LOT_LEV_LINE in c1["text"].splitlines()
    write_quick_settings(conn, risk_pct=0.5)
    fake2 = QuickFake(
        balance=Decimal("100"),
        available=Decimal("500"),
        marks={"BTCUSDT": Decimal("100")},
        ticks={"BTCUSDT": Decimal("0.01")},
        steps={"BTCUSDT": Decimal("1")},
        min_qty={"BTCUSDT": Decimal("1")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    c2 = build_quick_card(
        conn, fake2, symbol="BTCUSDT", side="long", stop=Decimal("99.12"), now_ms=NOW, allow_offer=True
    )
    assert "= 1.00%" in c2["text"]
    conn.close()


def test_card_failed_limit_ceiling_s3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "s3.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
    fake = QuickFake(
        balance=Decimal("100"),
        available=Decimal("500"),
        marks={"BTCUSDT": Decimal("100")},
        ticks={"BTCUSDT": Decimal("0.001")},
        steps={"BTCUSDT": Decimal("1")},
        min_qty={"BTCUSDT": Decimal("1")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    card = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="long", stop=Decimal("99.116"), now_ms=NOW, allow_offer=True
    )
    assert card["reason"] == "risk_over_1pct"
    assert "(1.01%)" in card["text"]
    assert "ลองใหม่" not in card["text"]
    conn.close()


def test_card_wallet_zero_maps_no_balance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "t0.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = QuickFake(
        balance=Decimal("0"),
        available=Decimal("10"),
        marks={"BTCUSDT": BTC_MARK},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": BTC_STEP},
        min_qty={"BTCUSDT": BTC_MIN_QTY},
        min_notional={"BTCUSDT": BTC_MIN_NOTIONAL},
    )
    card = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=BTC_STOP_SHORT, now_ms=NOW, allow_offer=True
    )
    assert card["reason"] == "size_failed"
    assert card["text"] == "❌ คำนวณขนาดไม่ได้ — wallet 0 USDT · risk 1% · 👉 ตรวจยอดใน Futures"
    assert "ลองใหม่" not in card["text"]
    pure = size_offer(
        mark=BTC_MARK,
        stop=BTC_STOP_SHORT,
        wallet_balance=Decimal("0"),
        available=Decimal("10"),
        min_qty=BTC_MIN_QTY,
        min_notional=BTC_MIN_NOTIONAL,
        step=BTC_STEP,
        risk_qty=Decimal("0.006"),
    )
    assert pure.reason == "no_balance"
    conn.close()


def test_card_quick_cap_with_fit_margin_offer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "t1.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0, cap_pct=1.0)
    _insert_quick_trade(
        conn, symbol="ETHUSDT", side="short", entry_qty=0.07, entry_avg=100.0, planned_stop=90.0, now_ms=NOW
    )
    fake = _btc_fake()
    card = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=BTC_STOP_SHORT, now_ms=NOW, allow_offer=True
    )
    assert card["reason"] == "quick_cap"

    conn2 = open_db(tmp_path / "t1b.db")
    _seed_settings(conn2)
    write_quick_settings(conn2, risk_pct=1.0, cap_pct=1.0)
    _insert_quick_trade(
        conn2, symbol="ETHUSDT", side="short", entry_qty=0.06, entry_avg=100.0, planned_stop=90.0, now_ms=NOW
    )
    card_ok = build_quick_card(
        conn2, _btc_fake(), symbol="BTCUSDT", side="short", stop=BTC_STOP_SHORT, now_ms=NOW, allow_offer=True
    )
    assert card_ok["ok"] is True
    assert card_ok["size_mode"] == "fit_margin"
    assert card_ok["qty"] == Decimal("0.003")
    conn.close()
    conn2.close()


# --- A3: flows -----------------------------------------------------------------


def test_flow_mark_move_min_lot_refusal_u(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "u.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=0.5)
    fake = QuickFake(
        balance=Decimal("100"),
        available=Decimal("500"),
        marks={"BTCUSDT": Decimal("100")},
        ticks={"BTCUSDT": Decimal("0.01")},
        steps={"BTCUSDT": Decimal("1")},
        min_qty={"BTCUSDT": Decimal("1")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    cid, card = create_card_from_typed_command(
        conn, fake, {"symbol": "BTCUSDT", "side": "long", "stop": Decimal("99.12")}, now_ms=NOW
    )
    assert card["size_mode"] == "min_lot"
    fake.marks["BTCUSDT"] = Decimal("100.01")
    sent, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("reason") == "risk_over_1pct"
    assert answers == [None]
    assert "(1.01%)" in sent[0]["text"]
    assert "ลองใหม่" not in "\n".join(m["text"] for m in sent)
    assert conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0] == 0
    conn.close()


def test_flow_mark_move_fit_margin_u2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "u2.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    # Fee-aware risk qty fits in ~34.78 USDT margin; keep available under that
    # so the card starts as fit_margin (qty_mismatch / mode drift still apply).
    fake = _must1_fake(available=Decimal("34"))
    parsed = {"symbol": "BTCUSDT", "side": "short", "stop": Decimal("62440")}
    cid, card = create_card_from_typed_command(conn, fake, parsed, now_ms=NOW)
    assert card["size_mode"] == "fit_margin"
    assert card["qty"] == Decimal("0.0026")
    fake.marks["BTCUSDT"] = Decimal("62000")
    sent, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("reason") == "size_changed"
    assert sent[0]["text"] == MSG_SIZE_CHANGED
    assert answers == [None]
    assert "ลองใหม่" not in "\n".join(m["text"] for m in sent)
    conn.close()


def test_flow_available_shrink_fit_margin_v(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "v.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = _btc_fake()
    cid, card = create_card_from_typed_command(
        conn, fake, {"symbol": "BTCUSDT", "side": "short", "stop": BTC_STOP_SHORT}, now_ms=NOW
    )
    assert card["qty"] == Decimal("0.003")
    fake.available = Decimal("30")
    sent, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("reason") == "size_changed"
    assert sent[0]["text"] == MSG_SIZE_CHANGED
    assert answers == [None]
    conn.close()


def test_flow_mode_drift_risk_to_offer_w(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "w1.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    parsed = {"symbol": "BTCUSDT", "side": "short", "stop": Decimal("62440")}
    fake = _must1_fake(available=Decimal("100"))
    cid, card = create_card_from_typed_command(conn, fake, parsed, now_ms=NOW)
    assert card.get("size_mode", "risk") == "risk"
    fake.available = Decimal("34")
    sent, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("reason") == "size_changed"
    assert sent[0]["text"] == MSG_SIZE_CHANGED
    conn.close()


def test_flow_mode_drift_offer_to_risk_w(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "w2.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    parsed = {"symbol": "BTCUSDT", "side": "short", "stop": Decimal("62440")}
    fake = _must1_fake(available=Decimal("34"))
    cid, card = create_card_from_typed_command(conn, fake, parsed, now_ms=NOW)
    assert card["size_mode"] == "fit_margin"
    fake.available = Decimal("100")
    sent, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("reason") == "size_changed"
    assert sent[0]["text"] == MSG_SIZE_CHANGED
    conn.close()


def test_flow_qk_rg_replay_idempotent_x(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "x1.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = _btc_fake()
    cid, _ = create_card_from_typed_command(
        conn, fake, {"symbol": "BTCUSDT", "side": "short", "stop": BTC_STOP_SHORT}, now_ms=NOW
    )
    _run_qk(conn, f"qk:rg:{cid}", fake)
    n1 = conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0]
    _run_qk(conn, f"qk:rg:{cid}", fake)
    n2 = conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0]
    assert n1 == 1 and n2 == 1
    conn.close()


def test_flow_qk_rf_after_available_grows_x(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "x2.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    parsed = {"symbol": "BTCUSDT", "side": "short", "stop": Decimal("62440")}
    fake = _must1_fake(available=Decimal("34"))
    cid, card = create_card_from_typed_command(conn, fake, parsed, now_ms=NOW)
    fake.available = Decimal("100")
    _, _, out, edits = _run_qk(conn, f"qk:rf:{cid}", fake)
    assert out.get("ok") is True
    row = conn.execute(
        "SELECT size_mode, quote_qty, registration_id FROM assist_quick_card WHERE id=?", (cid,)
    ).fetchone()
    assert row["size_mode"] in (None, "risk")
    assert Decimal(str(row["quote_qty"])) == Decimal("0.0028")
    assert row["registration_id"] is None
    assert edits
    assert edits[-1]["reply_markup"] == _quoted_markup_for(cid, "risk")
    assert edits[-1]["reply_markup"] != _quoted_markup_for(cid, "fit_margin")
    conn.close()


def test_legacy_qr_margin_below_min_same_side_y(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    from tests.test_assist_quick_card import _send_capture

    conn = open_db(tmp_path / "y.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    card_m = build_quick_card(
        conn,
        _must1_fake(available=Decimal("0.01")),
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62440"),
        now_ms=NOW,
        allow_offer=False,
    )
    assert card_m["reason"] == "margin_cap"
    assert card_m["text"] == (
        "❌ margin ไม่พอ — ขนาดตาม risk 0.0028 BTC ≈ 173.88 USDT "
        "ต้องใช้ ≈ 34.78 USDT ที่ 5x มี 0.01 USDT · 👉 พิมพ์ q ใหม่ บอทจะเสนอขนาดที่ margin พอ"
    )

    write_quick_settings(conn, risk_pct=0.25)
    below_fake = QuickFake(
        balance=Decimal("600"),
        available=Decimal("500"),
        marks={"BTCUSDT": Decimal("100")},
        ticks={"BTCUSDT": Decimal("0.01")},
        steps={"BTCUSDT": Decimal("1")},
        min_qty={"BTCUSDT": Decimal("3")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    card_b = build_quick_card(
        conn,
        below_fake,
        symbol="BTCUSDT",
        side="long",
        stop=Decimal("99.12"),
        now_ms=NOW,
        allow_offer=False,
    )
    assert card_b["reason"] == "below_min_notional"
    assert card_b["text"] == (
        "❌ ไม้เล็กกว่าขั้นต่ำ — ขนาดตาม risk 1 BTC ≈ 100 USDT "
        "ต่ำกว่าขั้นต่ำ 3 BTC / 5 USDT · 👉 พิมพ์ q ใหม่ บอทจะเสนอขนาดขั้นต่ำถ้าความเสี่ยงไม่เกินเพดาน"
    )

    write_quick_settings(conn, risk_pct=0.5, cap_pct=10.0)
    _insert_quick_trade(conn, symbol="ETHUSDT", side="long", now_ms=NOW)
    _insert_quick_trade(conn, symbol="SOLUSDT", side="long", now_ms=NOW)
    before = conn.execute("SELECT COUNT(*) FROM assist_quick_registration WHERE status='open'").fetchone()[0]
    card_cap = build_quick_card(
        conn,
        _happy_fake(),
        symbol="SUIUSDT",
        side="long",
        stop=Decimal("1.994"),
        now_ms=NOW,
        allow_offer=False,
    )
    sent3, send3 = _send_capture()
    out3 = handle_quick_register_callback(
        conn, card_cap["callback_data"], futures=_happy_fake(), send=send3, now_ms=NOW
    )
    assert out3["reason"] == "same_side_cap"
    assert len(sent3) == 1
    assert sent3[0]["text"] == same_side_cap_text("long", 2)
    assert "ลองใหม่" not in "\n".join(
        [card_m["text"], card_b["text"], card_cap["text"], *(m["text"] for m in sent3)]
    )
    after = conn.execute("SELECT COUNT(*) FROM assist_quick_registration WHERE status='open'").fetchone()[0]
    assert after == before
    conn.close()


# --- A4: same-side cap ---------------------------------------------------------


def test_same_side_cap_qk_rg_refused_at_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _sui_card_conn(tmp_path, risk_pct=0.5)
    _insert_quick_trade(conn, symbol="ETHUSDT", side="long", now_ms=NOW)
    _insert_quick_trade(conn, symbol="SOLUSDT", side="long", now_ms=NOW)
    cid, card = _a4_fixture(conn, _happy_fake())
    assert cid and card["ok"]
    sent, answers, out, edits = _run_qk(conn, f"qk:rg:{cid}", _happy_fake())
    assert out.get("reason") == "same_side_cap"
    assert sent[0]["text"] == same_side_cap_text("long", 2)
    assert answers == [None]
    assert edits == []
    row = conn.execute("SELECT status, registration_id FROM assist_quick_card WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "quoted"
    assert row["registration_id"] is None
    conn.close()


def test_same_side_cap_trade_plus_open_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _sui_card_conn(tmp_path, risk_pct=0.5)
    _insert_quick_trade(conn, symbol="ETHUSDT", side="long", now_ms=NOW)
    _insert_quick_registration(conn, symbol="SOLUSDT", side="long", now_ms=NOW)
    cid, _ = _a4_fixture(conn, _happy_fake())
    _, answers, out, _ = _run_qk(conn, f"qk:rg:{cid}", _happy_fake())
    assert out.get("reason") == "same_side_cap"
    assert answers == [None]
    conn.close()


def test_same_side_cap_two_shorts_allow_long(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _sui_card_conn(tmp_path, risk_pct=0.5)
    _insert_quick_trade(conn, symbol="ETHUSDT", side="short", now_ms=NOW)
    _insert_quick_trade(conn, symbol="SOLUSDT", side="short", now_ms=NOW)
    cid, card = _a4_fixture(conn, _happy_fake())
    _, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", _happy_fake())
    assert out.get("ok") is True
    conn.close()


def test_same_side_cap_closed_trade_not_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _sui_card_conn(tmp_path, risk_pct=0.5)
    _insert_quick_trade(conn, symbol="ETHUSDT", side="long", status="closed", now_ms=NOW)
    _insert_quick_trade(conn, symbol="SOLUSDT", side="long", now_ms=NOW)
    cid, _ = _a4_fixture(conn, _happy_fake())
    _, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", _happy_fake())
    assert out.get("ok") is True
    conn.close()


def test_same_side_cap_expired_registration_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _sui_card_conn(tmp_path, risk_pct=0.5)
    _insert_quick_trade(conn, symbol="ETHUSDT", side="long", now_ms=NOW)
    _insert_quick_registration(conn, symbol="SOLUSDT", side="long", status="expired", now_ms=NOW)
    cid, _ = _a4_fixture(conn, _happy_fake())
    _, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", _happy_fake())
    assert out.get("ok") is True
    conn.close()


def test_same_side_cap_tagged_registration_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _sui_card_conn(tmp_path, risk_pct=0.5)
    _insert_quick_trade(conn, symbol="ETHUSDT", side="long", now_ms=NOW)
    _insert_quick_registration(conn, symbol="SOLUSDT", side="long", status="tagged", now_ms=NOW)
    cid, _ = _a4_fixture(conn, _happy_fake())
    _, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", _happy_fake())
    assert out.get("ok") is True
    conn.close()


def test_same_side_cap_two_success_third_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _sui_card_conn(tmp_path, risk_pct=0.5)
    fake = QuickFake(
        balance=Decimal("1000"),
        marks={"SUIUSDT": Decimal("2.0"), "BTCUSDT": Decimal("100000"), "ETHUSDT": Decimal("3000")},
        ticks={"SUIUSDT": Decimal("0.0001"), "BTCUSDT": Decimal("0.1"), "ETHUSDT": Decimal("0.01")},
        steps={"SUIUSDT": Decimal("0.1"), "BTCUSDT": Decimal("0.001"), "ETHUSDT": Decimal("0.001")},
        min_qty={"SUIUSDT": Decimal("0.1"), "BTCUSDT": Decimal("0.001"), "ETHUSDT": Decimal("0.001")},
        min_notional={"SUIUSDT": Decimal("5"), "BTCUSDT": Decimal("5"), "ETHUSDT": Decimal("5")},
    )
    for sym, stop in (("SUIUSDT", Decimal("1.994")), ("ETHUSDT", Decimal("2950"))):
        cid, card = create_card_from_typed_command(
            conn, fake, {"symbol": sym, "side": "long", "stop": stop}, now_ms=NOW
        )
        assert card["ok"]
        out = _run_qk(conn, f"qk:rg:{cid}", fake)[2]
        assert out.get("ok") is True
    cid3, card3 = create_card_from_typed_command(
        conn, fake, {"symbol": "BTCUSDT", "side": "long", "stop": Decimal("99000")}, now_ms=NOW
    )
    assert card3["ok"]
    _, _, out3, _ = _run_qk(conn, f"qk:rg:{cid3}", fake)
    assert out3.get("reason") == "same_side_cap"
    conn.close()


def test_same_side_cap_uppercase_side_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = _sui_card_conn(tmp_path, risk_pct=0.5)
    tid = _insert_quick_trade(conn, symbol="BTCUSDT", side="long", now_ms=NOW)
    conn.execute("UPDATE assist_quick_trade SET side='LONG' WHERE id=?", (tid,))
    conn.commit()
    _insert_quick_registration(conn, symbol="ETHUSDT", side="long", now_ms=NOW)
    cid, _ = _a4_fixture(conn, _happy_fake())
    _, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", _happy_fake())
    assert out.get("reason") == "same_side_cap"
    conn.close()


# --- A5: safety ----------------------------------------------------------------


def test_callback_data_bytes_offer_and_refresh() -> None:
    cid = "abcd1234"
    for mode in ("risk", "min_lot", "fit_margin"):
        m = _quoted_markup_for(cid, mode)
        for row in m["inline_keyboard"]:
            for btn in row:
                assert len(btn["callback_data"].encode("utf-8")) <= 64
    long_cid = "c" * 40
    rf = _refresh_only_markup(long_cid)
    for row in rf["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64
    sym_cid = _quoted_markup_for("a" * 52, "fit_margin")
    for row in sym_cid["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_refusal_and_card_texts_never_say_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "ลองใหม่" not in REFUSAL["read_failed"]
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "th.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    card = build_quick_card(
        conn,
        QuickFake(
            balance=Decimal("142"),
            available=Decimal("142"),
            marks={"BTCUSDT": Decimal("59330.1")},
            ticks={"BTCUSDT": Decimal("0.1")},
            steps={"BTCUSDT": Decimal("0.001")},
            min_qty={"BTCUSDT": Decimal("0.001")},
            min_notional={"BTCUSDT": Decimal("5")},
        ),
        symbol="BTCUSDT",
        side="short",
        stop=Decimal("62000"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert "ลองใหม่" not in card["text"]
    conn.close()


def test_quick_fake_no_order_or_leverage_methods() -> None:
    fake = _happy_fake()
    for name in (
        "new_order",
        "cancel_order",
        "set_leverage",
        "change_margin_type",
        "change_position_mode",
        "post",
        "delete",
    ):
        assert not hasattr(fake, name)
    assert "new_order" not in QuickFake.ALLOWED_READS


def test_quick_modules_ast_safety() -> None:
    _assert_module_forbids(SRC / "quick_minlot.py")
    _assert_module_forbids(SRC / "quick_cap.py")


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ("from jarvise_assist.execute import x", "jarvise_assist.execute"),
        ("from .execute import x", "jarvise_assist.execute"),
        ("from . import execute", "jarvise_assist.execute"),
        ("from jarvise_assist import execute", "jarvise_assist.execute"),
        ("from jarvise_assist import messages as m", "jarvise_assist.messages"),
        ("from .futures import UMFutures", "jarvise_assist.futures"),
        ("from .. import jarvise_trade", "jarvise_trade"),
        ("from ..jarvise_trade.x import y", "jarvise_trade"),
        ("from jarvise_notify import quiet", "jarvise_notify.quiet"),
        ("import redis", "redis"),
        ("from ... import x", "level >= 3"),
    ],
)
def test_quick_modules_ast_negative_absolute_and_relative(tmp_path: Path, source: str, match: str) -> None:
    path = tmp_path / "bad.py"
    path.write_text(source + "\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=match):
        _assert_module_forbids(path)


@pytest.mark.parametrize(
    "source",
    [
        "from jarvise_assist.sizing import floor_to_step",
        "from . import sizing",
        "from .sizing import floor_to_step",
        "from jarvise_assist import sizing, quick_cap",
        "from ..jarvise_assist import sizing",
        "from decimal import Decimal",
        "from typing import Any",
        "from __future__ import annotations",
    ],
)
def test_quick_modules_ast_allows_safe_imports(tmp_path: Path, source: str) -> None:
    path = tmp_path / "ok.py"
    path.write_text(source + "\n", encoding="utf-8")
    _assert_module_forbids(path)


def test_registration_refusal_same_side_cap_text() -> None:
    assert (
        _registration_refusal_text("same_side_cap")
        == "ไม่ลงทะเบียน — ฝั่งนี้เปิดอยู่ครบเพดานแล้ว (เพดานฝั่งละ 2) · ปิดหรือให้ทะเบียนหมดอายุก่อน"
    )


def test_same_side_cap_helpers() -> None:
    assert same_side_cap_reached(2)
    assert not same_side_cap_reached(1)
    assert "2" in same_side_cap_text("long", 2)


# --- A-T: bound risk guard (passes at d06a0e4) ---------------------------------


def test_card_bound_risk_guard_head_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "at.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = _must1_fake(available=Decimal("34"))
    kwargs = dict(
        mark=Decimal("62100"),
        stop=Decimal("62440"),
        wallet_balance=Decimal("120"),
        available=Decimal("34"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("100"),
        step=Decimal("0.0001"),
        risk_qty=Decimal("0.0028"),
    )
    real = size_offer(**kwargs)

    def stub(**kw: Any) -> SizeOfferResult:
        r = size_offer(**{**kwargs, **kw})
        assert r.offer is not None
        return dataclasses.replace(r, offer=dataclasses.replace(r.offer, bound="risk"))

    monkeypatch.setattr("jarvise_assist.quick_card.size_offer", stub)
    card = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("62440"), now_ms=NOW, allow_offer=True
    )
    assert card["ok"] is True
    assert "ขนาดเต็มตาม risk · ใช้ margin กันไว้ 2% (ระบบหลักกัน 5%)" in card["text"]
    assert "(ขนาดเต็มตาม risk)" in card["text"]
    assert "⚠️ margin ไม่พอสำหรับขนาดเต็ม" not in card["text"]
    assert real.offer is not None
    conn.close()


def test_typed_offer_register_qty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "reg.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0)
    fake = _btc_fake()
    cid, card = create_card_from_typed_command(
        conn, fake, {"symbol": "BTCUSDT", "side": "short", "stop": BTC_STOP_SHORT}, now_ms=NOW
    )
    assert card["size_mode"] == "fit_margin"
    markup = _quoted_markup_for(cid, card["size_mode"])
    assert markup["inline_keyboard"][0][0]["text"] == "⚡ ยืนยันขนาดที่เสนอ"
    _, _, out, _ = _run_qk(conn, f"qk:rg:{cid}", fake)
    assert out.get("ok") is True
    reg = conn.execute(
        """
        SELECT r.qty, c.size_mode FROM assist_quick_registration r
        JOIN assist_quick_card c ON c.registration_id = r.id WHERE c.id=?
        """,
        (cid,),
    ).fetchone()
    assert Decimal(str(reg["qty"])) == card["qty"]
    conn.close()


# --- A-T: Step B tidies ---------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(margin_buffer=Decimal("0")),
        dict(max_leverage=0),
        dict(min_qty=Decimal("-1")),
        dict(min_notional=Decimal("-1")),
    ],
)
def test_size_offer_at_bad_config_returns_bad_input(kwargs: dict[str, Any]) -> None:
    base = dict(
        mark=BTC_MARK,
        stop=BTC_STOP_SHORT,
        wallet_balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        min_qty=BTC_MIN_QTY,
        min_notional=BTC_MIN_NOTIONAL,
        step=BTC_STEP,
        risk_qty=BTC_RISK_QTY,
    )
    base.update(kwargs)
    assert size_offer(**base).reason == "bad_input"


def test_size_offer_min_qty_zero_today_like_offer() -> None:
    r = size_offer(
        mark=BTC_MARK,
        stop=BTC_STOP_SHORT,
        wallet_balance=BTC_WALLET,
        available=BTC_AVAILABLE,
        min_qty=Decimal("0"),
        min_notional=Decimal("5"),
        step=BTC_STEP,
        risk_qty=BTC_RISK_QTY,
    )
    assert r.reason is None
    assert r.offer is not None
    assert r.offer.qty == Decimal("0.003")


def test_size_offer_both_minimums_zero_bad_input() -> None:
    for risk_qty in (Decimal("0"), BTC_RISK_QTY):
        r = size_offer(
            mark=BTC_MARK,
            stop=BTC_STOP_SHORT,
            wallet_balance=BTC_WALLET,
            available=BTC_AVAILABLE,
            min_qty=Decimal("0"),
            min_notional=Decimal("0"),
            step=BTC_STEP,
            risk_qty=risk_qty,
        )
        assert r.reason == "bad_input"


def test_size_offer_min_notional_zero_leverage_boundary() -> None:
    r = size_offer(
        mark=Decimal("100"),
        stop=Decimal("99"),
        wallet_balance=Decimal("1000"),
        available=Decimal("20"),
        min_qty=Decimal("1"),
        min_notional=Decimal("0"),
        step=Decimal("1"),
        risk_qty=Decimal("0"),
    )
    assert r.reason == "leverage_over_5x"
    assert r.candidate is not None
    assert r.candidate.leverage == 6


def test_format_size_offer_refusal_unknown_reason_never_raw_code() -> None:
    cand = SizeOffer(
        qty=Decimal("1"),
        notional=Decimal("100"),
        risk_usdt=Decimal("1"),
        fee_usdt=Decimal("0.1"),
        real_risk_pct=Decimal("1.1"),
        leverage=5,
        margin=Decimal("20"),
        kind="min_lot",
        bound="min_lot",
    )
    with_candidate = SizeOfferResult(
        offer=None,
        reason="unknown_reason_xyz",
        candidate=cand,
        q_min=Decimal("1"),
        q_fit=None,
        q_1pct=Decimal("1"),
        need_margin=Decimal("25"),
    )
    assert (
        _format_size_offer_refusal(
            with_candidate,
            mark=Decimal("100"),
            stop=Decimal("101"),
            wallet_balance=Decimal("100"),
            available=Decimal("50"),
            base="BTC",
            tick=Decimal("0.1"),
            cap_pct=Decimal("1"),
        )
        == REFUSAL["size_failed"]
    )
    no_cand = SizeOfferResult(
        offer=None,
        reason="unknown_reason_xyz",
        candidate=None,
        q_min=Decimal("1"),
        q_fit=None,
        q_1pct=Decimal("1"),
        need_margin=None,
    )
    assert (
        _format_size_offer_refusal(
            no_cand,
            mark=Decimal("100"),
            stop=Decimal("101"),
            wallet_balance=Decimal("100"),
            available=Decimal("50"),
            base="BTC",
            tick=Decimal("0.1"),
            cap_pct=Decimal("1"),
        )
        == REFUSAL["size_failed"]
    )


def test_card_sui_offer_respects_cap_pct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "cap_sui.db")
    _seed_settings(conn)
    write_quick_settings(conn, risk_pct=1.0, cap_pct=0.5)
    c_half = build_quick_card(
        conn,
        _sui_fake(Decimal("100")),
        symbol="SUIUSDT",
        side="short",
        stop=Decimal("10.2004"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert c_half["ok"] is True
    assert c_half["size_mode"] == "fit_margin"
    assert c_half["qty"] == Decimal("23.5")
    half_text = c_half["text"]
    assert "ขนาดที่เสนอ: 23.5 SUI ≈ 235 USDT (ใหญ่สุดที่ risk จริงรวมค่าธรรมเนียม ≤ 0.5%)" in half_text
    assert "risk จริง: −4.99 USDT = 0.50% ของ Futures 1000 USDT (เป้า 1% · เพดาน 0.5%)" in half_text
    assert "≤ 1%)" not in half_text
    assert "เพดาน 1%" not in half_text
    assert "ลองใหม่" not in half_text
    assert "ไม้นี้ 0.47% (ไม่รวมค่าธรรมเนียม)" in half_text

    write_quick_settings(conn, cap_pct=2.0)
    c_double = build_quick_card(
        conn,
        _sui_fake(Decimal("100")),
        symbol="SUIUSDT",
        side="short",
        stop=Decimal("10.2004"),
        now_ms=NOW,
        allow_offer=True,
    )
    assert c_double["ok"] is True
    # Fee-aware 1% risk fits margin at available=100 → plain risk mode.
    assert c_double["size_mode"] == "risk"
    assert c_double["qty"] == Decimal("47.0")
    double_text = c_double["text"]
    assert "risk จริง: −9.98 USDT = 1.00% ของ Futures 1000 USDT (เป้า 1%)" in double_text
    assert "เพดาน quick 2%" in double_text
    assert "ลองใหม่" not in double_text
    conn.close()


BUG3_CAP15 = (
    "❌ ไม่เปิด — ขั้นต่ำ 0.001 BTC เสี่ยง 2.74 USDT (1.93%) "
    "(ราคา 2.67 + ค่าธรรมเนียม 0.07) เกินเพดาน 2.13 USDT (1.5% ของ wallet 142) "
    "· stop ห่าง 4.50% กว้างเกินไปสำหรับ Quick "
    "· 👉 ไม้ขั้นต่ำรับ stop ได้ไกลสุด ≈ 3.47% (61388.9)"
)


def test_card_bug3_cap_pct_refusal_and_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_trade_client(monkeypatch)
    conn = open_db(tmp_path / "bug3_cap.db")
    _seed_settings(conn)
    fake = QuickFake(
        balance=Decimal("142"),
        available=Decimal("142"),
        marks={"BTCUSDT": Decimal("59330.1")},
        ticks={"BTCUSDT": Decimal("0.1")},
        steps={"BTCUSDT": Decimal("0.001")},
        min_qty={"BTCUSDT": Decimal("0.001")},
        min_notional={"BTCUSDT": Decimal("5")},
    )
    write_quick_settings(conn, risk_pct=1.0, cap_pct=1.5)
    refused = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("62000"), now_ms=NOW, allow_offer=True
    )
    assert refused["ok"] is False
    assert refused["reason"] == "risk_over_1pct"
    assert refused["text"] == BUG3_CAP15
    assert "ลองใหม่" not in refused["text"]

    write_quick_settings(conn, cap_pct=2.0)
    opened = build_quick_card(
        conn, fake, symbol="BTCUSDT", side="short", stop=Decimal("62000"), now_ms=NOW, allow_offer=True
    )
    assert opened["ok"] is True
    assert opened["size_mode"] == "min_lot"
    assert opened["qty"] == Decimal("0.001")
    assert "risk จริง: −2.74 USDT = 1.93% ของ Futures 142 USDT (เป้า 1% · เพดาน 2%)" in opened["text"]
    assert "ลองใหม่" not in opened["text"]
    conn.close()
