"""Pure Quick min-lot / fit-margin offer math (no I/O, no orders)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal

from jarvise_assist.messages import fmt_price, fmt_qty
from jarvise_assist.sizing import floor_to_step


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


def ceil_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step size must be positive")
    steps = (qty / step).to_integral_value(rounding=ROUND_UP)
    return steps * step


def _bad(*_args: object) -> tuple[None, str]:
    return None, "bad_input"


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
) -> tuple[SizeOffer | None, str | None]:
    if mark <= 0 or step <= 0 or stop == mark or risk_qty < 0:
        return _bad()
    for v in (mark, stop, wallet_balance, available, min_qty, min_notional, step, risk_qty):
        if not isinstance(v, Decimal):
            return _bad()
        if not v.is_finite():
            return _bad()
    if wallet_balance <= 0:
        return None, "no_balance"
    if available <= 0:
        return None, "leverage_over_5x"

    q_from_min_qty = ceil_to_step(min_qty, step)
    q_from_notional = ceil_to_step(min_notional / mark, step)
    q_min = max(q_from_min_qty, q_from_notional)

    if risk_qty >= q_min:
        q_fit = floor_to_step(available * Decimal(max_leverage) * margin_buffer / mark, step)
        offer_qty = min(risk_qty, q_fit)
        if offer_qty >= q_min:
            kind = "fit_margin"
            qty = offer_qty
        else:
            kind = "min_lot"
            qty = q_min
    else:
        kind = "min_lot"
        qty = q_min

    if qty <= 0:
        return None, "leverage_over_5x"

    notional = qty * mark
    risk_usdt = abs(mark - stop) * qty
    fee_usdt = (Decimal("2") * fee_bps_per_side / Decimal("10000")) * notional
    real_risk_pct = (risk_usdt + fee_usdt) / wallet_balance * Decimal("100")

    if available * margin_buffer > 0:
        leverage = max(1, int(math.ceil(float(notional / (available * margin_buffer)))))
    else:
        leverage = max_leverage + 1

    margin = notional / Decimal(leverage)

    if real_risk_pct > max_real_risk_pct:
        return None, "risk_over_1pct"
    if leverage > max_leverage:
        return None, "leverage_over_5x"

    return (
        SizeOffer(
            qty=qty,
            notional=notional,
            risk_usdt=risk_usdt,
            fee_usdt=fee_usdt,
            real_risk_pct=real_risk_pct,
            leverage=leverage,
            margin=margin,
            kind=kind,
        ),
        None,
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


def format_offer_refusal(
    reason: str,
    *,
    mark: Decimal,
    stop: Decimal,
    wallet_balance: Decimal,
    available: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    step: Decimal,
    base: str,
    fee_bps_per_side: Decimal = Decimal("6"),
) -> str:
    if reason == "no_balance":
        return "no_balance"
    if reason == "bad_input":
        return "bad_input"
    q_min = minimum_contract_qty(mark=mark, min_qty=min_qty, min_notional=min_notional, step=step)
    notional = q_min * mark
    risk_usdt = abs(mark - stop) * q_min
    fee_usdt = (Decimal("2") * fee_bps_per_side / Decimal("10000")) * notional
    real_pct = (risk_usdt + fee_usdt) / wallet_balance * Decimal("100")

    if reason == "risk_over_1pct":
        return (
            f"ขั้นต่ำ {fmt_qty(q_min)} {base} ≈ {fmt_price(notional.quantize(Decimal('0.01')))} USDT "
            f"= risk จริง {fmt_price(real_pct.quantize(Decimal('0.01')))}% เกิน 1% — ไม่เปิดไม้นี้"
        )
    if reason == "leverage_over_5x":
        need = (notional / Decimal("5")).quantize(Decimal("0.01"))
        avail_s = fmt_price(available.quantize(Decimal("0.01")))
        return f"margin ไม่พอแม้ใช้ 5x — ต้องใช้ ≈ {fmt_price(need)} USDT มีให้ใช้ {avail_s} USDT"
    return reason
