"""Pure Quick min-lot / fit-margin offer math (no I/O, no orders)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, ROUND_UP, Decimal

from jarvise_assist.sizing import floor_to_step, round_trip_fee_rate


@dataclass(frozen=True)
class SizeOffer:
    qty: Decimal
    notional: Decimal
    risk_usdt: Decimal
    fee_usdt: Decimal
    real_risk_pct: Decimal
    leverage: int
    margin: Decimal
    kind: str  # "min_lot" | "fit_margin"
    bound: str  # fit_margin: margin|risk_1pct|risk; min_lot: min_lot


@dataclass(frozen=True)
class SizeOfferResult:
    offer: SizeOffer | None
    reason: str | None
    candidate: SizeOffer | None
    q_min: Decimal | None
    q_fit: Decimal | None
    q_1pct: Decimal | None
    need_margin: Decimal | None


def ceil_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step size must be positive")
    steps = (qty / step).to_integral_value(rounding=ROUND_UP)
    return steps * step


def _decimal_ceil_ratio(num: Decimal, denom: Decimal) -> int:
    if denom <= 0:
        return 1
    lev = (num / denom).to_integral_value(rounding=ROUND_CEILING)
    return max(1, int(lev))


def _bad_input() -> SizeOfferResult:
    return SizeOfferResult(
        offer=None,
        reason="bad_input",
        candidate=None,
        q_min=None,
        q_fit=None,
        q_1pct=None,
        need_margin=None,
    )


def _no_balance() -> SizeOfferResult:
    return SizeOfferResult(
        offer=None,
        reason="no_balance",
        candidate=None,
        q_min=None,
        q_fit=None,
        q_1pct=None,
        need_margin=None,
    )


def _finite_decimals(
    *values: Decimal,
) -> bool:
    for v in values:
        if not isinstance(v, Decimal):
            return False
        if not v.is_finite():
            return False
    return True


def fmt_pct(value: Decimal, *, mode: str = "half_up", limit: Decimal | None = None) -> str:
    if mode == "half_up":
        q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{q:.2f}"
    if mode == "failed_limit":
        if limit is None:
            raise ValueError("limit required for failed_limit")
        half = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if half <= limit and value > limit:
            q = value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        else:
            q = half
        return f"{q:.2f}"
    if mode == "floor":
        q = value.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        return f"{q:.2f}"
    raise ValueError(f"unknown fmt_pct mode: {mode}")


def _round_price_to_tick(value: Decimal, tick: Decimal, *, short: bool) -> Decimal:
    if tick <= 0:
        return value
    if short:
        # short stop above mark: floor toward mark
        steps = (value / tick).to_integral_value(rounding=ROUND_FLOOR)
    else:
        # long stop below mark: ceil toward mark
        steps = (value / tick).to_integral_value(rounding=ROUND_CEILING)
    return steps * tick


def min_lot_stop_max(
    *,
    candidate: SizeOffer,
    mark: Decimal,
    stop: Decimal,
    wallet_balance: Decimal,
    tick: Decimal,
    max_real_risk_pct: Decimal = Decimal("1"),
) -> tuple[Decimal, Decimal, Decimal | None]:
    cap_usdt = wallet_balance * max_real_risk_pct / Decimal("100")
    stop_max_dist = (cap_usdt - candidate.fee_usdt) / candidate.qty
    stop_max_pct = stop_max_dist / mark * Decimal("100")
    if stop_max_dist <= 0:
        return stop_max_dist, stop_max_pct, None
    short = stop > mark
    if short:
        raw_price = mark + stop_max_dist
    else:
        raw_price = mark - stop_max_dist
    stop_max_price = _round_price_to_tick(raw_price, tick, short=short)
    return stop_max_dist, stop_max_pct, stop_max_price


def size_offer(
    *,
    mark: Decimal,
    stop: Decimal,
    wallet_balance: Decimal,
    available: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    step: Decimal,
    risk_qty: Decimal,
    fee_bps_per_side: Decimal = Decimal("6"),
    max_real_risk_pct: Decimal = Decimal("1"),
    max_leverage: int = 5,
    margin_buffer: Decimal = Decimal("0.98"),
) -> SizeOfferResult:
    dec_params = (
        mark,
        stop,
        wallet_balance,
        available,
        min_qty,
        min_notional,
        step,
        risk_qty,
        fee_bps_per_side,
        max_real_risk_pct,
        margin_buffer,
    )
    if not _finite_decimals(*dec_params):
        return _bad_input()
    if (
        mark <= 0
        or step <= 0
        or stop == mark
        or risk_qty < 0
        or min_qty < 0
        or min_notional < 0
        or margin_buffer <= 0
        or max_leverage <= 0
    ):
        return _bad_input()
    if wallet_balance <= 0:
        return _no_balance()

    q_from_min_qty = ceil_to_step(min_qty, step)
    q_from_notional = ceil_to_step(min_notional / mark, step)
    q_min = max(q_from_min_qty, q_from_notional)
    if q_min <= 0:
        return _bad_input()

    fee_rt = round_trip_fee_rate(fee_bps_per_side)
    dist = abs(mark - stop)
    q_1pct = floor_to_step(
        wallet_balance * max_real_risk_pct / Decimal("100") / (dist + fee_rt * mark),
        step,
    )

    if available <= 0:
        return SizeOfferResult(
            offer=None,
            reason="leverage_over_5x",
            candidate=None,
            q_min=q_min,
            q_fit=None,
            q_1pct=q_1pct,
            need_margin=None,
        )

    inv_lev = Decimal("1") / Decimal(max_leverage)
    q_fit = floor_to_step(available * margin_buffer / (inv_lev + fee_rt) / mark, step)

    if risk_qty >= q_min:
        offer_qty = min(risk_qty, q_fit, q_1pct)
        if offer_qty >= q_min:
            kind = "fit_margin"
            qty = offer_qty
            if qty == q_fit:
                bound = "margin"
            elif qty == q_1pct:
                bound = "risk_1pct"
            else:
                bound = "risk"
        else:
            kind = "min_lot"
            qty = q_min
            bound = "min_lot"
    else:
        kind = "min_lot"
        qty = q_min
        bound = "min_lot"

    notional = qty * mark
    risk_usdt = dist * qty
    fee_usdt = fee_rt * notional
    real_risk_pct = (risk_usdt + fee_usdt) / wallet_balance * Decimal("100")
    denom = available * margin_buffer - fee_rt * notional
    need_margin = (notional / Decimal(max_leverage) + fee_rt * notional) / margin_buffer

    if denom <= 0:
        candidate = SizeOffer(
            qty=qty,
            notional=notional,
            risk_usdt=risk_usdt,
            fee_usdt=fee_usdt,
            real_risk_pct=real_risk_pct,
            leverage=max_leverage + 1,
            margin=notional / Decimal(max_leverage),
            kind=kind,
            bound=bound,
        )
        return SizeOfferResult(
            offer=None,
            reason="leverage_over_5x",
            candidate=candidate,
            q_min=q_min,
            q_fit=q_fit,
            q_1pct=q_1pct,
            need_margin=need_margin,
        )

    leverage = _decimal_ceil_ratio(notional, denom)
    margin = notional / Decimal(leverage)
    candidate = SizeOffer(
        qty=qty,
        notional=notional,
        risk_usdt=risk_usdt,
        fee_usdt=fee_usdt,
        real_risk_pct=real_risk_pct,
        leverage=leverage,
        margin=margin,
        kind=kind,
        bound=bound,
    )

    if real_risk_pct > max_real_risk_pct:
        return SizeOfferResult(
            offer=None,
            reason="risk_over_1pct",
            candidate=candidate,
            q_min=q_min,
            q_fit=q_fit,
            q_1pct=q_1pct,
            need_margin=need_margin,
        )
    if leverage > max_leverage:
        return SizeOfferResult(
            offer=None,
            reason="leverage_over_5x",
            candidate=candidate,
            q_min=q_min,
            q_fit=q_fit,
            q_1pct=q_1pct,
            need_margin=need_margin,
        )

    return SizeOfferResult(
        offer=candidate,
        reason=None,
        candidate=candidate,
        q_min=q_min,
        q_fit=q_fit,
        q_1pct=q_1pct,
        need_margin=need_margin,
    )


def minimum_contract_qty(
    *,
    mark: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    step: Decimal,
) -> Decimal:
    q_from_min_qty = ceil_to_step(min_qty, step)
    q_from_notional = ceil_to_step(min_notional / mark, step)
    return max(q_from_min_qty, q_from_notional)
