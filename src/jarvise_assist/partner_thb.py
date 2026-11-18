"""Two-step THB payout commands. No KYC decrypt and no network."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_store as ps

logger = logging.getLogger(__name__)

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_MINUS = "−"
_CENT = Decimal("0.01")
_AMOUNT_RE = re.compile(r"^(?:[0-9]{1,9}(?:\.[0-9]{1,6})?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,6})?)$")
_FY_RE = re.compile(r"^(pr_[0-9a-f]{16}):([0-9a-f]{8})$")
_REQ_RE = re.compile(r"^pr_[0-9a-f]{16}$")
_HEX6_RE = re.compile(r"^#([0-9a-f]{6})$")
_PAID_KEYS = ("sold", "rate", "trade", "net", "chain", "wd", "bank", "slip", "sale")
_REQUIRED = ("sold", "rate", "trade", "net")
_PAID_USAGE = "p paid <เป้าหมาย> sold= rate= trade= net= [chain=0.01] [wd=20] [bank=0]"
_FIX_USAGE = "p fix <#รหัส|pr_รหัส> [sold=] [rate=] [trade=] [net=] [chain=] [wd=] [bank=]"
_CARRY_USAGE = "p carry <ชื่อ> settle <USDT> <บาทต่อUSDT>"
_NOT_SAVED = "ยังไม่บันทึกอะไร"
_REASON_NEED = "⛔ ต้องใส่เหตุผลบรรทัดที่ 2 (อย่างน้อย 10 ตัวอักษร) แล้วส่งใหม่ — ยังไม่บันทึก"
_DECIMALS = "ทศนิยมเกิน (USDT ≤6 · เรท ≤4 · บาท ≤2) — ไม่ปัดให้"
_DUE = "รายการนี้ยังไม่ได้กด 📤 USDT ออกจากพอร์ต — กดก่อน แล้วค่อย p paid"
_LEGACY = "รายการนี้จ่ายก่อนเริ่มระบบเงินบาท — ไม่ต้องบันทึก"
_ADMIN = "แอดมินไม่ต้องบันทึกเงินบาท"
_RECORDED = "บันทึกเงินบาทรายการนี้แล้ว — แก้ด้วย p fix #{short}"
_NONE = "{name} ไม่มีรายการรอบันทึกเงินบาท"
_CARRY_CHANGED = "ยอดเศษยกมาเปลี่ยน — พิมพ์ p paid ใหม่"
_CARRY_CHANGED_SETTLE = "ยอดเศษยกมาเปลี่ยน — พิมพ์ p carry ใหม่"
_KYC_CHANGED = "ข้อมูลรับเงินไม่ตรง — พิมพ์ p paid ใหม่"
_EXPIRED = "หมดเวลา — พิมพ์คำสั่งใหม่"
_SOLD_MIN = "จำนวนขายต้องไม่น้อยกว่า 1 USDT"
_SETTLE_EXCEEDS = "ชำระเกินยอดเศษที่มี"
_UNSELLABLE = "⚠️ ยอดน้อยกว่า 1 USDT หลังหักค่าเครือข่าย ขายเป็นเงินบาทไม่ได้ — โอนเป็น USDT/นอกระบบ แล้วจบที่ขั้นนี้"
_CARRY_OPEN = "{name} ยังมีรายการเปิดอยู่ — บันทึก p paid ให้ครบก่อน"
_CARRY_NOT_CLOSED = "{name} ยังไม่ได้ปิดพอร์ต — p carry ใช้กับพาร์ทเนอร์ที่ปิดแล้ว"
_UNSELLABLE_PAID = "รายการนี้จบที่ขั้น 📤 แล้ว (ยอดน้อยกว่า 1 USDT) — ไม่ต้องบันทึกเงินบาท"
_SLIP_LONG = "อ้างอิงยาวเกิน 40 ตัวอักษร"
_SETTLE_REASON_RE = re.compile(r"^THB ([0-9][0-9,]*\.[0-9]{2}) @([0-9]+(?:\.[0-9]+)?) · (.*)$", re.DOTALL)
_BUG = "ทำไม่สำเร็จ — ยังไม่บันทึกอะไร"
_DB = "บันทึกไม่ได้ — ยังไม่บันทึกอะไร"
_NOTHING_CHANGED = "ไม่มีอะไรเปลี่ยน ไม่บันทึก"
_CARRY_LOCKED = "เศษของรายการนี้ถูกใช้ในการถอนครั้งถัดไปแล้ว — แก้ได้เฉพาะ rate/trade/wd/bank/net"


def fmt_thb(satang: int) -> str:
    sign = _MINUS if int(satang) < 0 else ""
    body = f"{(Decimal(abs(int(satang))) / Decimal(100)):,.2f}"
    return f"{sign}{body}"


def fmt_rate(rate_e4: int) -> str:
    """2 to 4 decimal places, from the stored e4 rate. Trailing zeros stop at 2 dp."""
    text = f"{Decimal(int(rate_e4)) / Decimal(10_000):.4f}"
    whole, frac = text.split(".")
    frac = frac.rstrip("0")
    if len(frac) < 2:
        frac = frac.ljust(2, "0")
    return f"{whole}.{frac}"


def fmt_effective(net_satang: int, denom_micro: int) -> str:
    if denom_micro <= 0:
        return "—"
    value = (Decimal(int(net_satang)) * Decimal(10_000) / Decimal(int(denom_micro))).quantize(
        Decimal("0.0001"), rounding=ROUND_DOWN
    )
    return f"{value:.4f}"


def _usdt_2(micro: int) -> str:
    sign = _MINUS if int(micro) < 0 else ""
    q = Decimal(abs(int(micro))) / Decimal(1_000_000)
    return f"{sign}{q:,.2f}"


def _sold_usdt(micro: int) -> str:
    return f"{int(micro) // 1_000_000:,}"


def short_id(request_id: str) -> str:
    return str(request_id)[-6:]


def stamp_short(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=BANGKOK).strftime("%d/%m %H:%M")


def stamp_full(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=BANGKOK).strftime("%d/%m/%Y %H:%M")


def _clean_number(raw: str) -> str | None:
    text = str(raw).strip()
    if "," in text:
        if not _AMOUNT_RE.match(text):
            return None
        text = text.replace(",", "")
    return text


def _parse_keys(first: str) -> dict[str, Any] | str:
    parts = first.strip().split()
    if len(parts) < 3:
        return "usage"
    pairs: dict[str, str] = {}
    target_bits: list[str] = []
    for bit in parts[2:]:
        if "=" not in bit:
            target_bits.append(bit)
            continue
        key, _, value = bit.partition("=")
        key = key.strip().lower()
        if key not in _PAID_KEYS or not value:
            return "usage"
        if key in pairs:
            return "usage"
        pairs[key] = value.strip()
    if any(key not in pairs for key in _REQUIRED):
        return "usage"
    target = " ".join(target_bits).strip()
    if not target:
        return "usage"
    return {"target": target, "pairs": pairs}


def _money(raw: str, kind: str) -> int | None:
    cleaned = _clean_number(raw)
    if cleaned is None:
        return None
    if kind == "usdt":
        return fx.parse_usdt(cleaned)
    if kind == "rate":
        return fx.parse_rate(cleaned)
    return fx.parse_thb(cleaned)


def parse_paid_command(text: str) -> dict[str, Any] | str:
    """Parse ``p paid``. A string result is ``usage`` or ``decimals``."""
    first, _, rest = (text or "").partition("\n")
    parsed = _parse_keys(first)
    if parsed == "usage" or not isinstance(parsed, dict):
        return "usage"
    pairs = parsed["pairs"]
    sold = _money(pairs["sold"], "usdt")
    rate = _money(pairs["rate"], "rate")
    trade = _money(pairs["trade"], "thb")
    net = _money(pairs["net"], "thb")
    chain = _money(pairs.get("chain", "0.01"), "usdt")
    wd = _money(pairs.get("wd", "20"), "thb")
    bank = _money(pairs.get("bank", "0"), "thb")
    if None in (sold, rate, trade, net, chain, wd, bank):
        return "decimals"
    assert sold is not None and rate is not None and trade is not None and net is not None
    assert chain is not None and wd is not None and bank is not None
    slip = pairs.get("slip")
    sale = pairs.get("sale")
    if (slip is not None and len(slip) > 40) or (sale is not None and len(sale) > 40):
        return "usage"
    reason = rest.strip()
    if len(reason) > 200:
        reason = reason[:200]
    rate_text = _clean_number(pairs["rate"]) or pairs["rate"]
    return {
        "target": str(parsed["target"]),
        "sold": int(sold),
        "rate_e4": int(rate),
        "rate": Decimal(rate_text),
        "trade": int(trade),
        "net": int(net),
        "chain": int(chain),
        "wd": int(wd),
        "bank": int(bank),
        "slip": slip,
        "sale": sale,
        "reason": reason,
    }


def _partner_by_name(conn: Any, name: str) -> Any:
    key = ps.name_key(name)
    if not key:
        return None
    for row in ps.list_partners(conn):
        if ps.name_key(str(row["name"])) == key:
            return row
    return None


def _awaiting_rows(conn: Any, partner_id: str | None = None) -> list[dict[str, Any]]:
    return fx.awaiting_thb(conn, partner_id)


def resolve_paid_target(conn: Any, target: str) -> dict[str, Any]:
    """A request dict, or ``{"refusal": text}``."""
    raw = target.strip()
    hex_m = _HEX6_RE.match(raw.lower())
    if _REQ_RE.match(raw.lower()) or hex_m is not None:
        if _REQ_RE.match(raw.lower()):
            rows = [row for row in _all_cash(conn) if str(row["id"]) == raw.lower()]
        else:
            suffix = hex_m.group(1) if hex_m is not None else ""
            rows = [row for row in _all_cash(conn) if str(row["id"]).endswith(suffix)]
        if not rows:
            return {"refusal": _with_unsaved("ไม่พบรายการ")}
        if len(rows) > 1:
            return {"refusal": _with_unsaved("รหัสซ้ำ — ใส่รหัสเต็ม pr_…")}
        return _classify_request(conn, rows[0])
    partner = _partner_by_name(conn, raw)
    if partner is None:
        return {"refusal": _with_unsaved("ไม่พบพาร์ทเนอร์")}
    if int(partner["is_admin"] or 0) == 1:
        return {"refusal": _with_unsaved(_ADMIN)}
    waiting = _awaiting_rows(conn, str(partner["id"]))
    name = str(partner["name"])
    if not waiting:
        due = _due_for_partner(conn, str(partner["id"]))
        if due:
            return {"refusal": _with_unsaved(_DUE)}
        return {"refusal": _with_unsaved(_NONE.format(name=name))}
    if len(waiting) > 1:
        bits = []
        for row in waiting:
            bits.append(
                f"#{short_id(str(row['id']))} ({_kind(str(row['kind']))} {_usdt_2(int(row['payout_micro']))})"
            )
        listed = ", ".join(bits)
        return {"refusal": _with_unsaved(f"{name} มีหลายรายการรอบันทึก — ระบุรหัส: {listed}")}
    return _classify_request(conn, waiting[0])


def _with_unsaved(text: str) -> str:
    if text.endswith(_NOT_SAVED):
        return text
    return f"{text}\n{_NOT_SAVED}"


def _all_cash(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM partner_request
        WHERE kind IN ('withdraw_profit', 'close') AND status = 'confirmed'
        ORDER BY id
        """
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _due_for_partner(conn: Any, partner_id: str) -> list[dict[str, Any]]:
    return [row for row in fx.open_settlements(conn, partner_id) if row.get("stage") == "due"]


