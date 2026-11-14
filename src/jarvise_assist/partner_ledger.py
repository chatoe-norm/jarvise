"""Pure unit/NAV partner-ledger math. Decimal only; no I/O.

Account value ``V`` (USDT) is a function argument. Callers that withdraw or
close pass ``V`` *before* this event's cash leaves the wallet (admin confirms,
then transfers).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, getcontext

getcontext().prec = max(getcontext().prec, 28)

MICRO = Decimal("0.000001")
UNIT = Decimal("0.00000001")
CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class Refusal:
    reason: str


@dataclass(frozen=True)
class PartnerValue:
    units: Decimal
    cost: Decimal
    fee_rate: Decimal
    nav: Decimal
    gross: Decimal
    profit: Decimal
    share: Decimal
    fee: Decimal
    net: Decimal
    state: str  # up | down | flat


@dataclass(frozen=True)
class DepositPlan:
    amount: Decimal
    nav_pre: Decimal
    units_issued: Decimal
    units_after: Decimal
    cost_after: Decimal


@dataclass(frozen=True)
class ConvertPlan:
    nav: Decimal
    gross: Decimal
    profit: Decimal
    share: Decimal
    fee: Decimal
    fee_units: Decimal
    units_after: Decimal
    cost_after: Decimal


@dataclass(frozen=True)
class WithdrawPlan:
    nav: Decimal
    gross: Decimal
    profit: Decimal
    share: Decimal
    fee: Decimal
    fee_units: Decimal
    cash: Decimal
    redeemed_units: Decimal
    units_after: Decimal
    cost_after: Decimal


@dataclass(frozen=True)
class ClosePlan:
    nav: Decimal
    gross: Decimal
    profit: Decimal
    share: Decimal
    fee: Decimal
    fee_units: Decimal
    cash: Decimal
    units_after: Decimal
    cost_after: Decimal


@dataclass(frozen=True)
class AdjustPlan:
    units_after: Decimal
    cost_after: Decimal
    cost_delta: Decimal


def _q_down(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _q_up(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_UP)


def nav(account_value: Decimal, total_units: Decimal) -> Decimal | Refusal:
    """NAV(V, U) = V/U; NAV = 1 when U == 0. Refuse when V <= 0 with U > 0 or NAV <= 0."""
    v = Decimal(account_value)
    u = Decimal(total_units)
    if u == ZERO:
        return ONE
    if v <= ZERO:
        return Refusal("nav_not_positive")
    n = v / u
    if n <= ZERO:
        return Refusal("nav_not_positive")
    return n


def value_of(
    *,
    units: Decimal,
    cost: Decimal,
    fee_rate: Decimal,
    nav: Decimal,
) -> PartnerValue:
    """Partner mark-to-market view at a given NAV."""
    n = Decimal(nav)
    u = Decimal(units)
    c = Decimal(cost)
    fr = Decimal(fee_rate)
    gross = _q_down(u * n, MICRO)
    profit = gross - c
    if profit > ZERO:
        share = _q_down(profit * (ONE - fr), MICRO)
        fee = profit - share
        net = c + share
    else:
        share = ZERO
        fee = ZERO
        net = gross
    if net > c:
        state = "up"
    elif net < c:
        state = "down"
    else:
        state = "flat"
    return PartnerValue(
        units=u,
        cost=c,
        fee_rate=fr,
        nav=n,
        gross=gross,
        profit=profit,
        share=share,
        fee=fee,
        net=net,
        state=state,
    )


def plan_deposit(
    *,
    amount: Decimal,
    account_value_incl: Decimal,
    total_units: Decimal,
    cost: Decimal,
    units: Decimal = ZERO,
) -> DepositPlan | Refusal:
    """Deposit ``amount`` already in the wallet; ``account_value_incl`` includes it."""
    d = Decimal(amount)
    if d <= ZERO:
        return Refusal("nav_not_positive")
    v = Decimal(account_value_incl)
    u_total = Decimal(total_units)
    c = Decimal(cost)
    u_partner = Decimal(units)
    nav_pre = nav(v - d, u_total)
    if isinstance(nav_pre, Refusal):
        return nav_pre
    units_issued = _q_down(d / nav_pre, UNIT)
    return DepositPlan(
        amount=d,
        nav_pre=nav_pre,
        units_issued=units_issued,
        units_after=u_partner + units_issued,
        cost_after=c + d,
    )


def plan_convert(
    *,
    units: Decimal,
    cost: Decimal,
    fee_rate: Decimal,
    nav: Decimal,
) -> ConvertPlan | Refusal:
    """Move fee units to admin; partner cost becomes net. U and V unchanged; no cash."""
    n = Decimal(nav)
    if n <= ZERO:
        return Refusal("nav_not_positive")
    view = value_of(units=units, cost=cost, fee_rate=fee_rate, nav=n)
    if view.profit <= ZERO:
        return Refusal("no_profit")
    fee_units = _q_down(view.fee / n, UNIT)
    return ConvertPlan(
        nav=n,
        gross=view.gross,
        profit=view.profit,
        share=view.share,
        fee=view.fee,
        fee_units=fee_units,
        units_after=Decimal(units) - fee_units,
        cost_after=view.net,
    )


def plan_withdraw_profit(
    *,
    units: Decimal,
    cost: Decimal,
    fee_rate: Decimal,
    nav: Decimal,
) -> WithdrawPlan | Refusal:
    """Cash out partner share of profit. Pass V before cash leaves the wallet."""
    n = Decimal(nav)
    if n <= ZERO:
        return Refusal("nav_not_positive")
    view = value_of(units=units, cost=cost, fee_rate=fee_rate, nav=n)
    if view.profit <= ZERO:
        return Refusal("no_profit")
    cash = _q_down(view.share, CENT)
    if cash < CENT:
        return Refusal("below_min")
    fee_units = _q_down(view.fee / n, UNIT)
    redeemed_units = _q_up(cash / n, UNIT)
    return WithdrawPlan(
        nav=n,
        gross=view.gross,
        profit=view.profit,
        share=view.share,
        fee=view.fee,
        fee_units=fee_units,
        cash=cash,
        redeemed_units=redeemed_units,
        units_after=Decimal(units) - fee_units - redeemed_units,
        cost_after=Decimal(cost),
    )


def plan_close(
    *,
    units: Decimal,
    cost: Decimal,
    fee_rate: Decimal,
    nav: Decimal,
) -> ClosePlan | Refusal:
    """Redeem all units. Pass V before cash leaves the wallet."""
    n = Decimal(nav)
    if n <= ZERO:
        return Refusal("nav_not_positive")
    view = value_of(units=units, cost=cost, fee_rate=fee_rate, nav=n)
    if view.profit > ZERO:
        fee_units = _q_down(view.fee / n, UNIT)
        cash = _q_down(Decimal(cost) + view.share, CENT)
        fee = view.fee
        share = view.share
    else:
        fee_units = ZERO
        cash = _q_down(view.gross, CENT)
        fee = ZERO
        share = ZERO
    return ClosePlan(
        nav=n,
        gross=view.gross,
        profit=view.profit,
        share=share,
        fee=fee,
        fee_units=fee_units,
        cash=cash,
        units_after=ZERO,
        cost_after=ZERO,
    )


def plan_adjust_cost(
    *,
    units: Decimal,
    cost: Decimal,
    new_cost: Decimal,
    reason: str,
) -> AdjustPlan | Refusal:
    """Admin sets cost basis; units unchanged. Reason must be at least 10 characters."""
    if len(reason) < 10:
        return Refusal("reason_short")
    nc = Decimal(new_cost)
    if nc < ZERO:
        return Refusal("nav_not_positive")
    u = Decimal(units)
    c = Decimal(cost)
    return AdjustPlan(units_after=u, cost_after=nc, cost_delta=nc - c)
