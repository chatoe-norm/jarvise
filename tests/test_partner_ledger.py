"""Pure unit/NAV math for the partner ledger (no I/O)."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

import pytest

from jarvise_assist.partner_ledger import (
    AdjustPlan,
    ClosePlan,
    ConvertPlan,
    DepositPlan,
    PartnerValue,
    Refusal,
    WithdrawPlan,
    nav,
    plan_adjust_cost,
    plan_close,
    plan_convert,
    plan_deposit,
    plan_withdraw_profit,
    value_of,
)

D = Decimal
MICRO = D("0.000001")
UNIT = D("0.00000001")
CENT = D("0.01")


def test_nav_empty_fund_is_one() -> None:
    assert nav(D("0"), D("0")) == D("1")
    assert nav(D("100"), D("0")) == D("1")


def test_nav_divides_and_refuses_non_positive() -> None:
    assert nav(D("1200"), D("1000")) == D("1.2")
    assert isinstance(nav(D("0"), D("100")), Refusal)
    assert nav(D("0"), D("100")).reason == "nav_not_positive"  # type: ignore[union-attr]
    assert isinstance(nav(D("-1"), D("10")), Refusal)
    assert nav(D("-1"), D("10")).reason == "nav_not_positive"  # type: ignore[union-attr]


def test_value_of_profit_split_and_states() -> None:
    # fee 50%, cost 100, units*NAV = 120
    v = value_of(units=D("100"), cost=D("100"), fee_rate=D("0.5"), nav=D("1.2"))
    assert isinstance(v, PartnerValue)
    assert v.gross == D("120")
    assert v.profit == D("20")
    assert v.share == D("10")
    assert v.fee == D("10")
    assert v.net == D("110")
    assert v.state == "up"

    flat = value_of(units=D("500"), cost=D("600"), fee_rate=D("0.5"), nav=D("1.2"))
    assert flat.gross == D("600")
    assert flat.profit == D("0")
    assert flat.fee == D("0")
    assert flat.net == D("600")
    assert flat.state == "flat"

    down = value_of(units=D("500"), cost=D("600"), fee_rate=D("0.5"), nav=D("1"))
    assert down.gross == D("500")
    assert down.fee == D("0")
    assert down.net == D("500")
    assert down.state == "down"


def test_value_of_quantizes_gross_and_share_down_to_micro() -> None:
    # 1/3 * 1 = 0.333... → gross DOWN to 0.000001
    v = value_of(units=D("1") / D("3"), cost=D("0"), fee_rate=D("0.5"), nav=D("1"))
    assert v.gross == D("0.333333")
    # profit = 0.333333; share = 0.1666665 → DOWN 0.166666
    assert v.share == D("0.166666")
    assert v.fee == v.profit - v.share
    assert v.fee + v.share == v.profit


def test_admin_fee_rate_zero_never_pays_fee() -> None:
    v = value_of(units=D("100"), cost=D("100"), fee_rate=D("0"), nav=D("1.2"))
    assert v.gross == D("120")
    assert v.share == D("20")
    assert v.fee == D("0")
    assert v.net == D("120")


def test_plan_deposit_issues_units_down_to_e8() -> None:
    # First deposit into empty fund
    p = plan_deposit(amount=D("1000"), account_value_incl=D("1000"), total_units=D("0"), cost=D("0"))
    assert isinstance(p, DepositPlan)
    assert p.nav_pre == D("1")
    assert p.units_issued == D("1000")
    assert p.cost_after == D("1000")
    assert p.units_after == D("1000")

    # B deposits 600 when V after = 1800, U = 1000 → NAV_pre = 1.2 → 500 units
    p2 = plan_deposit(amount=D("600"), account_value_incl=D("1800"), total_units=D("1000"), cost=D("0"))
    assert isinstance(p2, DepositPlan)
    assert p2.nav_pre == D("1.2")
    assert p2.units_issued == D("500")
    assert p2.cost_after == D("600")

    # Rounding: d / NAV_pre DOWN to 1e-8
    p3 = plan_deposit(amount=D("1"), account_value_incl=D("4"), total_units=D("3"), cost=D("0"))
    assert isinstance(p3, DepositPlan)
    # NAV_pre = 3/3 = 1; 1/1 = 1 exact
    assert p3.units_issued == D("1")
    # NAV_pre = 10/3, amount 1 → units = 0.3 DOWN? 1 / (10/3) = 0.3
    p4 = plan_deposit(amount=D("1"), account_value_incl=D("11"), total_units=D("3"), cost=D("0"))
    assert isinstance(p4, DepositPlan)
    assert p4.nav_pre == D("10") / D("3")
    assert p4.units_issued == (D("1") / p4.nav_pre).quantize(UNIT, rounding=ROUND_DOWN)


def test_plan_deposit_refuses_bad_nav() -> None:
    r = plan_deposit(amount=D("10"), account_value_incl=D("5"), total_units=D("100"), cost=D("0"))
    # V_pre = -5 with U>0
    assert isinstance(r, Refusal)
    assert r.reason == "nav_not_positive"


def test_owner_example_convert() -> None:
    # fee 50%, cost 100, gross 120 → convert: cost 110, admin +10 USDT of units
    # NAV=1 keeps fee_units exact (10), matching the owner worked example.
    units = D("120")
    nav_v = D("1")
    p = plan_convert(units=units, cost=D("100"), fee_rate=D("0.5"), nav=nav_v)
    assert isinstance(p, ConvertPlan)
    assert p.fee == D("10")
    assert p.fee_units == D("10")
    assert p.cost_after == D("110")
    assert p.units_after == D("110")


def test_owner_example_withdraw() -> None:
    units = D("120")
    nav_v = D("1")
    p = plan_withdraw_profit(units=units, cost=D("100"), fee_rate=D("0.5"), nav=nav_v)
    assert isinstance(p, WithdrawPlan)
    assert p.cash == D("10")
    assert p.fee == D("10")
    assert p.cost_after == D("100")  # unchanged
    assert p.fee_units == D("10")
    assert p.redeemed_units == D("10")
    assert p.units_after == D("100")
    gross_after = (p.units_after * nav_v).quantize(MICRO, rounding=ROUND_DOWN)
    assert gross_after == D("100")


def test_owner_example_close_with_profit() -> None:
    units = D("120")
    nav_v = D("1")
    p = plan_close(units=units, cost=D("100"), fee_rate=D("0.5"), nav=nav_v)
    assert isinstance(p, ClosePlan)
    assert p.cash == D("110")
    assert p.fee == D("10")
    assert p.fee_units == D("10")
    assert p.units_after == D("0")
    assert p.cost_after == D("0")


def test_close_loss_no_fee_and_convert_withdraw_refuse() -> None:
    units = D("90")
    nav_v = D("1")
    # cost 100, gross 90
    c = plan_close(units=units, cost=D("100"), fee_rate=D("0.5"), nav=nav_v)
    assert isinstance(c, ClosePlan)
    assert c.cash == D("90")
    assert c.fee == D("0")
    assert c.fee_units == D("0")
    assert c.units_after == D("0")

    r1 = plan_convert(units=units, cost=D("100"), fee_rate=D("0.5"), nav=nav_v)
    assert isinstance(r1, Refusal) and r1.reason == "no_profit"
    r2 = plan_withdraw_profit(units=units, cost=D("100"), fee_rate=D("0.5"), nav=nav_v)
    assert isinstance(r2, Refusal) and r2.reason == "no_profit"


def test_withdraw_below_min() -> None:
    # tiny profit so share DOWN to 0.01 fails
    # units=1, nav=1.005, cost=1 → gross=1.005, profit=0.005, share at 50%=0.0025 → cash DOWN 0.00
    r = plan_withdraw_profit(units=D("1"), cost=D("1"), fee_rate=D("0.5"), nav=D("1.005"))
    assert isinstance(r, Refusal)
    assert r.reason == "below_min"


def test_withdraw_redeemed_units_round_up() -> None:
    # cash / NAV with UP
    p = plan_withdraw_profit(units=D("100"), cost=D("100"), fee_rate=D("0.5"), nav=D("1.2"))
    assert isinstance(p, WithdrawPlan)
    assert p.redeemed_units == (p.cash / D("1.2")).quantize(UNIT, rounding=ROUND_UP)


def test_plan_adjust_cost() -> None:
    p = plan_adjust_cost(units=D("10"), cost=D("50"), new_cost=D("40"), reason="admin correction ok")
    assert isinstance(p, AdjustPlan)
    assert p.cost_after == D("40")
    assert p.units_after == D("10")

    r = plan_adjust_cost(units=D("10"), cost=D("50"), new_cost=D("40"), reason="short")
    assert isinstance(r, Refusal) and r.reason == "reason_short"

    r2 = plan_adjust_cost(units=D("10"), cost=D("50"), new_cost=D("-1"), reason="admin correction ok")
    assert isinstance(r2, Refusal)


def test_nav_unchanged_for_others_after_deposit() -> None:
    # A has 1000 units, V=1200, NAV=1.2. B deposits 600, V becomes 1800.
    nav_before = nav(D("1200"), D("1000"))
    assert nav_before == D("1.2")
    p = plan_deposit(amount=D("600"), account_value_incl=D("1800"), total_units=D("1000"), cost=D("0"))
    assert isinstance(p, DepositPlan)
    u_after = D("1000") + p.units_issued
    nav_after = nav(D("1800"), u_after)
    assert isinstance(nav_after, Decimal)
    assert abs(nav_after - D("1.2")) <= MICRO


def test_nav_unchanged_for_others_after_close() -> None:
    # U=1800, V=1800, NAV=1. Close B's 500 units for cash 500 → V_after=1300, U=1300
    nav_before = nav(D("1800"), D("1800"))
    assert nav_before == D("1")
    c = plan_close(units=D("500"), cost=D("600"), fee_rate=D("0.5"), nav=D("1"))
    assert isinstance(c, ClosePlan)
    u_after = D("1800") - D("500")
    v_after = D("1800") - c.cash
    nav_after = nav(v_after, u_after)
    assert nav_after == D("1")
    assert abs(nav_after - nav_before) <= MICRO  # type: ignore[operator]


@pytest.mark.parametrize(
    "fn,kwargs,reason",
    [
        (
            plan_convert,
            {"units": D("10"), "cost": D("10"), "fee_rate": D("0.5"), "nav": D("0.9")},
            "no_profit",
        ),
        (
            plan_withdraw_profit,
            {"units": D("10"), "cost": D("10"), "fee_rate": D("0.5"), "nav": D("0.9")},
            "no_profit",
        ),
    ],
)
def test_refusal_codes_parametrized(fn, kwargs, reason) -> None:  # type: ignore[no-untyped-def]
    r = fn(**kwargs)
    assert isinstance(r, Refusal)
    assert r.reason == reason