def _classify_request(conn: Any, req: dict[str, Any]) -> dict[str, Any]:
    partner = ps.get_partner(conn, str(req["partner_id"]))
    if partner is not None and int(partner["is_admin"] or 0) == 1:
        return {"refusal": _with_unsaved(_ADMIN)}
    state = str(req.get("payout_state") or "")
    if state == "due":
        return {"refusal": _with_unsaved(_DUE)}
    if state != "paid":
        return {"refusal": _with_unsaved(_DUE)}
    paid_at = req.get("paid_at_ms")
    if paid_at is None or int(paid_at) < int(fx.THB_FLOW_SINCE_MS):
        return {"refusal": _with_unsaved(_LEGACY)}
    if fx.current_fx(conn, str(req["id"])) is not None:
        return {"refusal": _with_unsaved(_RECORDED.format(short=short_id(str(req["id"]))))}
    if fx.unsellable(conn, req):
        return {"refusal": _with_unsaved(_UNSELLABLE_PAID)}
    return {"request": req, "partner": partner}


def _kind(kind: str) -> str:
    return {"withdraw_profit": "ถอนกำไร", "close": "ปิดพอร์ต"}.get(kind, kind)


def kyc_bits(conn: Any, partner_id: str) -> dict[str, Any]:
    row = pk.current_kyc(conn, partner_id)
    if row is None:
        return {"id": None, "line": "⚠️ ยังไม่มีข้อมูลรับเงินในระบบ — ติดต่อแอดมิน", "holder_mismatch": False}
    account = pk.mask_account(str(row["bank_code"]), str(row["account_last4"]))
    line = f"{account} ({row['holder_name_mask']})"
    return {
        "id": str(row["id"]),
        "line": line,
        "holder_mismatch": int(row["holder_matches"]) != 1,
    }


def fee_pct(trade: int, gross: int) -> Decimal:
    if gross <= 0:
        return Decimal(0)
    return (Decimal(trade) / Decimal(gross) * Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP)


def soft_lines(
    *,
    trade: int,
    gross: int,
    wd: int,
    chain: int,
    bank: int,
    leftover: int,
    kyc_changed: bool,
    holder_mismatch: bool,
    no_kyc: bool,
) -> list[str]:
    lines: list[str] = []
    pct = fee_pct(trade, gross)
    if pct < Decimal("0.20") or pct > Decimal("0.30"):
        lines.append(f"⚠️ ค่าซื้อขาย {pct:.2f}% ไม่อยู่ในช่วง 0.20–0.30% ของยอดขาย")
    if wd not in (0, 2_000, 10_000):
        lines.append("⚠️ ค่าถอนบาทไม่ใช่ 0, 20 หรือ 100")
    if chain > 1_000_000:
        lines.append("⚠️ ค่าเครือข่ายมากกว่า 1 USDT")
    if bank > 0:
        lines.append("⚠️ มีค่าโอนธนาคาร")
    if leftover >= 1_000_000:
        lines.append("⚠️ เศษยกไปตั้งแต่ 1 USDT")
    if kyc_changed:
        lines.append("⚠️ บัญชีรับเงินเปลี่ยนหลังยืนยัน — จะบันทึกบัญชีปัจจุบัน")
    if holder_mismatch:
        lines.append("⚠️ ชื่อบัญชีไม่ตรงกับชื่อจริง")
    if no_kyc:
        lines.append("⚠️ ยังไม่มีข้อมูลรับเงินในระบบ — ติดต่อแอดมิน")
    return lines


def figure_lines(
    *,
    name: str,
    kind: str,
    request_id: str,
    payout_micro: int,
    paid_at_ms: int,
    carry_micro: int,
    chain_micro: int,
    sold_micro: int,
    rate_e4: int,
    trade: int,
    wd: int,
    bank: int,
    net: int,
    gross: int,
    residual: int,
    leftover: int,
    kyc_line: str,
    title: str | None = None,
) -> list[str]:
    available = int(payout_micro) + int(carry_micro) - int(chain_micro)
    head = title or f"ตรวจก่อนบันทึกโอนเงินบาท · {name} · {_kind(kind)} #{short_id(request_id)}"
    calc = int(gross) - int(trade) - int(wd) - int(bank)
    shown = -int(residual)
    tol = "เกินเกณฑ์ ±1.00" if abs(int(residual)) > fx.RESIDUAL_TOL_SATANG else "เกณฑ์ ±1.00"
    pct = fee_pct(trade, gross)
    denom = int(payout_micro) + int(carry_micro) - int(leftover)
    lines = [
        head,
        f"ยอดถอน {_usdt_2(payout_micro)} USDT · USDT ออก {stamp_short(paid_at_ms)}",
        (
            f"เศษยกมา {_usdt_2(carry_micro)} · ค่าเครือข่าย {_MINUS}{_usdt_2(chain_micro).lstrip(_MINUS)}"
            f" → มีให้ขาย {_usdt_2(available)} USDT"
        ),
        f"ขาย {_sold_usdt(sold_micro)} USDT × {fmt_rate(rate_e4)} = {fmt_thb(gross)} บาท",
        (
            f"ค่าซื้อขาย {_MINUS}{fmt_thb(trade)} ({pct:.2f}%) · "
            f"ค่าถอนบาท {_MINUS}{fmt_thb(wd)} · ค่าโอนธนาคาร {fmt_thb(bank)}"
        ),
        f"คำนวณได้ {fmt_thb(calc)} · ตามสลิป {fmt_thb(net)} · ส่วนต่าง {fmt_thb(shown)} ({tol})",
        f"เฉลี่ยสุทธิ {fmt_effective(net, denom)} บาท/USDT",
        (f"เศษ {_usdt_2(leftover)} USDT ยกไปครั้งหน้า → ยอดเศษหลังบันทึก {_usdt_2(leftover)} USDT"),
        f"โอนเข้า: {kyc_line}" if not kyc_line.startswith("⚠️") else kyc_line,
        "เรทอ้างอิง: ยังไม่เปิดใช้",
    ]
    return lines


