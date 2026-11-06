"""Thai style-A Telegram copy for trade-assistant signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jarvise_assist.futures import SymbolAccountConfig
from jarvise_assist.sizing import SizedSignal, round_level_to_tick

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
        entry_s = fmt_price(entry)
        stop_s = fmt_price(stop)
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
        expiry_line = f"หมดอายุ {exp_s}"
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
        entry_s = fmt_price(sized.entry)
        stop_s = fmt_price(sized.stop)
        target_s = fmt_price(sized.target)
    lines = [
        f"{emoji} {sized.symbol} · {side_label} (4h)",
        f"เข้า: {entry_s}",
        f"Stop: {stop_s} ({stop_sign}{float(stop_pct):.1f}%)",
        f"เป้า: {target_s} (2R)",
        f"ขนาด: {fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT",
        f"Leverage ที่ต้องใช้: {lev}x (isolated — บัญชีต้องเป็น Single-Asset ไม่ใช่ Multi-Assets)",
        f"margin ≈ {margin_s} USDT",
        f"ถ้าชน stop เสีย: {loss_s} USDT ({fmt_price(loss_pct)}%)",
        expiry_line,
        STOP_INSTRUCTION,
        TP_INSTRUCTION,
        "ตั้ง TP/SL ที่เดียว (ใน order หรือ Position TP/SL) — ตั้งทั้งสองที่จะได้ stop 2 ตัว",
    ]
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


def format_no_stop_warning(*, symbol: str, side: str, stop: Decimal) -> str:
    side_label = "LONG" if side.lower() == "long" else "SHORT"
    return (
        f"⚠️ {symbol.upper()} {side_label} ไม่มี stop order — "
        f"ตั้ง Stop-Market (Reduce-Only, Mark Price) ที่ {fmt_price(stop)}"
    )


def format_partial_stop_warning(
    *,
    symbol: str,
    side: str,
    covered: Decimal,
    position_qty: Decimal,
    stop: Decimal,
) -> str:
    side_label = "LONG" if side.lower() == "long" else "SHORT"
    covered_s = fmt_qty(covered)
    qty_s = fmt_qty(position_qty)
    return (
        f"⚠️ {symbol.upper()} {side_label} stop คุมแค่ {covered_s}/{qty_s} — "
        f"ตั้ง stop ให้ครบทั้งไม้ (Reduce-Only, Mark Price) ที่ {fmt_price(stop)}"
    )


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
) -> str:
    """Move-stop advice body. Levels already tick-rounded by the caller."""
    side_l = side.lower()
    side_label = "LONG" if side_l == "long" else "SHORT"
    if tick is not None and tick > 0:
        old_s = _fmt_price_tick(old_stop, tick) if old_stop is not None else "ไม่มี"
        new_s = _fmt_price_tick(new_stop, tick)
        mark_s = _fmt_price_tick(mark, tick)
    else:
        old_s = fmt_price(old_stop) if old_stop is not None else "ไม่มี"
        new_s = fmt_price(new_stop)
        mark_s = fmt_price(mark)
    risk = abs(entry - initial_stop)
    if side_l == "long":
        lock_usdt = (new_stop - entry) * qty
    else:
        lock_usdt = (entry - new_stop) * qty
    if risk > 0:
        lock_r = lock_usdt / (qty * risk) if qty != 0 else Decimal("0")
        lock_r_s = f"{float(lock_r):+.2f}"
    else:
        lock_r_s = "R unknown"
    lock_usdt_s = f"{float(lock_usdt):+.2f}"
    hhmm = datetime.fromtimestamp(int(expires_at_ms) / 1000.0, tz=BANGKOK).strftime("%H:%M")
    return (
        f"{symbol.upper()} {side_label} · qty {fmt_qty(qty)}\n"
        f"stop {old_s} → {new_s}\n"
        f"mark {mark_s}\n"
        f"ล็อก ≈ {lock_r_s}R ({lock_usdt_s} USDT) (ก่อนค่าธรรมเนียม)\n"
        f"ปุ่มหมดอายุ {hhmm}"
    )


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
