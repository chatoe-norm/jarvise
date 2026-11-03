"""Thai style-A Telegram copy for trade-assistant signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jarvise_assist.futures import SymbolAccountConfig
from jarvise_assist.sizing import SizedSignal, round_to_tick

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
    exp_s = exp.strftime("%H:%M")
    base = _base_asset(sized.symbol)
    notional_s = fmt_price(sized.notional.quantize(Decimal("0.01")))
    loss_s = fmt_price(sized.loss_at_stop.quantize(Decimal("0.01")))
    lev = max(1, int(sized.leverage))
    margin = (sized.notional / Decimal(lev)).quantize(Decimal("0.01"))
    margin_s = fmt_price(margin)
    if tick_size is not None and tick_size > 0:
        entry_s = _fmt_price_tick(sized.entry, tick_size)
        stop_s = _fmt_price_tick(round_to_tick(sized.stop, tick_size), tick_size)
        target_s = _fmt_price_tick(round_to_tick(sized.target, tick_size), tick_size)
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
        f"หมดอายุ {exp_s}",
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


def format_move_stop_advice(*, symbol: str, side: str, stop: Decimal) -> str:
    side_l = side.lower()
    emoji = "↗️" if side_l == "long" else "↘️"
    side_label = "LONG" if side_l == "long" else "SHORT"
    return f"{emoji} {symbol.upper()} {side_label} · เลื่อน stop → {fmt_price(stop)}"


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