def preview_text(lines: list[str], warnings: list[str]) -> str:
    body = list(lines)
    body.extend(warnings)
    body.append("กดยืนยันภายใน 15 นาที")
    return "\n".join(body)


def fx_buttons(request_id: str, token: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ บันทึกโอนเงินบาท", "callback_data": f"pa:fy:{request_id}:{token}"},
                {"text": "✖️ ยกเลิก", "callback_data": f"pa:fn:{request_id}:{token}"},
            ]
        ]
    }


def resend_button(request_id: str, *, carry: bool = False) -> dict[str, Any]:
    data = f"pa:fc:{request_id}" if carry else f"pa:fs:{request_id}"
    return {"inline_keyboard": [[{"text": "📨 ส่งแจ้งพาร์ทเนอร์อีกครั้ง", "callback_data": data}]]}


def code_th(code: str, *, available_micro: int | None = None, carry: bool = False) -> str:
    if code == "sold_not_step":
        return "จำนวนขายต้องเป็นจำนวนเต็ม USDT"
    if code == "sold_below_min":
        return _SOLD_MIN
    if code == "sold_exceeds_available":
        have = _usdt_2(available_micro or 0)
        return f"ขายเกินยอดที่มี (มี {have} USDT)"
    if code == "rate_out_of_bounds":
        return "เรทต้องอยู่ระหว่าง 20–60 บาท/USDT"
    if code == "not_paid":
        return "ยังไม่ได้กด 📤"
    if code == "bad_kind":
        return "รายการนี้ไม่มีเงินออก"
    if code == "not_current":
        return "มีฉบับแก้ไขใหม่กว่าแล้ว — พิมพ์ p fix ใหม่"
    if code == "carry_mismatch":
        return _CARRY_CHANGED_SETTLE if carry else _CARRY_CHANGED
    if code == "carry_consumed":
        return _CARRY_LOCKED
    if code == "settle_exceeds":
        return _SETTLE_EXCEEDS
    if code == "kyc_changed":
        return _KYC_CHANGED
    if code in ("caller_transaction", "negative_balance"):
        logger.error("partner thb bug code=%s", code)
        return _BUG
    if code == "draft_gone":
        return _EXPIRED
    if code in ("db_constraint", "bad_input"):
        return _DB
    return _BUG


def _figures(req: dict[str, Any], parsed: dict[str, Any], carry: int, *, correction: bool) -> dict[str, Any]:
    return fx.compute(
        usdt_payout_micro=int(req["payout_micro"]),
        carry_in_micro=carry,
        chain_fee_micro=int(parsed["chain"]),
        usdt_sold_micro=int(parsed["sold"]),
        rate=parsed["rate"],
        trade_fee_satang=int(parsed["trade"]),
        thb_wd_fee_satang=int(parsed["wd"]),
        bank_fee_satang=int(parsed["bank"]),
        net_satang=int(parsed["net"]),
        correction=correction,
    )


def _hard_code(figured: dict[str, Any]) -> str | None:
    for code in figured.get("refusals") or []:
        if code != "reason_required":
            return str(code)
    if figured.get("gross_satang") is None or figured.get("rate_e4") is None:
        if "rate_out_of_bounds" in (figured.get("refusals") or []):
            return "rate_out_of_bounds"
        return "too_many_decimals"
    return None


def handle_paid_text(
    conn: Any,
    text: str,
    *,
    send: Any,
    edit: Any,
    entered_by: str,
    now_ms: int,
) -> dict[str, Any]:
    parsed = parse_paid_command(text)
    if parsed == "usage":
        send(_PAID_USAGE)
        return {"ok": False, "reason": "usage"}
    if parsed == "decimals" or not isinstance(parsed, dict):
        send(_DECIMALS)
        return {"ok": False, "reason": "decimals"}
    found = resolve_paid_target(conn, str(parsed["target"]))
    if found.get("refusal"):
        send(str(found["refusal"]))
        return {"ok": False, "reason": "target"}
    req = found["request"]
    partner = found["partner"]
    pid = str(req["partner_id"])
    carry = fx.carry_balance(conn, pid)
    figured = _figures(req, parsed, carry, correction=False)
    hard = _hard_code(figured)
    name = str(partner["name"]) if partner is not None else "?"
    kyc = kyc_bits(conn, pid)
    if hard is not None:
        send(
            _with_unsaved(
                code_th(hard, available_micro=int(req["payout_micro"]) + carry - int(parsed["chain"]))
            )
        )
        return {"ok": False, "reason": hard}
    if int(parsed["sold"]) == 0 or figured.get("gross_satang") is None:
        send(_with_unsaved(_SOLD_MIN))
        return {"ok": False, "reason": "sold_below_min"}
    lines = figure_lines(
        name=name,
        kind=str(req["kind"]),
        request_id=str(req["id"]),
        payout_micro=int(req["payout_micro"]),
        paid_at_ms=int(req["paid_at_ms"] or now_ms),
        carry_micro=carry,
        chain_micro=int(parsed["chain"]),
        sold_micro=int(parsed["sold"]),
        rate_e4=int(figured["rate_e4"]),
        trade=int(parsed["trade"]),
        wd=int(parsed["wd"]),
        bank=int(parsed["bank"]),
        net=int(parsed["net"]),
        gross=int(figured["gross_satang"]),
        residual=int(figured["residual_satang"] or 0),
        leftover=int(figured["leftover_micro"]),
        kyc_line=kyc["line"],
    )
    if "reason_required" in (figured.get("refusals") or []) and len(str(parsed["reason"]).strip()) < 10:
        send("\n".join(lines + [_REASON_NEED]))
        return {"ok": False, "reason": "reason_required"}
    stored_payout_kyc = req["payout_kyc_id"] if "payout_kyc_id" in req.keys() else None
    kyc_changed = (
        stored_payout_kyc is not None and kyc["id"] is not None and str(stored_payout_kyc) != str(kyc["id"])
    )
    warnings = soft_lines(
        trade=int(parsed["trade"]),
        gross=int(figured["gross_satang"]),
        wd=int(parsed["wd"]),
        chain=int(parsed["chain"]),
        bank=int(parsed["bank"]),
        leftover=int(figured["leftover_micro"]),
        kyc_changed=kyc_changed,
        holder_mismatch=bool(kyc["holder_mismatch"]),
        no_kyc=kyc["id"] is None,
    )
    old = fx.fx_draft_row(conn, str(req["id"]))
    payload = {
        "op": "paid",
        "sold": str(parsed["sold"]),
        "rate": str(parsed["rate"]),
        "trade": str(parsed["trade"]),
        "net": str(parsed["net"]),
        "chain": str(parsed["chain"]),
        "wd": str(parsed["wd"]),
        "bank": str(parsed["bank"]),
        "carry_in": str(carry),
        "paid_kyc_id": "" if kyc["id"] is None else kyc["id"],
        "payout_micro": str(req["payout_micro"]),
        "reason": str(parsed["reason"]),
        "slip": parsed["slip"] or "",
        "sale": parsed["sale"] or "",
        "ref": {"status": "off"},
    }
    staged = fx.stage_fx_draft(
        conn,
        request_id=str(req["id"]),
        mode="paid",
        payload=payload,
        created_by=entered_by,
        now_ms=now_ms,
    )
    if not staged.get("ok"):
        send(_with_unsaved(_DB))
        return {"ok": False, "reason": staged.get("reason")}
    token = str(staged["token"])
    body = preview_text(lines, warnings)
    sent = send(body, reply_markup=fx_buttons(str(req["id"]), token))
    message_id = sent if isinstance(sent, int) else None
    if message_id is not None:
        fx.set_fx_draft_message_id(conn, str(req["id"]), token, message_id)
    if old is not None and old.get("admin_message_id") is not None:
        try:
            edit(int(old["admin_message_id"]), "ถูกแทนด้วยรายการใหม่", reply_markup={"inline_keyboard": []})
        except Exception:  # noqa: BLE001
            logger.info("partner thb old preview edit failed request_id=%s", req["id"])
    return {"ok": True, "cmd": "paid_preview", "request_id": str(req["id"]), "token": token}


