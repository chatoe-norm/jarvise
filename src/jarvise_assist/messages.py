"""Thai style-A Telegram copy for trade-assistant signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from jarvise_assist.sizing import SizedSignal

# Asia/Bangkok has no DST; avoid the tzdata package on Windows.
BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")


def _fmt_price(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fmt_qty(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _base_asset(symbol: str) -> str:
    sym = symbol.upper()
    if sym.endswith("USDT"):
        return sym[:-4]
    return sym


def format_signal_message(
    sized: SizedSignal,
    *,
    expires_at_ms: int,
    balance: Decimal | None = None,
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
    notional_s = _fmt_price(sized.notional.quantize(Decimal("0.01")))
    loss_s = _fmt_price(sized.loss_at_stop.quantize(Decimal("0.01")))
    lev = max(1, int(sized.leverage))
    margin = (sized.notional / Decimal(lev)).quantize(Decimal("0.01"))
    margin_s = _fmt_price(margin)
    lines = [
        f"{emoji} {sized.symbol} · {side_label} (4h)",
        f"เข้า: {_fmt_price(sized.entry)}",
        f"Stop: {_fmt_price(sized.stop)} ({stop_sign}{float(stop_pct):.1f}%)",
        f"เป้า: {_fmt_price(sized.target)} (2R)",
        f"ขนาด: {_fmt_qty(sized.qty)} {base} ≈ {notional_s} USDT",
        f"Leverage ที่ต้องใช้: {lev}x (isolated)",
        f"margin ≈ {margin_s} USDT",
        f"ถ้าชน stop เสีย: {loss_s} USDT ({_fmt_price(loss_pct)}%)",
        f"หมดอายุ {exp_s}",
        "ตั้ง Stop-Market (Reduce-Only, trigger = Mark Price) ทันทีหลังเข้า",
    ]
    if sized.leverage_capped:
        lines.insert(
            -1,
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
        f"ตั้ง Stop-Market (Reduce-Only, Mark Price) ที่ {_fmt_price(stop)}"
    )


def format_move_stop_advice(*, symbol: str, side: str, stop: Decimal) -> str:
    side_l = side.lower()
    emoji = "↗️" if side_l == "long" else "↘️"
    side_label = "LONG" if side_l == "long" else "SHORT"
    return f"{emoji} {symbol.upper()} {side_label} · เลื่อน stop → {_fmt_price(stop)}"


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
