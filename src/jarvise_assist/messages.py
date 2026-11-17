"""Thai style-A Telegram copy for trade-assistant signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jarvise_assist.futures import SymbolAccountConfig
from jarvise_assist.sizing import SizedSignal, floor_to_step, round_level_to_tick
from jarvise_assist.track import FEE_BPS_PER_SIDE
from jarvise_paper.profit_lock import DEFAULT_BREAKEVEN_R, DEFAULT_TRAIL_AFTER_R

# Asia/Bangkok has no DST; avoid the tzdata package on Windows.
BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
STOP_INSTRUCTION = "ตั้ง Stop-Market (Reduce-Only, trigger = Mark Price) ทันทีหลังเข้า"
TP_INSTRUCTION = "ตั้ง TP เป็น Mark Price เช่นกัน"


def fmt_price(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


# Back-compat alias used by older call sites / tests.
_fmt_price = fmt_price


def fmt_price_display(value: Decimal | float | str, tick: Decimal | None = None) -> str:
    """Owner-facing price: tick when known, else strip float binary noise.

    Decimal(str(float)) freezes binary residue that fmt_price will not strip, so
    Decimals without a tick also round-trip through float + .10g.
    """
    if tick is not None and tick > 0:
        return _fmt_price_tick(Decimal(str(value)), tick)
    if isinstance(value, float):
        return fmt_price(Decimal(format(value, ".10g")))
    if isinstance(value, Decimal):
        return fmt_price(Decimal(format(float(value), ".10g")))
    return fmt_price(Decimal(str(value)))


def _tick_decimals(tick: Decimal) -> int:
    t = tick.normalize()
    exp = t.as_tuple().exponent
    if not isinstance(exp, int):
        return 0
    return max(0, -exp)


def _fmt_price_tick(value: Decimal, tick: Decimal) -> str:
    decimals = _tick_decimals(tick)
    quant = Decimal("1").scaleb(-decimals) if decimals > 0 else Decimal("1")
    shown = value.quantize(quant, rounding=ROUND_HALF_UP)
    return format(shown, f".{decimals}f")


def format_prior_idea_line(
    *,
    label: str,
    entry: Decimal,
    stop: Decimal,
    qty: Decimal,
    sent_at_ms: int | None = None,
    tick_size: Decimal | None = None,
    with_time: bool = False,
) -> str:
    """Owner line citing a prior idea (on-demand re-quote or morning hold). Reference only: do not place this order."""
    if tick_size is not None and tick_size > 0:
        entry_s = _fmt_price_tick(entry, tick_size)
        stop_s = _fmt_price_tick(stop, tick_size)
    else:
        entry_s = fmt_price_display(entry)
        stop_s = fmt_price_display(stop)
    qty_s = fmt_qty(qty)
    head = label
    if with_time and sent_at_ms:
        hhmm = datetime.fromtimestamp(int(sent_at_ms) / 1000.0, tz=BANGKOK).strftime("%H:%M")
        head = f"{label} ({hhmm})"
    body = f"เข้า {entry_s}"
    if stop > 0:
        body = f"{body} · stop {stop_s}"
    return f"อ้างอิงเท่านั้น ไม่ต้องตั้ง · {head}: {body} · {qty_s}"


def fmt_qty(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


_fmt_qty = fmt_qty


def _base_asset(symbol: str) -> str:
    sym = symbol.upper()
    if sym.endswith("USDT"):
        return sym[:-4]
    return sym


def account_leverage_warning_lines(
    sized: SizedSignal,
    config: SymbolAccountConfig | None,
) -> list[str]:
    """Extra lines when account leverage/marginType disagrees with the message."""
    if config is None:
        return []
    lines: list[str] = []
    if config.leverage > 0 and config.leverage < int(sized.leverage):
        lines.append(f"บัญชีตั้ง {sized.symbol} ไว้ {config.leverage}x — ต้องเปลี่ยนเป็น {int(sized.leverage)}x")
    margin = (config.margin_type or "").upper()
    if margin and margin != "ISOLATED":
        lines.append(f"บัญชีตั้ง {sized.symbol} เป็น {margin} — ต้องเป็น Isolated")
    return lines


def _budget_line(sized: SizedSignal, balance: Decimal | None) -> str | None:
    bal = sized.balance_usdt if sized.balance_usdt is not None else balance
    if bal is None or bal <= 0:
        return None
    budget = sized.budget_usdt
    if budget is None:
        budget = bal * (sized.risk_pct / Decimal("100"))
    return f"ฐาน: Futures {fmt_price(bal)} USDT × {fmt_price(sized.risk_pct)}% = งบ {fmt_price(budget)} USDT"


def _step_removed_over_5pct(sized: SizedSignal) -> bool:
    budget = sized.budget_usdt
    step = sized.step_size
    if budget is None or budget <= 0 or step is None or step <= 0 or sized.r_distance <= 0:
        return False
    raw_qty = budget / sized.r_distance
    floored = floor_to_step(raw_qty, step)
    removed = (raw_qty - floored) * sized.r_distance
    return removed > budget * Decimal("0.05")


def _requote_reason_line(sized: SizedSignal) -> str:
    when = ""
    if sized.requote_anchor_sent_at_ms:
        hhmm = datetime.fromtimestamp(int(sized.requote_anchor_sent_at_ms) / 1000.0, tz=BANGKOK).strftime(
            "%H:%M"
        )
        when = f" {hhmm}"
    loss = sized.requote_first_loss
    loss_s = fmt_price(loss.quantize(Decimal("0.01"))) if loss is not None else "?"
    return f"ลดขนาดเพราะ: เพดาน re-quote จากไอเดีย {sized.symbol}{when} (เสี่ยงครั้งแรก {loss_s} USDT)"


def _reduction_lines(sized: SizedSignal) -> list[str]:
    budget = sized.budget_usdt
    if budget is None or budget <= 0 or sized.loss_at_stop >= budget * Decimal("0.95"):
        return []
    lines: list[str] = []
    for reason in sized.reduce_reasons:
        if reason == "step" and _step_removed_over_5pct(sized):
            lines.append("ลดขนาดเพราะ: ปัดจำนวนตาม lot step")
        elif reason == "margin_cap":
            lines.append("ลดขนาดเพราะ: margin ที่ว่างไม่พอ")
        elif reason == "requote_cap":
            lines.append(_requote_reason_line(sized))
    return lines


def format_signal_message(
    sized: SizedSignal,
    *,
    expires_at_ms: int,
    balance: Decimal | None = None,
    tick_size: Decimal | None = None,
    account_config: SymbolAccountConfig | None = None,
    confirm_within_min: int | None = None,
) -> str:
    side = sized.side.lower()
    emoji = "🔺" if side == "long" else "🔻"
    side_label = "LONG" if side == "long" else "SHORT"
    stop_pct = (sized.stop - sized.entry) / sized.entry * Decimal("100")
    stop_sign = "+" if stop_pct >= 0 else ""
    if balance is not None and balance > 0:
        loss_pct = (sized.loss_at_stop / balance * Decimal("100")).quantize(Decimal("0.01"))
    else:
        loss_pct = sized.risk_pct
    exp = datetime.fromtimestamp(expires_at_ms / 1000.0, tz=UTC).astimezone(BANGKOK)
    if confirm_within_min is not None:
        exp_s = exp.strftime("%H:%M:%S")
        expiry_line = f"หมดอายุ {exp_s} · ยืนยันภายใน {confirm_within_min} นาที"
    else:
        exp_s = exp.strftime("%H:%M")
        expiry_line = f"ราคานี้ถึง {exp_s} · กดหลังจากนั้นบอทคิดราคาใหม่"
    base = _base_asset(sized.symbol)
    notional_s = fmt_price(sized.notional.quantize(Decimal("0.01")))
    loss_s = fmt_price(sized.loss_at_stop.quantize(Decimal("0.01")))
    lev = max(1, int(sized.leverage))
    margin = (sized.notional / Decimal(lev)).quantize(Decimal("0.01"))
    margin_s = fmt_price(margin)
    if tick_size is not None and tick_size > 0:
        entry_s = _fmt_price_tick(sized.entry, tick_size)
        stop_s = _fmt_price_tick(
            round_level_to_tick(sized.stop, tick_size, side=side, kind="stop"),
            tick_size,
        )
        target_s = _fmt_price_tick(
            round_level_to_tick(sized.target, tick_size, side=side, kind="target"),
            tick_size,
        )
    else:
        entry_s = fmt_price_display(sized.entry)
        stop_s = fmt_price_display(sized.stop)
        target_s = fmt_price_display(sized.target)
    lines = [
        f"{emoji} {sized.symbol} · {side_label} (4h)",
        f"เข้า: {entry_s}",
        f"Stop: {stop_s} ({stop_sign}{float(stop_pct):.1f}%)",
        f"เป้า: {target_s} (2R)",
        f"ขนาด: {fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT",
    ]
    budget_line = _budget_line(sized, balance)
    if budget_line is not None:
        lines.append(budget_line)
    lines.extend(
        [
            f"Leverage ที่ต้องใช้: {lev}x (isolated — บัญชีต้องเป็น Single-Asset ไม่ใช่ Multi-Assets)",
            f"margin ≈ {margin_s} USDT",
            f"ถ้าชน stop เสีย: {loss_s} USDT ({fmt_price(loss_pct)}%)",
            *_reduction_lines(sized),
            expiry_line,
            STOP_INSTRUCTION,
            TP_INSTRUCTION,
            "ตั้ง TP/SL ที่เดียว (ใน order หรือ Position TP/SL) — ตั้งทั้งสองที่จะได้ stop 2 ตัว",
        ]
    )
    for warn in account_leverage_warning_lines(sized, account_config):
        lines.insert(lines.index(STOP_INSTRUCTION), warn)
    if sized.leverage_capped:
        lines.insert(
            lines.index(STOP_INSTRUCTION),
            f"ขนาดเต็มต้องใช้ {sized.uncapped_leverage}x — ลดเป็น {sized.leverage}x แล้ว",
        )
    return "\n".join(lines)


def format_permission_error_alert() -> str:
    return (
        "Jarvise trade assist: คีย์ Binance อ่าน futures ไม่ได้ "
        "(permission error). สร้างคีย์ read-only ใหม่แล้วใส่บน VPS — "
        "จะไม่ลองซ้ำจนกว่าจะรีเซ็ต edge"
    )


def format_key_unsafe_alert() -> str:
    return "Jarvise trade assist: คีย์ Binance มีสิทธิ์เทรดหรือถอน — ใช้เฉพาะ Enable Reading แล้วลองใหม่"


def format_futures_down_alert(*, minutes: int) -> str:
    return f"Jarvise trade assist: อ่าน futures ไม่ได้เกิน {minutes} นาที"


def format_futures_recovered_alert() -> str:
    return "Jarvise trade assist: อ่าน futures ได้แล้ว"


def format_no_stop_warning(
    *,
    symbol: str,
    side: str,
    stop: Decimal,
    tick: Decimal | None = None,
) -> str:
    side_label = "LONG" if side.lower() == "long" else "SHORT"
    if tick is not None and tick > 0:
        stop_s = _fmt_price_tick(
            round_level_to_tick(stop, tick, side=side, kind="stop"),
            tick,
        )
    else:
        stop_s = fmt_price_display(stop)
    return (
        f"⚠️ {symbol.upper()} {side_label} ไม่มี stop order — "
        f"ตั้ง Stop-Market (Reduce-Only, Mark Price) ที่ {stop_s}"
    )


def format_partial_stop_warning(
    *,
    symbol: str,
    side: str,
    covered: Decimal,
    position_qty: Decimal,
    stop: Decimal,
    tick: Decimal | None = None,
) -> str:
    side_label = "LONG" if side.lower() == "long" else "SHORT"
    covered_s = fmt_qty(covered)
    qty_s = fmt_qty(position_qty)
    if tick is not None and tick > 0:
        stop_s = _fmt_price_tick(
            round_level_to_tick(stop, tick, side=side, kind="stop"),
            tick,
        )
    else:
        stop_s = fmt_price_display(stop)
    return (
        f"⚠️ {symbol.upper()} {side_label} stop คุมแค่ {covered_s}/{qty_s} — "
        f"ตั้ง stop ให้ครบทั้งไม้ (Reduce-Only, Mark Price) ที่ {stop_s}"
    )


def _lock_at_stop(
    *,
    side: str,
    qty: Decimal,
    entry: Decimal,
    initial_stop: Decimal,
    new_stop: Decimal,
) -> tuple[Decimal, Decimal | None]:
    """Locked PnL at ``new_stop`` and its R multiple (None when risk <= 0)."""
    side_l = side.lower()
    if side_l == "long":
        lock_usdt = (new_stop - entry) * qty
    else:
        lock_usdt = (entry - new_stop) * qty
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return lock_usdt, None
    if qty == 0:
        return lock_usdt, Decimal("0")
    return lock_usdt, lock_usdt / (qty * risk)


def _fmt_r_level(level: Decimal) -> str:
    """+0.5R / +1R / +1.5R — no trailing zeros. The leading '+' is added by callers."""
    text = format(level.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fmt_signed_r(value: Decimal | None) -> str:
    if value is None:
        return "R unknown"
    return f"{float(value):+.2f}R"


def _rule_r_label(level: float) -> str:
    """+0.5R / +1R from the profit-lock constants the advice actually uses."""
    return f"+{_fmt_r_level(Decimal(str(level)))}R"


def wick_rule_line(*, source: str) -> str:
    """Owner line for the trigger ``trail_stop_with_source`` implements.

    Favorable travel is the high (long) or low (short) of a closed 1h spot bar,
    not that bar's close. The R levels are the profit-lock constants.
    """
    if source == "trail":
        level = _rule_r_label(DEFAULT_TRAIL_AFTER_R)
    else:
        level = _rule_r_label(DEFAULT_BREAKEVEN_R)
    return f"เกณฑ์: ไส้แท่ง 1h สปอตที่ปิดแล้วแตะ {level}"


# Progress alerts restate the same wick rule. Built from the constants so a
# close-price sentence cannot drift back in while the code still reads wicks.
PROGRESS_RULE_LINE = (
    "แจ้งเพื่อทราบ · กติกาเดิม: "
    f"เลื่อน BE เมื่อไส้แท่ง 1h สปอตที่ปิดแล้วแตะ {_rule_r_label(DEFAULT_BREAKEVEN_R)}"
    f" · trail เมื่อไส้แตะ {_rule_r_label(DEFAULT_TRAIL_AFTER_R)}"
)
MANUAL_MOVE_LINE = "ยังไม่ได้เลื่อน stop — ถ้าต้องการ ให้เลื่อนเองในแอป (Stop-Market, Reduce-Only, Mark Price)"


def approx_r_after_fees(
    *,
    side: str,
    qty: Decimal,
    entry: Decimal,
    initial_stop: Decimal,
    new_stop: Decimal,
    fee_bps_per_side: Decimal | None = None,
) -> Decimal | None:
    """Locked R at ``new_stop`` after the assist taker fee on entry and on the stop.

    Fee is ``FEE_BPS_PER_SIDE`` (Binance USDⓈ-M VIP0 taker) each side. Slippage
    is not included. None when the initial risk is not positive.
    """
    lock_usdt, _lock_r = _lock_at_stop(
        side=side, qty=qty, entry=entry, initial_stop=initial_stop, new_stop=new_stop
    )
    risk = abs(entry - initial_stop)
    if risk <= 0 or qty == 0:
        return None
    bps = fee_bps_per_side if fee_bps_per_side is not None else Decimal(str(FEE_BPS_PER_SIDE))
    fee = (bps / Decimal("10000")) * abs(qty) * (abs(entry) + abs(new_stop))
    return (lock_usdt - fee) / (abs(qty) * risk)


def stop_gap_r(
    *,
    side: str,
    entry: Decimal,
    initial_stop: Decimal,
    mark: Decimal,
    new_stop: Decimal,
) -> Decimal | None:
    """Signed distance from the current mark to the proposed stop, in initial R.

    Positive when the stop is still on the protective side of the mark
    (below a long, above a short).
    """
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return None
    if side.lower() == "long":
        return (mark - new_stop) / risk
    return (new_stop - mark) / risk


def format_move_stop_advice(
    *,
    symbol: str,
    side: str,
    qty: Decimal,
    old_stop: Decimal | None,
    new_stop: Decimal,
    mark: Decimal,
    entry: Decimal,
    initial_stop: Decimal,
    expires_at_ms: int,
    tick: Decimal | None = None,
    source: str | None = None,
    with_button: bool = True,
) -> str:
    """Move-stop advice body. Levels already tick-rounded by the caller.

    ``with_button=False`` is the 07:30 flush: the stop was not moved and there
    is no button, so the text must not mention a button or an expiry.
    """
    side_l = side.lower()
    side_label = "LONG" if side_l == "long" else "SHORT"
    if tick is not None and tick > 0:
        old_s = _fmt_price_tick(old_stop, tick) if old_stop is not None else "ไม่มี"
        new_s = _fmt_price_tick(new_stop, tick)
        mark_s = _fmt_price_tick(mark, tick)
    else:
        old_s = fmt_price_display(old_stop) if old_stop is not None else "ไม่มี"
        new_s = fmt_price_display(new_stop)
        mark_s = fmt_price_display(mark)
    lock_usdt, lock_r = _lock_at_stop(
        side=side, qty=qty, entry=entry, initial_stop=initial_stop, new_stop=new_stop
    )
    after = approx_r_after_fees(side=side, qty=qty, entry=entry, initial_stop=initial_stop, new_stop=new_stop)
    gap = stop_gap_r(side=side, entry=entry, initial_stop=initial_stop, mark=mark, new_stop=new_stop)
    lock_usdt_s = f"{float(lock_usdt):+.2f}"
    lock_prefix = "ถ้าเลื่อน ล็อก ≈" if not with_button else "ล็อก ≈"
    lines = [
        f"{symbol.upper()} {side_label} · qty {fmt_qty(qty)}",
        f"stop {old_s} → {new_s}",
        f"mark {mark_s}",
        (
            f"{lock_prefix} {_fmt_signed_r(lock_r)} ({lock_usdt_s} USDT) (ก่อนค่าธรรมเนียม)"
            f" · หลังค่าธรรมเนียม ≈ {_fmt_signed_r(after)}"
        ),
        f"ห่าง mark {_fmt_signed_r(gap)}",
    ]
    if source in {"breakeven", "trail"}:
        lines.append(wick_rule_line(source=str(source)))
    if with_button:
        hhmm = datetime.fromtimestamp(int(expires_at_ms) / 1000.0, tz=BANGKOK).strftime("%H:%M")
        lines.append(f"ปุ่มหมดอายุ {hhmm}")
    else:
        lines.append(MANUAL_MOVE_LINE)
    return "\n".join(lines)


def format_exit_advice(
    *,
    symbol: str,
    side: str,
    reason: str,
    approx_r: Decimal,
) -> str:
    side_label = "LONG" if side.lower() == "long" else "SHORT"
    reason_l = reason.lower()
    if reason_l == "flat":
        detail = "สัญญาณ 4h เป็น FLAT"
    elif reason_l == "opposite":
        detail = "สัญญาณ 4h กลับด้าน"
    elif reason_l == "target_2r":
        detail = "ราคาถึงเป้า 2R"
    else:
        detail = reason
    r_s = f"{float(approx_r):.1f}"
    return f"✅ {symbol.upper()} {side_label} · ออกตอนนี้\n{detail} · ประมาณ {r_s}R"


def progress_alert_after_tap(text: str, *, keep_close_line: bool = False) -> str:
    """Drop stale move/press lines from a progress alert after an ms: tap or expiry.

    With keep_close_line=False (default), output is byte-identical to the pre-keep-xx
    strip: every "กด " line goes. With True, the close explanation line stays.
    """
    kept: list[str] = []
    for line in str(text).splitlines():
        if line.startswith("📌 ยังไม่ได้เลื่อน") or line.startswith("กด 🛡"):
            continue
        if line.startswith("กด ") and not keep_close_line:
            continue
        kept.append(line)
    return "\n".join(kept)


def format_progress_alert(
    *,
    symbol: str,
    side: str,
    level: Decimal,
    r_now: Decimal,
    mark: Decimal,
    entry: Decimal,
    stop: Decimal,
    stop_r: Decimal,
    tick: Decimal,
    with_button: bool,
    pending_stop: Decimal | None = None,
    pending_source: str | None = None,
    pending_lock_r: Decimal | None = None,
    pending_after_fees: Decimal | None = None,
    pending_gap_r: Decimal | None = None,
    move_button: bool = False,
    move_expires_at_ms: int | None = None,
    early_close: bool = False,
) -> str:
    """Owner alert that the live mark crossed a progress level. No new order rule."""
    side_label = "LONG" if side.lower() == "long" else "SHORT"
    lines = [
        (f"📈 {symbol.upper()} {side_label} ถึง +{_fmt_r_level(level)}R แล้ว (ตอนนี้ {r_now:+.2f}R)"),
        (
            f"mark {_fmt_price_tick(mark, tick)} · entry {_fmt_price_tick(entry, tick)} · "
            f"stop ปัจจุบัน {_fmt_price_tick(stop, tick)} ({stop_r:+.2f}R)"
        ),
        PROGRESS_RULE_LINE,
    ]
    if pending_stop is not None:
        src = str(pending_source or "")
        if src == "breakeven":
            src_label = "ทุน"
        elif src == "trail":
            src_label = "trail"
        else:
            src_label = src
        reminder = f"📌 ยังไม่ได้เลื่อน stop ตามคำแนะนำ: → {_fmt_price_tick(pending_stop, tick)} ({src_label})"
        if pending_lock_r is not None:
            reminder += f" · ล็อก ≈ {float(pending_lock_r):+.2f}R"
        if pending_after_fees is not None:
            reminder += f" · หลังค่าธรรมเนียม ≈ {float(pending_after_fees):+.2f}R"
        if pending_gap_r is not None:
            reminder += f" · ห่าง mark {float(pending_gap_r):+.2f}R"
        if not move_button:
            reminder += " — เลื่อนเองในแอป (Stop-Market, Reduce-Only, Mark Price)"
        lines.append(reminder)
        if move_button and move_expires_at_ms is not None:
            hhmm = datetime.fromtimestamp(int(move_expires_at_ms) / 1000.0, tz=BANGKOK).strftime("%H:%M")
            lines.append(f"กด 🛡 เลื่อน stop = ตั้ง stop ใหม่ทั้งไม้ แล้วยกเลิก stop เดิม · ปุ่มเลื่อนหมดอายุ {hhmm}")
    if with_button:
        if early_close:
            lines.append("กด ⚠️ ปิดก่อนแผน = ปิดทั้งไม้ที่ราคาตลาดตอนกด · ไม่ใช่ทางออกตามแผน")
        else:
            lines.append("กด ✅ ปิดไม้ = ปิดทั้งไม้ที่ราคาตลาดตอนกด")
    return "\n".join(lines)


def safe_symbol_configs(client: Any) -> dict[str, SymbolAccountConfig]:
    """Read symbolConfig; empty dict on any failure (never raises)."""
    try:
        getter = getattr(client, "symbol_config", None)
        if getter is None:
            return {}
        raw = getter()
        if isinstance(raw, dict):
            return raw
    except Exception:  # noqa: BLE001
        return {}
    return {}