def partner_notice(conn: Any, request_id: str) -> str:
    """Partner THB notice rendered from the stored row. Never shows a reference rate."""
    row = fx.current_fx(conn, request_id)
    if row is None:
        return ""
    req = ps.get_request(conn, request_id)
    kind = str(req["kind"]) if req is not None else "withdraw_profit"
    partner = ps.get_partner(conn, str(req["partner_id"])) if req is not None else None
    closed = partner is not None and str(partner["status"]) == "closed"
    lines = [f"💸 โอนเงินบาทเข้าบัญชีของคุณแล้ว · {_kind(kind)}"]
    lines.append(f"ยอดถอน {_usdt_2(int(row['usdt_payout_micro']))} USDT")
    if int(row["carry_in_micro"]) > 0:
        lines.append(f"เศษยกมาจากครั้งก่อน +{_usdt_2(int(row['carry_in_micro']))} USDT")
    if int(row["chain_fee_micro"]) > 0:
        lines.append(f"ค่าธรรมเนียมเครือข่าย {_MINUS}{_usdt_2(int(row['chain_fee_micro']))} USDT")
    lines.append(
        f"ขาย {_sold_usdt(int(row['usdt_sold_micro']))} USDT × {fmt_rate(int(row['rate_e4']))} = "
        f"{fmt_thb(int(row['gross_satang']))} บาท"
    )
    if int(row["trade_fee_satang"]) > 0:
        lines.append(f"ค่าธรรมเนียมซื้อขาย {_MINUS}{fmt_thb(int(row['trade_fee_satang']))} บาท")
    if int(row["thb_wd_fee_satang"]) > 0:
        lines.append(f"ค่าถอนเงินบาท {_MINUS}{fmt_thb(int(row['thb_wd_fee_satang']))} บาท")
    if int(row["bank_fee_satang"]) > 0:
        lines.append(f"ค่าโอนธนาคาร {_MINUS}{fmt_thb(int(row['bank_fee_satang']))} บาท")
    if int(row["residual_satang"]) != 0:
        lines.append(f"ปัดเศษ/อื่น ๆ {fmt_thb(-int(row['residual_satang']))} บาท")
        if abs(int(row["residual_satang"])) > fx.RESIDUAL_TOL_SATANG and row["reason"]:
            lines.append(f"หมายเหตุ: {row['reason']}")
    denom = int(row["usdt_payout_micro"]) + int(row["carry_in_micro"]) - int(row["usdt_leftover_micro"])
    lines.append(
        f"รับสุทธิ {fmt_thb(int(row['net_satang']))} บาท "
        f"(เฉลี่ย {fmt_effective(int(row['net_satang']), denom)} บาท/USDT)"
    )
    if int(row["usdt_leftover_micro"]) > 0:
        separate = kind == "close" or (
            closed and req is not None and not _later_v1_fx(conn, str(req["partner_id"]), request_id)
        )
        if separate:
            lines.append(
                f"เศษ {_usdt_2(int(row['usdt_leftover_micro']))} USDT แอดมินจะแจ้งการชำระเศษนี้แยกต่างหาก"
            )
        else:
            lines.append(
                f"เศษ {_usdt_2(int(row['usdt_leftover_micro']))} USDT ยังไม่ได้ขาย "
                f"(ขายได้ขั้นละ 1 USDT) — ยกไปรวมกับการถอนครั้งถัดไป"
            )
    kyc_id = row["paid_kyc_id"]
    if kyc_id:
        mask = conn.execute(
            "SELECT bank_code, account_last4, holder_name_mask FROM partner_kyc WHERE id = ?",
            (str(kyc_id),),
        ).fetchone()
        if mask is not None:
            account = pk.mask_account(str(mask["bank_code"]), str(mask["account_last4"]))
            lines.append(f"โอนเข้า: {account} ({mask['holder_name_mask']})")
    slip = row["slip_ref"]
    stamp = stamp_full(int(row["entered_at_ms"]))
    if slip:
        lines.append(f"บันทึก {stamp} · อ้างอิงสลิป {slip}")
    else:
        lines.append(f"บันทึก {stamp}")
    return "\n".join(lines)


def _later_v1_fx(conn: Any, partner_id: str, request_id: str) -> bool:
    """True when another request has a version-1 fx row inserted after this one."""
    row = conn.execute(
        """
        SELECT 1
        FROM partner_payout_fx AS other
        WHERE other.partner_id = ?
          AND other.version = 1
          AND other.request_id != ?
          AND other.rowid > (
              SELECT rowid FROM partner_payout_fx
              WHERE request_id = ? AND version = 1
          )
        LIMIT 1
        """,
        (str(partner_id), str(request_id), str(request_id)),
    ).fetchone()
    return row is not None


def correction_notice(conn: Any, request_id: str, previous: dict[str, Any]) -> str:
    row = fx.current_fx(conn, request_id)
    if row is None:
        return ""
    req = ps.get_request(conn, request_id)
    kind = str(req["kind"]) if req is not None else "withdraw_profit"
    n = int(row["version"]) - 1
    lines = [
        f"✏️ แก้ไขรายการโอนเงินบาท (แก้ครั้งที่ {n}) · {_kind(kind)} {_usdt_2(int(row['usdt_payout_micro']))} USDT"
    ]
    labels = (
        ("chain_fee_micro", "ค่าธรรมเนียมเครือข่าย", "usdt"),
        ("usdt_sold_micro", "จำนวนขาย", "sold"),
        ("rate_e4", "เรท", "rate"),
        ("trade_fee_satang", "ค่าซื้อขาย", "thb"),
        ("thb_wd_fee_satang", "ค่าถอนบาท", "thb"),
        ("bank_fee_satang", "ค่าโอนธนาคาร", "thb"),
        ("net_satang", "รับสุทธิ", "thb"),
    )
    for key, label, kind_n in labels:
        if int(previous[key]) == int(row[key]):
            continue
        lines.append(_change_line(label, int(previous[key]), int(row[key]), kind_n))
    if int(previous["usdt_leftover_micro"]) != int(row["usdt_leftover_micro"]):
        lines.append(
            "เศษยกไปครั้งหน้า: "
            f"เดิม {_usdt_2(int(previous['usdt_leftover_micro']))} → "
            f"ใหม่ {_usdt_2(int(row['usdt_leftover_micro']))} USDT"
        )
    if row["reason"]:
        lines.append(f"เหตุผล: {row['reason']}")
    lines.append("— รายละเอียดใหม่ —")
    lines.append(partner_notice(conn, request_id))
    return "\n".join(lines)


def _plain_field(value: int, kind: str) -> str:
    if kind == "sold":
        return _sold_usdt(value)
    if kind == "usdt":
        return _usdt_2(value)
    if kind == "rate":
        return fmt_rate(value)
    return fmt_thb(value)


def _change_line(label: str, old: int, new: int, kind: str) -> str:
    unit = {"usdt": " USDT", "sold": " USDT", "thb": " บาท"}.get(kind, "")
    return f"{label}: เดิม {_plain_field(old, kind)} → ใหม่ {_plain_field(new, kind)}{unit}"


def step1_admin(conn: Any, request_id: str) -> str:
    req = ps.get_request(conn, request_id)
    if req is None:
        return ""
    partner = ps.get_partner(conn, str(req["partner_id"]))
    name = str(partner["name"]) if partner is not None else "?"
    payout = int(req["payout_micro"] or 0)
    if partner is not None and int(partner["is_admin"] or 0) == 1:
        return f"💸 โอนแล้ว {_usdt_2(payout)} USDT ให้ {name}"
    kyc = kyc_bits(conn, str(req["partner_id"]))
    account = kyc["line"] if kyc["id"] else "⚠️ ยังไม่มีข้อมูลรับเงิน"
    lines = [
        f"📤 USDT ออกจากพอร์ตแล้ว · {name} · {_kind(str(req['kind']))} {_usdt_2(payout)} USDT",
    ]
    if fx.unsellable(conn, req):
        lines.extend([f"บัญชีรับเงิน: {account}", _UNSELLABLE])
    else:
        carry = fx.carry_balance(conn, str(req["partner_id"]))
        chain = 10_000
        available = payout + carry - chain
        sold = (available // 1_000_000) * 1_000_000
        lines.extend(
            [
                (
                    f"ถ้าค่าเครือข่าย 0.01: มีให้ขาย {_usdt_2(available)} USDT "
                    f"(เศษยกมา {_usdt_2(carry)}) → ขายได้ {_sold_usdt(sold)} USDT"
                ),
                f"บัญชีรับเงิน: {account}",
                "ขายแล้วบันทึกด้วย:",
                f"p paid #{short_id(request_id)} sold= rate= trade= net=",
            ]
        )
    return "\n".join(lines)


def step1_partner(conn: Any, request_id: str) -> str:
    req = ps.get_request(conn, request_id)
    if req is None:
        return ""
    payout = int(req["payout_micro"] or 0)
    if fx.unsellable(conn, req):
        head = f"💸 แอดมินแจ้งว่าโอน {_usdt_2(payout)} USDT ให้คุณแล้ว"
        account = pk.partner_payout_line(
            conn, payout_kyc_id=req["payout_kyc_id"], cash=ps.usdt_from_micro(payout)
        )
        if not account:
            kyc = kyc_bits(conn, str(req["partner_id"]))
            account = "⚠️ ยังไม่มีข้อมูลรับเงินในระบบ — ติดต่อแอดมิน" if kyc["id"] is None else f"โอนเข้า: {kyc['line']}"
        return "\n".join([head, account])
    kind = str(req["kind"])
    if kind == "close":
        head = f"📤 ยอดปิดพอร์ต {_usdt_2(payout)} USDT ออกจากพอร์ตแล้ว"
    else:
        head = f"📤 ยอดถอนกำไร {_usdt_2(payout)} USDT ออกจากพอร์ตแล้ว"
    kyc = kyc_bits(conn, str(req["partner_id"]))
    if kyc["id"] is None:
        account = "⚠️ ยังไม่มีข้อมูลรับเงินในระบบ — ติดต่อแอดมิน"
    else:
        account = f"บัญชีที่จะรับเงิน: {kyc['line']}"
    return "\n".join(
        [
            head,
            "แอดมินกำลังแปลงเป็นเงินบาท และจะแจ้งยอดเงินบาทอีกครั้งเมื่อโอนเข้าบัญชีแล้ว",
            account,
        ]
    )


def step1_releases(conn: Any, request_id: str) -> bool:
    """A closed partner whose payout can never be sold is finished at step 1."""
    req = ps.get_request(conn, request_id)
    if req is None or not fx.unsellable(conn, req):
        return False
    partner = ps.get_partner(conn, str(req["partner_id"]))
    if partner is None or str(partner["status"]) != "closed":
        return False
    return not fx.open_settlements(conn, str(req["partner_id"]))


def confirm_callback(
    conn: Any,
    data: str,
    *,
    now_ms: int,
    entered_by: str,
    answer: Any,
    edit: Any,
    send: Any,
    notify: Any,
    release: Any,
    close_facts: Any,
    mid: Any,
) -> dict[str, Any]:
    """``pa:fy`` / ``pa:fn`` / ``pa:fs`` / ``pa:fc``. Post-commit work never says nothing was saved."""
    if data.startswith("pa:fc:"):
        rid = data[len("pa:fc:") :]
        if _REQ_RE.match(rid) is None:
            answer("รายการนี้ปิดไปแล้ว")
            return {"ok": False, "reason": "bad_id"}
        return _resend_carry(
            conn,
            rid,
            answer=answer,
            edit=edit,
            send=send,
            notify=notify,
            release=release,
            close_facts=close_facts,
            mid=mid,
            now_ms=now_ms,
        )
    if data.startswith("pa:fs:"):
        rid = data[len("pa:fs:") :]
        return _resend(
            conn,
            rid,
            answer=answer,
            edit=edit,
            send=send,
            notify=notify,
            release=release,
            close_facts=close_facts,
            mid=mid,
            now_ms=now_ms,
        )
    kind = "fy" if data.startswith("pa:fy:") else "fn" if data.startswith("pa:fn:") else ""
    if not kind:
        return {"ok": False, "reason": "unknown"}
    m = _FY_RE.match(data[len("pa:fy:") :] if kind == "fy" else data[len("pa:fn:") :])
    if m is None:
        answer("รายการนี้ปิดไปแล้ว")
        return {"ok": False, "reason": "bad_id"}
    rid, token = m.group(1), m.group(2)
    if kind == "fn":
        if not fx.delete_fx_draft(conn, rid, token=token):
            return _missing_draft(conn, rid, token, now_ms, answer=answer, edit=edit, send=send, mid=mid)
        answer()
        _edit_or_send(edit, send, mid, "ยกเลิกแล้ว", {"inline_keyboard": []})
        return {"ok": True, "cmd": "fx_cancel"}
    loaded = fx.load_fx_draft(conn, rid, token, now_ms)
    if loaded is None:
        return _missing_draft(conn, rid, token, now_ms, answer=answer, edit=edit, send=send, mid=mid)
    payload = loaded["payload"]
    op = str(payload.get("op") or loaded.get("mode") or "paid")
    req_row = ps.get_request(conn, rid)
    partner_id = "" if req_row is None else str(req_row["partner_id"])
    current = kyc_bits(conn, partner_id)
    stored_kyc = str(payload.get("paid_kyc_id") or "")
    current_id = "" if current["id"] is None else current["id"]
    if op == "paid" and stored_kyc != current_id:
        answer(_KYC_CHANGED)
        _edit_or_send(edit, send, mid, _with_unsaved(_KYC_CHANGED), {"inline_keyboard": []})
        return {"ok": False, "reason": "kyc_changed"}
    if op == "carry":
        if fx.carry_balance(conn, partner_id) != int(payload["amount_micro"]):
            fx.delete_fx_draft(conn, rid, token=token)
            answer(_CARRY_CHANGED_SETTLE)
            _edit_or_send(edit, send, mid, _with_unsaved(_CARRY_CHANGED_SETTLE), {"inline_keyboard": []})
            return {"ok": False, "reason": "carry_mismatch"}
        result = fx.settle_carry(
            conn,
            partner_id,
            int(payload["amount_micro"]),
            str(payload.get("reason") or ""),
            entered_by,
            now_ms,
            draft_token=token,
            draft_request_id=rid,
            thb_satang=int(payload["thb_satang"]),
            rate_e4=int(payload["rate_e4"]),
            expect_balance_micro=int(payload["amount_micro"]),
        )
    elif op == "fix":
        result = fx.record_correction(
            conn,
            base_fx_id=str(loaded["base_fx_id"]),
            carry_in_micro=int(payload["carry_in"]),
            chain_fee_micro=int(payload["chain"]),
            usdt_sold_micro=int(payload["sold"]),
            rate=Decimal(str(payload["rate"])),
            trade_fee_satang=int(payload["trade"]),
            thb_wd_fee_satang=int(payload["wd"]),
            bank_fee_satang=int(payload["bank"]),
            net_satang=int(payload["net"]),
            ref_status="off",
            reason=str(payload.get("reason") or "") or None,
            slip_ref=str(payload.get("slip") or "") or None,
            sale_ref=str(payload.get("sale") or "") or None,
            entered_by=entered_by,
            now_ms=now_ms,
            draft_token=token,
        )
    else:
        result = fx.record_payout_fx(
            conn,
            request_id=rid,
            carry_in_micro=int(payload["carry_in"]),
            chain_fee_micro=int(payload["chain"]),
            usdt_sold_micro=int(payload["sold"]),
            rate=Decimal(str(payload["rate"])),
            trade_fee_satang=int(payload["trade"]),
            thb_wd_fee_satang=int(payload["wd"]),
            bank_fee_satang=int(payload["bank"]),
            net_satang=int(payload["net"]),
            ref_status="off",
            reason=str(payload.get("reason") or "") or None,
            slip_ref=str(payload.get("slip") or "") or None,
            sale_ref=str(payload.get("sale") or "") or None,
            paid_kyc_id=None if stored_kyc == "" else stored_kyc,
            entered_by=entered_by,
            now_ms=now_ms,
            draft_token=token,
        )
    if not result.get("ok"):
        code = str(result.get("reason") or "")
        if code in ("caller_transaction", "negative_balance"):
            logger.error("partner thb bug request_id=%s partner_id=%s code=%s", rid, partner_id, code)
        text = code_th(code, available_micro=_available(payload), carry=op == "carry")
        answer(text)
        _edit_or_send(edit, send, mid, _with_unsaved(text), {"inline_keyboard": []})
        return {"ok": False, "reason": code}
    try:
        answer("บันทึกแล้ว")
        return _after_commit(
            conn,
            rid,
            partner_id,
            op=op,
            previous=payload.get("previous"),
            answer=answer,
            edit=edit,
            send=send,
            notify=notify,
            release=release,
            close_facts=close_facts,
            mid=mid,
            now_ms=now_ms,
            already_toasted=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("partner thb post-commit failed request_id=%s", rid)
        answer("บันทึกแล้ว แต่ส่งข้อความไม่ครบ — กด 📨")
        _edit_or_send(
            edit, send, mid, "บันทึกแล้ว แต่ส่งข้อความไม่ครบ — กด 📨", resend_button(rid, carry=op == "carry")
        )
        return {"ok": True, "cmd": "fx_saved_partial", "request_id": rid}


def _available(payload: dict[str, Any]) -> int:
    try:
        return (
            int(payload.get("payout_micro") or 0)
            + int(payload.get("carry_in") or 0)
            - int(payload.get("chain") or 0)
        )
    except (TypeError, ValueError):
        return 0


def _missing_draft(
    conn: Any, rid: str, token: str, now_ms: int, *, answer: Any, edit: Any, send: Any, mid: Any
) -> dict[str, Any]:
    row = fx.fx_draft_row(conn, rid)
    current = fx.current_fx(conn, rid)
    if row is not None and str(row["token"]) == token and int(row["expires_at_ms"]) <= int(now_ms):
        answer(_EXPIRED)
        _edit_or_send(edit, send, mid, _EXPIRED, {"inline_keyboard": []})
        return {"ok": False, "reason": "expired"}
    if row is not None and str(row["token"]) != token:
        answer("มีรายการใหม่แทนแล้ว")
        _edit_or_send(edit, send, mid, "มีรายการใหม่แทนแล้ว", {"inline_keyboard": []})
        return {"ok": False, "reason": "replaced"}
    if row is None:
        carry_line = _carry_settled_line(conn, rid)
        if carry_line is not None:
            answer("บันทึกไปแล้ว")
            _edit_or_send(edit, send, mid, carry_line, resend_button(rid, carry=True))
            return {"ok": False, "reason": "already"}
    if row is None and current is not None:
        answer("บันทึกไปแล้ว")
        _edit_or_send(edit, send, mid, _settled_line(conn, rid), resend_button(rid))
        return {"ok": False, "reason": "already"}
    answer("ยกเลิกหรือหมดเวลาแล้ว")
    _edit_or_send(edit, send, mid, "ยกเลิกหรือหมดเวลาแล้ว", {"inline_keyboard": []})
    return {"ok": False, "reason": "gone"}


def _settled_line(conn: Any, rid: str) -> str:
    req = ps.get_request(conn, rid)
    partner = ps.get_partner(conn, str(req["partner_id"])) if req is not None else None
    name = str(partner["name"]) if partner is not None else "?"
    row = fx.current_fx(conn, rid)
    if row is None:
        return "บันทึกไปแล้ว"
    return f"💸 บันทึกโอนเงินบาทแล้ว · {name} · {fmt_thb(int(row['net_satang']))} บาท (#{short_id(rid)})"


def _newest_carry(conn: Any, partner_id: str) -> Any:
    return conn.execute(
        """
        SELECT kind, amount_micro, reason, created_at_ms
        FROM partner_usdt_carry
        WHERE partner_id = ?
        ORDER BY seq DESC
        LIMIT 1
        """,
        (str(partner_id),),
    ).fetchone()


def _carry_card_text(name: str, amount_micro: int, reason: str) -> str:
    matched = _SETTLE_REASON_RE.match(reason)
    thb = matched.group(1) if matched is not None else "—"
    return f"💸 ชำระเศษแล้ว · {name} · {_usdt_2(abs(int(amount_micro)))} USDT = {thb} บาท"


def _carry_settled_line(conn: Any, rid: str) -> str | None:
    """Carry card only on the anchor, when its newest carry row is a new enough settle."""
    req = ps.get_request(conn, rid)
    if req is None:
        return None
    partner_id = str(req["partner_id"])
    if str(rid) != str(_carry_anchor(conn, partner_id) or ""):
        return None
    row = _newest_carry(conn, partner_id)
    if row is None or str(row["kind"]) != "settle":
        return None
    current = fx.current_fx(conn, rid)
    if current is not None and int(row["created_at_ms"]) < int(current["entered_at_ms"]):
        return None
    partner = ps.get_partner(conn, str(req["partner_id"]))
    name = str(partner["name"]) if partner is not None else "?"
    return _carry_card_text(name, int(row["amount_micro"]), str(row["reason"] or ""))


def carry_notice_from_db(conn: Any, partner_id: str) -> str:
    """Partner carry notice from the latest settle row, not from the draft payload."""
    row = conn.execute(
        """
        SELECT amount_micro, reason FROM partner_usdt_carry
        WHERE partner_id = ? AND kind = 'settle'
        ORDER BY seq DESC LIMIT 1
        """,
        (str(partner_id),),
    ).fetchone()
    if row is None:
        return "💸 ชำระเศษเป็นเงินบาท"
    usdt = _usdt_2(abs(int(row["amount_micro"])))
    reason = str(row["reason"] or "")
    matched = _SETTLE_REASON_RE.match(reason)
    if matched is not None:
        lines = [f"💸 ชำระเศษ {usdt} USDT เป็นเงินบาท {matched.group(1)} บาท", matched.group(3)]
    else:
        lines = [f"💸 ชำระเศษ {usdt} USDT เป็นเงินบาท"]
        if reason:
            lines.append(reason)
    return "\n".join(lines)


def _after_commit(
    conn: Any,
    rid: str,
    partner_id: str,
    *,
    op: str,
    previous: Any,
    answer: Any,
    edit: Any,
    send: Any,
    notify: Any,
    release: Any,
    close_facts: Any,
    mid: Any,
    now_ms: int,
    already_toasted: bool,
) -> dict[str, Any]:
    if not already_toasted:
        answer("บันทึกแล้ว")
    partner = ps.get_partner(conn, partner_id)
    carry_head = _carry_settled_line(conn, rid) if op == "carry" else None
    if op == "carry":
        note = carry_notice_from_db(conn, partner_id)
    elif op == "fix" and isinstance(previous, dict):
        note = correction_notice(conn, rid, previous)
    else:
        note = partner_notice(conn, rid)
    sent_ok = False
    no_chat = partner is None or partner["telegram_user_id"] is None
    if (
        partner is not None
        and str(partner["status"]) == "closed"
        and not fx.open_settlements(conn, partner_id)
    ):
        facts = close_facts(conn, partner_id)
        if facts:
            note += "\n" + "\n".join(facts)
        if fx.carry_balance(conn, partner_id) <= 0:
            note += "\nปิดพอร์ตเรียบร้อย บอทจะไม่แสดงยอดอีก"
        if no_chat:
            sent_ok = False
        else:
            warnings = release(
                conn, partner, send=notify and _notify_send(notify, partner), note=note, now_ms=now_ms
            )
            sent_ok = not warnings
    elif partner is not None and not no_chat:
        sent_ok = bool(notify(partner, note))
    head = carry_head or _settled_line(conn, rid)
    if sent_ok:
        card = head + "\n✅ แจ้งพาร์ทเนอร์แล้ว"
        markup: dict[str, Any] = {"inline_keyboard": []}
    elif _resend_finished(conn, partner, already_toasted=already_toasted):
        card = head + "\nℹ️ พาร์ทเนอร์ไม่ได้ผูกแชทแล้ว — ไม่ต้องส่งซ้ำ"
        markup = {"inline_keyboard": []}
    else:
        card = head + "\n⚠️ แจ้งพาร์ทเนอร์ไม่สำเร็จ — บันทึกแล้ว"
        markup = resend_button(rid, carry=op == "carry")
    edited = False
    if mid is not None:
        try:
            edited = bool(edit(int(mid), card, reply_markup=markup))
        except Exception:  # noqa: BLE001
            edited = False
    if not edited:
        send(card, reply_markup=markup)
    return {"ok": True, "cmd": "fx_saved", "request_id": rid, "notified": sent_ok}


def _resend_finished(conn: Any, partner: Any, *, already_toasted: bool) -> bool:
    """True when a later 📨 tap has nobody left to tell and nothing left open."""
    if already_toasted or partner is None:
        return False
    if partner["telegram_user_id"] is not None or str(partner["status"]) != "closed":
        return False
    return not fx.open_settlements(conn, str(partner["id"]))


def _notify_send(notify: Any, partner: Any) -> Any:
    def _send(text: str, **kwargs: Any) -> Any:
        return notify(partner, text)

    return _send


def _resend(conn: Any, rid: str, **kwargs: Any) -> dict[str, Any]:
    req = ps.get_request(conn, rid)
    if req is None:
        kwargs["answer"]("ไม่พบรายการ")
        return {"ok": False, "reason": "not_found"}
    history = fx.fx_history(conn, rid)
    previous = history[-2] if len(history) >= 2 else None
    return _after_commit(
        conn,
        rid,
        str(req["partner_id"]),
        op="fix" if previous is not None else "paid",
        previous=previous,
        answer=kwargs["answer"],
        edit=kwargs["edit"],
        send=kwargs["send"],
        notify=kwargs["notify"],
        release=kwargs["release"],
        close_facts=kwargs["close_facts"],
        mid=kwargs["mid"],
        now_ms=kwargs["now_ms"],
        already_toasted=False,
    )


def _resend_carry(conn: Any, rid: str, **kwargs: Any) -> dict[str, Any]:
    req = ps.get_request(conn, rid)
    if req is None:
        kwargs["answer"]("ไม่พบรายการ")
        return {"ok": False, "reason": "not_found"}
    return _after_commit(
        conn,
        rid,
        str(req["partner_id"]),
        op="carry",
        previous=None,
        answer=kwargs["answer"],
        edit=kwargs["edit"],
        send=kwargs["send"],
        notify=kwargs["notify"],
        release=kwargs["release"],
        close_facts=kwargs["close_facts"],
        mid=kwargs["mid"],
        now_ms=kwargs["now_ms"],
        already_toasted=False,
    )


def _edit_or_send(edit: Any, send: Any, mid: Any, text: str, markup: dict[str, Any]) -> None:
    edited = False
    if mid is not None:
        try:
            edited = bool(edit(int(mid), text, reply_markup=markup))
        except Exception:  # noqa: BLE001
            edited = False
    if not edited:
        send(text, reply_markup=markup)


def handle_fix_text(
    conn: Any, text: str, *, send: Any, edit: Any, entered_by: str, now_ms: int
) -> dict[str, Any]:
    first, _, rest = (text or "").partition("\n")
    parts = first.strip().split()
    if len(parts) < 3:
        send(_FIX_USAGE)
        return {"ok": False, "reason": "usage"}
    target = parts[2]
    if not (_HEX6_RE.match(target.lower()) or _REQ_RE.match(target.lower())):
        partner = _partner_by_name(conn, target)
        if partner is None:
            send(_FIX_USAGE)
            return {"ok": False, "reason": "usage"}
        rows = conn.execute(
            """
            SELECT request_id, MAX(version) AS v
            FROM partner_payout_fx
            WHERE partner_id = ?
            GROUP BY request_id
            ORDER BY MAX(entered_at_ms) DESC
            LIMIT 5
            """,
            (str(partner["id"]),),
        ).fetchall()
        if not rows:
            send(f"{partner['name']} ยังไม่มีรายการเงินบาท")
            return {"ok": True, "cmd": "fix_list", "n": 0}
        lines = [f"รายการเงินบาทของ {partner['name']}"]
        for row in rows:
            current = fx.current_fx(conn, str(row["request_id"]))
            if current is None:
                continue
            lines.append(
                f"· #{short_id(str(row['request_id']))} · {fmt_thb(int(current['net_satang']))} บาท · v{current['version']}"
            )
        send("\n".join(lines))
        return {"ok": True, "cmd": "fix_list", "n": len(rows)}
    found = _resolve_fx_target(conn, target)
    if found.get("refusal"):
        send(str(found["refusal"]))
        return {"ok": False, "reason": "target"}
    current = found["fx"]
    req = found["request"]
    reason = rest.strip()
    if len(reason) < 10:
        send(_REASON_NEED)
        return {"ok": False, "reason": "reason_required"}
    pairs = _fix_pairs(parts[3:])
    if pairs is None:
        send(_FIX_USAGE)
        return {"ok": False, "reason": "usage"}
    if any(key in pairs and len(pairs[key]) > 40 for key in ("slip", "sale")):
        send(_with_unsaved(_SLIP_LONG))
        return {"ok": False, "reason": "slip_long"}
    parsed = _merge_fix(current, pairs)
    if parsed is None:
        send(_DECIMALS)
        return {"ok": False, "reason": "decimals"}
    if _unchanged(current, parsed):
        send(_NOTHING_CHANGED)
        return {"ok": False, "reason": "unchanged"}
    figured = _figures(req, parsed, int(current["carry_in_micro"]), correction=True)
    hard = _hard_code(figured)
    if hard:
        send(_with_unsaved(code_th(hard)))
        return {"ok": False, "reason": hard}
    new_left = int(figured["leftover_micro"])
    if fx.carry_locked(conn, str(req["id"])) and new_left != int(current["usdt_leftover_micro"]):
        send(_with_unsaved(_CARRY_LOCKED))
        return {"ok": False, "reason": "carry_consumed"}
    payload = {
        "op": "fix",
        "sold": str(parsed["sold"]),
        "rate": str(parsed["rate"]),
        "trade": str(parsed["trade"]),
        "net": str(parsed["net"]),
        "chain": str(parsed["chain"]),
        "wd": str(parsed["wd"]),
        "bank": str(parsed["bank"]),
        "carry_in": str(current["carry_in_micro"]),
        "paid_kyc_id": "" if current["paid_kyc_id"] is None else str(current["paid_kyc_id"]),
        "payout_micro": str(current["usdt_payout_micro"]),
        "reason": reason[:200],
        "slip": parsed["slip"] or "",
        "sale": parsed["sale"] or "",
        "ref": {"status": "off"},
        "previous": {
            key: int(current[key])
            for key in (
                "chain_fee_micro",
                "usdt_sold_micro",
                "rate_e4",
                "trade_fee_satang",
                "thb_wd_fee_satang",
                "bank_fee_satang",
                "net_satang",
                "usdt_leftover_micro",
            )
        },
    }
    old = fx.fx_draft_row(conn, str(req["id"]))
    staged = fx.stage_fx_draft(
        conn,
        request_id=str(req["id"]),
        mode="fix",
        payload=payload,
        created_by=entered_by,
        now_ms=now_ms,
        base_fx_id=str(current["id"]),
    )
    if not staged.get("ok"):
        send(_with_unsaved(_DB))
        return {"ok": False, "reason": staged.get("reason")}
    partner = ps.get_partner(conn, str(req["partner_id"]))
    name = str(partner["name"]) if partner is not None else "?"
    kyc = kyc_bits(conn, str(req["partner_id"]))
    lines = figure_lines(
        name=name,
        kind=str(req["kind"]),
        request_id=str(req["id"]),
        payout_micro=int(current["usdt_payout_micro"]),
        paid_at_ms=int(req["paid_at_ms"] or now_ms),
        carry_micro=int(current["carry_in_micro"]),
        chain_micro=int(parsed["chain"]),
        sold_micro=int(parsed["sold"]),
        rate_e4=int(figured["rate_e4"]),
        trade=int(parsed["trade"]),
        wd=int(parsed["wd"]),
        bank=int(parsed["bank"]),
        net=int(parsed["net"]),
        gross=int(figured["gross_satang"]),
        residual=int(figured["residual_satang"] or 0),
        leftover=int(figured["leftover_micro"]),
        kyc_line=kyc["line"],
        title=f"ตรวจก่อนแก้ไข · {name} · #{short_id(str(req['id']))}",
    )
    lines.extend(_fix_change_lines(current, parsed, figured))
    lines.append(f"เหตุผล: {reason[:200]}")
    token = str(staged["token"])
    _remember_preview(
        conn,
        str(req["id"]),
        token,
        preview_text(lines, []),
        fx_buttons(str(req["id"]), token),
        send=send,
        edit=edit,
        old=old,
    )
    return {"ok": True, "cmd": "fix_preview", "request_id": str(req["id"]), "token": token}


def _fix_change_lines(current: dict[str, Any], parsed: dict[str, Any], figured: dict[str, Any]) -> list[str]:
    pairs = (
        ("chain_fee_micro", int(parsed["chain"]), "ค่าธรรมเนียมเครือข่าย", "usdt"),
        ("usdt_sold_micro", int(parsed["sold"]), "จำนวนขาย", "sold"),
        ("rate_e4", int(figured["rate_e4"]), "เรท", "rate"),
        ("trade_fee_satang", int(parsed["trade"]), "ค่าซื้อขาย", "thb"),
        ("thb_wd_fee_satang", int(parsed["wd"]), "ค่าถอนบาท", "thb"),
        ("bank_fee_satang", int(parsed["bank"]), "ค่าโอนธนาคาร", "thb"),
        ("net_satang", int(parsed["net"]), "รับสุทธิ", "thb"),
        ("usdt_leftover_micro", int(figured["leftover_micro"]), "เศษยกไปครั้งหน้า", "usdt"),
    )
    lines: list[str] = []
    for key, new, label, kind in pairs:
        old = int(current[key])
        if old == new:
            continue
        lines.append(_change_line(label, old, new, kind))
    return lines


def _remember_preview(
    conn: Any,
    request_id: str,
    token: str,
    body: str,
    markup: dict[str, Any],
    *,
    send: Any,
    edit: Any,
    old: dict[str, Any] | None,
) -> None:
    sent = send(body, reply_markup=markup)
    if isinstance(sent, int):
        fx.set_fx_draft_message_id(conn, request_id, token, sent)
    if old is not None and old.get("admin_message_id") is not None and edit is not None:
        try:
            edit(int(old["admin_message_id"]), "ถูกแทนด้วยรายการใหม่", reply_markup={"inline_keyboard": []})
        except Exception:  # noqa: BLE001
            logger.info("partner thb old preview edit failed request_id=%s", request_id)


def _fix_pairs(bits: list[str]) -> dict[str, str] | None:
    pairs: dict[str, str] = {}
    for bit in bits:
        if "=" not in bit:
            return None
        key, _, value = bit.partition("=")
        key = key.lower()
        if key not in _PAID_KEYS or key in pairs or not value:
            return None
        pairs[key] = value
    return pairs


def _merge_fix(current: dict[str, Any], pairs: dict[str, str]) -> dict[str, Any] | None:
    def keep_usdt(key: str, column: str) -> int | None:
        if key not in pairs:
            return int(current[column])
        return _money(pairs[key], "usdt")

    sold = keep_usdt("sold", "usdt_sold_micro")
    chain = keep_usdt("chain", "chain_fee_micro")
    if "rate" in pairs:
        rate_e4 = _money(pairs["rate"], "rate")
        rate_text = _clean_number(pairs["rate"])
        rate = None if rate_text is None else Decimal(rate_text)
    else:
        rate_e4 = int(current["rate_e4"])
        rate = Decimal(rate_e4) / Decimal(10_000)
    trade = int(current["trade_fee_satang"]) if "trade" not in pairs else _money(pairs["trade"], "thb")
    wd = int(current["thb_wd_fee_satang"]) if "wd" not in pairs else _money(pairs["wd"], "thb")
    bank = int(current["bank_fee_satang"]) if "bank" not in pairs else _money(pairs["bank"], "thb")
    net = int(current["net_satang"]) if "net" not in pairs else _money(pairs["net"], "thb")
    if None in (sold, chain, rate_e4, rate, trade, wd, bank, net):
        return None
    assert sold is not None and chain is not None and rate_e4 is not None and rate is not None
    assert trade is not None and wd is not None and bank is not None and net is not None
    return {
        "sold": int(sold),
        "chain": int(chain),
        "rate": rate,
        "rate_e4": int(rate_e4),
        "trade": int(trade),
        "wd": int(wd),
        "bank": int(bank),
        "net": int(net),
        "slip": pairs.get("slip", current["slip_ref"]),
        "sale": pairs.get("sale", current["sale_ref"]),
    }


def _unchanged(current: dict[str, Any], parsed: dict[str, Any]) -> bool:
    return (
        int(parsed["sold"]) == int(current["usdt_sold_micro"])
        and int(parsed["chain"]) == int(current["chain_fee_micro"])
        and int(parsed["rate_e4"]) == int(current["rate_e4"])
        and int(parsed["trade"]) == int(current["trade_fee_satang"])
        and int(parsed["wd"]) == int(current["thb_wd_fee_satang"])
        and int(parsed["bank"]) == int(current["bank_fee_satang"])
        and int(parsed["net"]) == int(current["net_satang"])
    )


def _resolve_fx_target(conn: Any, target: str) -> dict[str, Any]:
    raw = target.strip().lower()
    if _REQ_RE.match(raw):
        ids = [raw]
    else:
        m = _HEX6_RE.match(raw)
        suffix = "" if m is None else m.group(1)
        ids = [
            str(row["request_id"])
            for row in conn.execute("SELECT DISTINCT request_id FROM partner_payout_fx").fetchall()
            if str(row["request_id"]).endswith(suffix)
        ]
    if len(ids) != 1:
        return {"refusal": "ไม่พบรายการ หรือรหัสซ้ำ — ใส่รหัสเต็ม"}
    current = fx.current_fx(conn, ids[0])
    req = ps.get_request(conn, ids[0])
    if current is None or req is None:
        return {"refusal": "ไม่พบรายการ"}
    return {"fx": current, "request": {key: req[key] for key in req.keys()}}


def handle_carry_text(
    conn: Any, text: str, *, send: Any, edit: Any, entered_by: str, now_ms: int
) -> dict[str, Any]:
    first, _, rest = (text or "").partition("\n")
    parts = first.strip().split()
    if len(parts) != 6 or parts[3].lower() != "settle":
        send(_CARRY_USAGE)
        return {"ok": False, "reason": "usage"}
    partner = _partner_by_name(conn, parts[2])
    if partner is None or int(partner["is_admin"] or 0) == 1:
        send("ไม่พบพาร์ทเนอร์")
        return {"ok": False, "reason": "not_found"}
    amount = _money(parts[4], "usdt")
    rate = _money(parts[5], "rate")
    if amount is None or rate is None:
        send(_DECIMALS)
        return {"ok": False, "reason": "decimals"}
    if int(rate) < fx.RATE_MIN_E4 or int(rate) > fx.RATE_MAX_E4:
        send(_with_unsaved(code_th("rate_out_of_bounds")))
        return {"ok": False, "reason": "rate_out_of_bounds"}
    if str(partner["status"]) != "closed":
        send(_with_unsaved(_CARRY_NOT_CLOSED.format(name=str(partner["name"]))))
        return {"ok": False, "reason": "not_closed"}
    reason = rest.strip()
    if len(reason) < 10:
        send(_REASON_NEED)
        return {"ok": False, "reason": "reason_required"}
    pid = str(partner["id"])
    open_rows = fx.open_settlements(conn, pid)
    if any(row.get("stage") != "carry_closed" for row in open_rows):
        send(_with_unsaved(_CARRY_OPEN.format(name=str(partner["name"]))))
        return {"ok": False, "reason": "open_settlement"}
    balance = fx.carry_balance(conn, pid)
    if int(amount) != balance:
        send(_with_unsaved("ยอดเศษไม่ตรงกับที่ระบุ"))
        return {"ok": False, "reason": "carry_mismatch"}
    thb_satang = int(
        (
            Decimal(int(amount)) / Decimal(1_000_000) * (Decimal(int(rate)) / Decimal(10_000)) * Decimal(100)
        ).quantize(Decimal(1), rounding=ROUND_UP)
    )
    anchor = _carry_anchor(conn, pid)
    if anchor is None:
        send(_with_unsaved("ไม่มีรายการให้ผูกแบบยืนยัน"))
        return {"ok": False, "reason": "no_anchor"}
    payload = {
        "op": "carry",
        "amount_micro": str(amount),
        "thb_satang": str(thb_satang),
        "rate_e4": str(rate),
        "reason": reason[:200],
    }
    old = fx.fx_draft_row(conn, anchor)
    staged = fx.stage_fx_draft(
        conn,
        request_id=anchor,
        mode="fix",
        payload=payload,
        created_by=entered_by,
        now_ms=now_ms,
    )
    if not staged.get("ok"):
        send(_with_unsaved(_DB))
        return {"ok": False, "reason": staged.get("reason")}
    token = str(staged["token"])
    body = "\n".join(
        [
            f"ตรวจก่อนชำระเศษ · {partner['name']}",
            f"เศษ {_usdt_2(int(amount))} USDT × {fmt_rate(int(rate))} = {fmt_thb(thb_satang)} บาท",
            f"เหตุผล: {reason[:200]}",
            "กดยืนยันภายใน 15 นาที",
        ]
    )
    _remember_preview(conn, anchor, token, body, fx_buttons(anchor, token), send=send, edit=edit, old=old)
    return {
        "ok": True,
        "cmd": "carry_preview",
        "request_id": anchor,
        "token": token,
        "thb_satang": thb_satang,
    }


def due_line(conn: Any, row: dict[str, Any], now_ms: int) -> str:
    decided = int(row.get("decided_at_ms") or row.get("created_at_ms") or now_ms)
    account = pk.payout_account_line(
        conn,
        partner_id=str(row["partner_id"]),
        payout_kyc_id=row.get("payout_kyc_id"),
        partner_name=str(row.get("partner_name") or "?"),
    )
    return (
        f"ค้างโอน USDT · {row.get('partner_name') or '?'} · {_kind(str(row['kind']))} · "
        f"{_usdt_2(int(row['payout_micro']))} USDT · ยืนยัน {stamp_short(decided)}\n{account}"
    )


def _carry_anchor(conn: Any, partner_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT id FROM partner_request
        WHERE partner_id = ? AND kind IN ('close', 'withdraw_profit') AND status = 'confirmed'
        ORDER BY CASE kind WHEN 'close' THEN 0 ELSE 1 END, decided_at_ms DESC
        LIMIT 1
        """,
        (str(partner_id),),
    ).fetchone()
    return None if row is None else str(row["id"])
