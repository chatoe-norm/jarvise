"""Quick config registry, clamps, advice, and DB read/write.

Pure logic + SQLite. No Telegram, no network, no order paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from jarvise_assist.sizing import portfolio_risk_cap_pct

ConfirmDir = Literal["up", "down", "toward_any", "none"]
ConfigType = Literal["pct", "r", "int", "hhmm", "enum", "list_pct", "autobe"]
Phase = Literal["now", "A0b", "A1", "A2"]
AdviceLevel = Literal["warn", "suggest"]

UNIVERSE_ORDER = ("core", "core_explore", "any")
COLUMN_KEYS = frozenset({"quick_risk_pct", "quick_risk_cap_pct"})
AWAITING_QUICK_TTL_MS = 300_000
# Reserved KV marker: invalidate outstanding qc:ok cards. Not a registry key;
# get_quick_config / history / reset ignore it (never logged as config history).
CONFIRM_CANCEL_KEY = "_confirm_cancel_ms"

_NUM_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
# Lenient read path only: optional leading sign, then clamp. Write stays on _NUM_RE.
_NUM_SIGNED_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_HHMM_COMPACT_RE = re.compile(r"^([01]\d|2[0-3])([0-5]\d)$")


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    alias: str
    group: str
    type: ConfigType
    unit: str
    default: Any
    hard_min: Any | None
    hard_max: Any | None
    confirm_dir: ConfirmDir
    phase: Phase
    label_th: str
    meaning_th: str
    enum_values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Advice:
    level: AdviceLevel
    text_th: str
    fix: tuple[str, Any] | None = None


# Ordered registry (display / iteration order).
QUICK_CONFIG: tuple[ConfigEntry, ...] = (
    ConfigEntry(
        "quick_risk_pct",
        "risk",
        "risk",
        "pct",
        "%",
        Decimal("0.5"),
        Decimal("0.05"),
        Decimal("2.0"),
        "up",
        "now",
        "ความเสี่ยงต่อไม้",
        "ความเสี่ยงต่อไม้ quick (รวมค่าธรรมเนียม) — ใช้แล้วในบัตรวันนี้",
    ),
    ConfigEntry(
        "quick_risk_cap_pct",
        "cap",
        "risk",
        "pct",
        "%",
        Decimal("1.0"),
        Decimal("0.1"),
        Decimal("5.0"),
        "up",
        "now",
        "เพดาน quick",
        "เพดานความเสี่ยง quick รวมที่เปิดอยู่ — ใช้แล้วในบัตรวันนี้",
    ),
    ConfigEntry(
        "quick_counter_risk_pct",
        "counter",
        "risk",
        "pct",
        "%",
        Decimal("0.25"),
        Decimal("0.05"),
        Decimal("2.0"),
        "up",
        "A1",
        "ความเสี่ยงสวนเทรนด์",
        "ความเสี่ยงเมื่อสวนเทรนด์หรือไม่ทราบเทรนด์ (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_combined_risk_warn_pct",
        "warn",
        "risk",
        "pct",
        "%",
        Decimal("5.0"),
        Decimal("1.0"),
        Decimal("20.0"),
        "up",
        "A1",
        "เตือนความเสี่ยงรวม",
        "ระดับเตือนเมื่อไม้หลัก+quick รวมเกิน (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_exec_universe",
        "universe",
        "risk",
        "enum",
        "",
        "core_explore",
        None,
        None,
        "toward_any",
        "A1",
        "จักรวาลบอทเข้าไม้",
        "เหรียญที่บอทเข้าไม้ได้: core / core_explore / any (ใช้ตั้งแต่ A1)",
        enum_values=UNIVERSE_ORDER,
    ),
    ConfigEntry(
        "quick_tp1_r",
        "tp1r",
        "tp",
        "r",
        "R",
        Decimal("1.0"),
        Decimal("0.3"),
        Decimal("5.0"),
        "none",
        "A1",
        "TP1 (R)",
        "ระยะ TP1 เป็นทวีคูณของ R (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_tp1_close_pct",
        "tp1pct",
        "tp",
        "pct",
        "%",
        Decimal("50"),
        Decimal("0"),
        Decimal("100"),
        "none",
        "A1",
        "ปิดที่ TP1 (%)",
        "สัดส่วน qty ที่ปิดที่ TP1 · 0=ไม่มี TP1 (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_night_start",
        "nightstart",
        "time",
        "hhmm",
        "",
        "23:30",
        None,
        None,
        "none",
        "A1",
        "เริ่มคืน",
        "เริ่มช่วงห้ามเข้าไม้ของบอท (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_night_end",
        "nightend",
        "time",
        "hhmm",
        "",
        "07:30",
        None,
        None,
        "none",
        "A1",
        "สิ้นสุดคืน",
        "จบช่วงห้ามเข้าไม้ของบอท (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_confirm_ttl_s",
        "ttl",
        "time",
        "int",
        "s",
        30,
        10,
        120,
        "up",
        "A1",
        "TTL ยืนยันเข้าไม้",
        "อายุปุ่มยืนยันเข้าไม้ของบอท (วินาที) (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_chase_r",
        "chase",
        "guard",
        "r",
        "R",
        Decimal("0.3"),
        Decimal("0.05"),
        Decimal("1.0"),
        "up",
        "A1",
        "chase (R)",
        "ปฏิเสธถ้า mark วิ่งเกิน chase·R (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_stop_buffer_pct",
        "bufpct",
        "guard",
        "pct",
        "%",
        Decimal("0.1"),
        Decimal("0.02"),
        Decimal("1.0"),
        "down",
        "A1",
        "บัฟเฟอร์ stop (%)",
        "stop ต้องห่าง mark อย่างน้อย pct (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_stop_buffer_ticks",
        "bufticks",
        "guard",
        "int",
        "ticks",
        2,
        1,
        20,
        "down",
        "A1",
        "บัฟเฟอร์ stop (ticks)",
        "stop ต้องห่าง mark อย่างน้อย N ticks (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_max_fee_r",
        "feer",
        "guard",
        "r",
        "R",
        Decimal("0.30"),
        Decimal("0.05"),
        Decimal("1.0"),
        "up",
        "A1",
        "fee สูงสุด (R)",
        "ปฏิเสธเมื่อค่าธรรมเนียมไป-กลับเกิน feer·R (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_max_leverage",
        "lev",
        "guard",
        "int",
        "x",
        5,
        1,
        5,
        "up",
        "A1",
        "เลเวอเรจสูงสุด",
        "เพดานเลเวอเรจ isolated ของบอท quick (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_same_side_cap",
        "sides",
        "guard",
        "int",
        "",
        2,
        1,
        5,
        "up",
        "A1",
        "จำนวนไม้ต่อฝั่ง",
        "จำนวนไม้ quick เปิดพร้อมกันต่อฝั่ง (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_fee_bps_per_side",
        "fee",
        "guard",
        "pct",
        "bps",
        Decimal("6"),
        Decimal("2"),
        Decimal("20"),
        "down",
        "A1",
        "fee (bps/ฝั่ง)",
        "ประมาณค่าธรรมเนียมต่อฝั่งสำหรับ sizing (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_stop_far_pct",
        "far",
        "guard",
        "pct",
        "%",
        Decimal("20"),
        Decimal("1"),
        Decimal("50"),
        "up",
        "A1",
        "stop ไกลสุด (%)",
        "ปฏิเสธ stop ที่ห่าง mark เกินนี้ (ใช้ตั้งแต่ A1)",
    ),
    ConfigEntry(
        "quick_stop_presets",
        "presets",
        "guard",
        "list_pct",
        "%",
        (Decimal("0.3"), Decimal("0.5"), Decimal("1")),
        Decimal("0.05"),
        Decimal("19.9"),
        "none",
        "A0b",
        "ปุ่ม stop สำเร็จรูป",
        "รายการ % stop บน stepper (1–4 ค่า) (ใช้ตั้งแต่ A0b)",
    ),
    ConfigEntry(
        "quick_be_offset_pct",
        "be",
        "a2",
        "pct",
        "%",
        Decimal("0.2"),
        Decimal("0.0"),
        Decimal("1.0"),
        "none",
        "A2",
        "BE offset (%)",
        "ระดับ BE = entry ± offset (A2 ยังไม่ใช้)",
    ),
    ConfigEntry(
        "quick_auto_be_time",
        "autobe",
        "a2",
        "autobe",
        "",
        "night",
        None,
        None,
        "none",
        "A2",
        "เวลา auto-BE",
        "hhmm หรือ night (= nightstart) (A2 ยังไม่ใช้)",
    ),
    ConfigEntry(
        "quick_time_stop_candles",
        "tstop",
        "a2",
        "int",
        "",
        6,
        2,
        48,
        "none",
        "A2",
        "time-stop (แท่ง)",
        "จำนวนแท่งก่อนแนะนำ time-stop (A2 ยังไม่ใช้)",
    ),
    ConfigEntry(
        "quick_time_stop_min_r",
        "tstopr",
        "a2",
        "r",
        "R",
        Decimal("0.5"),
        Decimal("0.1"),
        Decimal("2.0"),
        "none",
        "A2",
        "time-stop ขั้นต่ำ (R)",
        "ถ้ายังไม่ถึง +R นี้ใน N แท่ง (A2 ยังไม่ใช้)",
    ),
)

_BY_KEY: dict[str, ConfigEntry] = {e.key: e for e in QUICK_CONFIG}
_BY_ALIAS: dict[str, ConfigEntry] = {e.alias.lower(): e for e in QUICK_CONFIG}
_BY_ALIAS.update({e.key.lower(): e for e in QUICK_CONFIG})

GROUP_LABELS: dict[str, str] = {
    "risk": "ความเสี่ยง",
    "tp": "TP",
    "time": "เวลา",
    "guard": "ป้องกัน",
    "a2": "A2 (ยังไม่ใช้)",
}


def resolve_entry(alias_or_key: str) -> ConfigEntry | None:
    return _BY_ALIAS.get(str(alias_or_key).strip().lower())


def _dec(v: Any, places: str = "0.01") -> Decimal:
    return Decimal(str(v)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _fmt_num(v: Decimal | float | int) -> str:
    d = Decimal(str(v))
    if d == d.to_integral_value():
        return str(int(d))
    s = f"{d:.10f}".rstrip("0").rstrip(".")
    return s


def canonical_string(key: str, value: Any) -> str:
    entry = _BY_KEY[key]
    if entry.type == "list_pct":
        return ",".join(_fmt_num(Decimal(str(x))) for x in value)
    if entry.type == "hhmm":
        return str(value)
    if entry.type == "autobe":
        return str(value)
    if entry.type == "enum":
        return str(value)
    if entry.type == "int":
        return str(int(value))
    return _fmt_num(Decimal(str(value)))


def _parse_hhmm(raw: str) -> str | None:
    s = raw.strip()
    m = _HHMM_RE.fullmatch(s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m2 = _HHMM_COMPACT_RE.fullmatch(s)
    if m2:
        return f"{m2.group(1)}:{m2.group(2)}"
    return None


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def night_window_minutes(start: str, end: str) -> int:
    a, b = _minutes(start), _minutes(end)
    if b > a:
        return b - a
    return (24 * 60 - a) + b


def _bounds_th(entry: ConfigEntry) -> str:
    if entry.type == "list_pct":
        return (
            f"1–4 ค่า แต่ละค่า {_fmt_num(Decimal(str(entry.hard_min)))}–"
            f"{_fmt_num(Decimal(str(entry.hard_max)))} เรียงจากน้อยไปมาก ไม่ซ้ำ"
        )
    if entry.type == "enum":
        return " / ".join(entry.enum_values or ())
    if entry.type == "hhmm":
        return "00:00–23:59 · start≠end · หน้าต่าง ≤ 12 ชม."
    if entry.type == "autobe":
        return "hhmm หรือ night"
    unit = entry.unit
    lo = entry.hard_min
    hi = entry.hard_max
    if entry.type == "int":
        return f"{lo}–{hi}{unit}"
    return f"{_fmt_num(Decimal(str(lo)))}–{_fmt_num(Decimal(str(hi)))}{unit}"


def parse_value(
    key: str,
    raw: str,
    *,
    check_bounds: bool = True,
) -> tuple[Any | None, str | None]:
    """Parse raw text for ``key``. Returns (value, None) or (None, thai_error).

    Write path uses ``check_bounds=True`` (strict). Read path uses
    ``check_bounds=False`` then clamps via ``_clamp_value``.
    """
    entry = _BY_KEY.get(key) or resolve_entry(key)
    if entry is None:
        return None, "❌ ไม่รู้จักคีย์นี้"
    key = entry.key
    s = (raw or "").strip()
    if not s:
        return None, f"❌ ค่าว่าง — ขอบเขต {_bounds_th(entry)}"

    if entry.type == "enum":
        v = s.lower().replace("-", "_")
        if v not in (entry.enum_values or ()):
            return None, f"❌ ค่าใช้ไม่ได้ — เลือก {_bounds_th(entry)}"
        return v, None

    if entry.type == "autobe":
        low = s.lower()
        if low == "night":
            return "night", None
        hh = _parse_hhmm(s)
        if hh is None:
            return None, f"❌ เวลาใช้ไม่ได้ — {_bounds_th(entry)}"
        return hh, None

    if entry.type == "hhmm":
        hh = _parse_hhmm(s)
        if hh is None:
            return None, f"❌ เวลาใช้ไม่ได้ — {_bounds_th(entry)}"
        return hh, None

    num_re = _NUM_RE if check_bounds else _NUM_SIGNED_RE

    if entry.type == "list_pct":
        parts = [p.strip() for p in s.replace("_", ",").split(",") if p.strip()]
        if not 1 <= len(parts) <= 4:
            return None, f"❌ presets ต้องมี 1–4 ค่า — {_bounds_th(entry)}"
        vals: list[Decimal] = []
        for p in parts:
            tok = p[:-1].strip() if p.endswith("%") else p
            if not num_re.fullmatch(tok):
                return None, f'❌ ค่า "{p}" ใช้ไม่ได้ — {_bounds_th(entry)}'
            try:
                d = Decimal(tok).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except InvalidOperation:
                return None, f'❌ ค่า "{p}" ใช้ไม่ได้ — {_bounds_th(entry)}'
            if check_bounds and (d < entry.hard_min or d > entry.hard_max):  # type: ignore[operator]
                return None, f"❌ {_fmt_num(d)} เกินขอบเขต — {_bounds_th(entry)}"
            vals.append(d)
        if len(set(vals)) != len(vals):
            return None, f"❌ presets ต้องไม่ซ้ำ — {_bounds_th(entry)}"
        if vals != sorted(vals):
            return None, f"❌ presets ต้องเรียงจากน้อยไปมาก — {_bounds_th(entry)}"
        return tuple(vals), None

    # numeric: strip unit suffixes
    tok = s
    for suf in ("%", "R", "r", "s", "S", "x", "X", "bps", "BPS"):
        if tok.lower().endswith(suf.lower()) and len(tok) > len(suf):
            cand = tok[: -len(suf)].strip()
            if num_re.fullmatch(cand):
                tok = cand
                break
    if not num_re.fullmatch(tok):
        return None, f'❌ ค่า "{_truncate(s)}" ใช้ไม่ได้ — ขอบเขต {_bounds_th(entry)}'

    try:
        if entry.type == "int":
            # reject non-integers like 1.5 for int keys
            d = Decimal(tok)
            if d != d.to_integral_value():
                return None, f"❌ ต้องเป็นจำนวนเต็ม — ขอบเขต {_bounds_th(entry)}"
            iv = int(d)
            if check_bounds and (
                iv < int(entry.hard_min) or iv > int(entry.hard_max)  # type: ignore[arg-type]
            ):
                return None, f"❌ {iv} เกินขอบเขต — {_bounds_th(entry)}"
            return iv, None
        places = "0.01"
        if entry.key == "quick_fee_bps_per_side":
            places = "0.01"
        d = Decimal(tok).quantize(Decimal(places), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None, f'❌ ค่า "{_truncate(s)}" ใช้ไม่ได้ — ขอบเขต {_bounds_th(entry)}'

    if check_bounds:
        lo = Decimal(str(entry.hard_min))
        hi = Decimal(str(entry.hard_max))
        if d < lo or d > hi:
            return None, f"❌ {_fmt_num(d)} เกินขอบเขต — {_bounds_th(entry)}"
    return d, None


def mark_confirm_cancelled(conn: Any, *, now_ms: int) -> None:
    """Store cancel epoch so outstanding qc:ok cards become stale. No history row."""
    conn.execute(
        """
        INSERT INTO assist_quick_config (key, value, updated_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at_ms=excluded.updated_at_ms
        """,
        (CONFIRM_CANCEL_KEY, str(int(now_ms)), int(now_ms)),
    )
    conn.commit()


def get_confirm_cancel_ms(conn: Any) -> int | None:
    row = conn.execute(
        "SELECT value FROM assist_quick_config WHERE key=?",
        (CONFIRM_CANCEL_KEY,),
    ).fetchone()
    if row is None:
        return None
    raw = row["value"] if hasattr(row, "keys") else row[0]
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _truncate(tok: str) -> str:
    return tok[:32] + "…" if len(tok) > 32 else tok


def _clamp_value(entry: ConfigEntry, value: Any) -> tuple[Any, bool]:
    """Clamp to bounds. Returns (value, was_clamped)."""
    if entry.type == "enum":
        if value in (entry.enum_values or ()):
            return value, False
        return entry.default, True
    if entry.type == "autobe":
        if value == "night":
            return value, False
        if isinstance(value, str) and _parse_hhmm(value):
            return _parse_hhmm(str(value)), False
        return entry.default, True
    if entry.type == "hhmm":
        hh = _parse_hhmm(str(value)) if value is not None else None
        if hh:
            return hh, False
        return entry.default, True
    if entry.type == "list_pct":
        try:
            vals = tuple(Decimal(str(x)) for x in value)
            if (
                1 <= len(vals) <= 4
                and all(entry.hard_min <= v <= entry.hard_max for v in vals)  # type: ignore[operator]
                and list(vals) == sorted(vals)
                and len(set(vals)) == len(vals)
            ):
                return vals, False
        except Exception:  # noqa: BLE001
            pass
        return entry.default, True
    if entry.type == "int":
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return entry.default, True
        lo, hi = int(entry.hard_min), int(entry.hard_max)  # type: ignore[arg-type]
        clamped = max(lo, min(hi, iv))
        return clamped, clamped != iv
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return entry.default, True
    lo_d = Decimal(str(entry.hard_min))
    hi_d = Decimal(str(entry.hard_max))
    clamped_d = max(lo_d, min(hi_d, d)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return clamped_d, clamped_d != d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _select_risk_cap(conn: Any) -> tuple[Decimal | None, Decimal | None]:
    """Plain SELECT — never INSERT. Missing row → (None, None)."""
    row = conn.execute("SELECT risk_pct, cap_pct FROM assist_quick_settings WHERE id=1").fetchone()
    if row is None:
        return None, None
    try:
        risk = Decimal(str(row[0] if not hasattr(row, "keys") else row["risk_pct"]))
        cap = Decimal(str(row[1] if not hasattr(row, "keys") else row["cap_pct"]))
    except (InvalidOperation, TypeError, KeyError, IndexError):
        return None, None
    return risk, cap


def _select_config_map(conn: Any) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM assist_quick_config").fetchall()
    out: dict[str, str] = {}
    for r in rows:
        if hasattr(r, "keys"):
            out[str(r["key"])] = str(r["value"])
        else:
            out[str(r[0])] = str(r[1])
    return out


@dataclass
class QuickConfigView:
    values: dict[str, Any]
    clamped_keys: frozenset[str]
    defaulted_keys: frozenset[str]
    stored_notes: dict[str, str]  # key → Thai note about clamp/default

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def get_quick_config(conn: Any) -> QuickConfigView:
    """Read all keys with defaults and clamps. No writes."""
    risk_raw, cap_raw = _select_risk_cap(conn)
    stored = _select_config_map(conn)
    values: dict[str, Any] = {}
    clamped: set[str] = set()
    defaulted: set[str] = set()
    notes: dict[str, str] = {}

    for entry in QUICK_CONFIG:
        key = entry.key
        raw: Any
        if key == "quick_risk_pct":
            raw = risk_raw
        elif key == "quick_risk_cap_pct":
            raw = cap_raw
        else:
            raw = stored.get(key)

        if raw is None:
            values[key] = entry.default
            if key in COLUMN_KEYS:
                # missing column row → use registry default (not the SQL seed)
                defaulted.add(key)
            elif key not in stored and key not in COLUMN_KEYS:
                # missing config key is silent default (not flagged as "defaulted"
                # for display of untouched keys — use alias ✓ vs stored)
                pass
            continue

        if key in COLUMN_KEYS:
            parsed = Decimal(str(raw))
            val, was = _clamp_value(entry, parsed)
            values[key] = val
            if was:
                clamped.add(key)
                notes[key] = f"ค่าที่เก็บ {_fmt_num(parsed)} เกินขอบเขต → ใช้ {_fmt_num(Decimal(str(val)))}"
            continue

        # config table: lenient parse (no bounds), then clamp. Junk → default.
        parsed_val, err = parse_value(key, str(raw), check_bounds=False)
        if err is not None or parsed_val is None:
            values[key] = entry.default
            defaulted.add(key)
            notes[key] = f"ค่าเสีย → ใช้ค่าเริ่มต้น ({canonical_string(key, entry.default)})"
            continue
        val, was = _clamp_value(entry, parsed_val)
        values[key] = val
        if was:
            clamped.add(key)
            notes[key] = (
                f"ค่าที่เก็บ {canonical_string(key, parsed_val)} เกินขอบเขต → ใช้ {canonical_string(key, val)}"
            )

    return QuickConfigView(
        values=values,
        clamped_keys=frozenset(clamped),
        defaulted_keys=frozenset(defaulted),
        stored_notes=notes,
    )


def effective_quick_values(cfg: dict[str, Any] | QuickConfigView) -> dict[str, Any]:
    """Runtime safety clamps on top of stored/clamped values."""
    values = cfg.values if isinstance(cfg, QuickConfigView) else cfg
    risk = Decimal(str(values["quick_risk_pct"]))
    cap = Decimal(str(values["quick_risk_cap_pct"]))
    counter = Decimal(str(values["quick_counter_risk_pct"]))
    lev = int(values["quick_max_leverage"])
    risk_used = min(risk, cap)
    counter_used = min(counter, risk_used)
    lev_used = min(lev, 5)
    out = dict(values)
    out["risk_used"] = risk_used
    out["counter_used"] = counter_used
    out["lev_used"] = lev_used
    return out


def _ensure_settings_row_for_write(conn: Any) -> None:
    """Ensure assist_quick_settings id=1 exists without changing stored values."""
    row = conn.execute("SELECT 1 FROM assist_quick_settings WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            """
            INSERT OR IGNORE INTO assist_quick_settings
            (id, risk_pct, day_max_loss_r, streak_lock_n, cap_pct)
            VALUES (1, 1.0, 2, 3, 1.0)
            """
        )


def _validate_night_pair(start: str, end: str) -> str | None:
    if start == end:
        return "❌ ช่วงคืน start ต้องไม่เท่า end"
    mins = night_window_minutes(start, end)
    if mins > 12 * 60:
        return "❌ ช่วงคืนต้องไม่เกิน 12 ชม."
    return None


def needs_confirm(key: str, old: Any, new: Any) -> bool:
    entry = _BY_KEY[key]
    direction = entry.confirm_dir
    if direction == "none":
        return False
    if direction == "toward_any":
        try:
            oi = UNIVERSE_ORDER.index(str(old))
            ni = UNIVERSE_ORDER.index(str(new))
        except ValueError:
            return True
        return ni > oi
    if direction == "up":
        return Decimal(str(new)) > Decimal(str(old))
    if direction == "down":
        return Decimal(str(new)) < Decimal(str(old))
    return False


def needs_confirm_any(changes: list[tuple[str, Any, Any]]) -> bool:
    return any(needs_confirm(k, o, n) for k, o, n in changes)


def set_quick_config(
    conn: Any,
    key: str,
    value: Any,
    *,
    source: str,
    now_ms: int,
    commit: bool = True,
    skip_cross_key: bool = False,
) -> tuple[Any, Any]:
    """Validate, write column or config upsert, append log. One transaction."""
    entry = _BY_KEY.get(key) or resolve_entry(key)
    if entry is None:
        raise ValueError(f"unknown key: {key}")
    key = entry.key

    if isinstance(value, str):
        parsed, err = parse_value(key, value)
    else:
        parsed, err = parse_value(key, canonical_string(key, value))
    if err is not None or parsed is None:
        raise ValueError(err or "invalid")
    value = parsed

    view = get_quick_config(conn)
    old = view.values[key]

    if not skip_cross_key and key in {"quick_night_start", "quick_night_end"}:
        start = value if key == "quick_night_start" else view.values["quick_night_start"]
        end = value if key == "quick_night_end" else view.values["quick_night_end"]
        night_err = _validate_night_pair(str(start), str(end))
        if night_err:
            raise ValueError(night_err)

    if canonical_string(key, old) == canonical_string(key, value):
        return old, old

    new_s = canonical_string(key, value)
    old_s = canonical_string(key, old)

    started = False
    if commit and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        started = True
    try:
        if key == "quick_risk_pct":
            _ensure_settings_row_for_write(conn)
            conn.execute(
                "UPDATE assist_quick_settings SET risk_pct=?, updated_at_ms=? WHERE id=1",
                (float(Decimal(str(value))), int(now_ms)),
            )
        elif key == "quick_risk_cap_pct":
            _ensure_settings_row_for_write(conn)
            conn.execute(
                "UPDATE assist_quick_settings SET cap_pct=?, updated_at_ms=? WHERE id=1",
                (float(Decimal(str(value))), int(now_ms)),
            )
        else:
            conn.execute(
                """
                INSERT INTO assist_quick_config (key, value, updated_at_ms)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (key, new_s, int(now_ms)),
            )
        conn.execute(
            """
            INSERT INTO assist_quick_config_log (key, old_value, new_value, source, at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, old_s, new_s, source, int(now_ms)),
        )
        if commit:
            conn.commit()
    except BaseException:
        if commit and started:
            conn.rollback()
        elif commit and conn.in_transaction:
            conn.rollback()
        raise
    return old, value


def reset_quick_config(
    conn: Any,
    key_or_all: str,
    *,
    source: str,
    now_ms: int,
) -> list[tuple[str, Any, Any]]:
    """Reset one key or all to registry defaults. Never auto-called.

    Reset-all validates the FINAL night pair (not intermediate states) and
    applies atomically.
    """
    if key_or_all == "all":
        keys = [e.key for e in QUICK_CONFIG]
        final_start = next(e.default for e in QUICK_CONFIG if e.key == "quick_night_start")
        final_end = next(e.default for e in QUICK_CONFIG if e.key == "quick_night_end")
        night_err = _validate_night_pair(str(final_start), str(final_end))
        if night_err:
            raise ValueError(night_err)
        skip_cross = True
    else:
        entry = resolve_entry(key_or_all)
        if entry is None:
            raise ValueError(f"unknown key: {key_or_all}")
        keys = [entry.key]
        skip_cross = False

    changes: list[tuple[str, Any, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for key in keys:
            entry = _BY_KEY[key]
            old, new = set_quick_config(
                conn,
                key,
                entry.default,
                source=source,
                now_ms=now_ms,
                commit=False,
                skip_cross_key=skip_cross,
            )
            if canonical_string(key, old) != canonical_string(key, new):
                changes.append((key, old, new))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return changes


def quick_config_history(conn: Any, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, key, old_value, new_value, source, at_ms
        FROM assist_quick_config_log
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        if hasattr(r, "keys"):
            out.append(dict(r))
        else:
            out.append(
                {
                    "id": r[0],
                    "key": r[1],
                    "old_value": r[2],
                    "new_value": r[3],
                    "source": r[4],
                    "at_ms": r[5],
                }
            )
    return out


def _changed_keys(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    changed: set[str] = set()
    for k in set(before) | set(after):
        if k not in before or k not in after:
            changed.add(k)
            continue
        a, b = before[k], after[k]
        if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
            if tuple(Decimal(str(x)) for x in a) != tuple(Decimal(str(x)) for x in b):
                changed.add(k)
        else:
            try:
                if Decimal(str(a)) != Decimal(str(b)):
                    changed.add(k)
            except (InvalidOperation, TypeError):
                if a != b:
                    changed.add(k)
    return changed


def _d(cfg: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(cfg[key]))


def quick_config_advice(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    main_risk_pct: Decimal,
) -> list[Advice]:
    """Advice triggered by the change (inputs include changed keys)."""
    changed = _changed_keys(before, after)
    if not changed:
        return []
    return _advice_for(after, main_risk_pct=main_risk_pct, only_keys=changed)


def quick_config_active_warnings(
    cfg: dict[str, Any],
    *,
    main_risk_pct: Decimal,
) -> list[Advice]:
    """All currently active warnings/suggestions for the settings view."""
    return _advice_for(cfg, main_risk_pct=main_risk_pct, only_keys=None)


def _advice_for(
    cfg: dict[str, Any],
    *,
    main_risk_pct: Decimal,
    only_keys: set[str] | None,
) -> list[Advice]:
    risk = _d(cfg, "quick_risk_pct")
    cap = _d(cfg, "quick_risk_cap_pct")
    counter = _d(cfg, "quick_counter_risk_pct")
    warn = _d(cfg, "quick_combined_risk_warn_pct")
    tp1pct = _d(cfg, "quick_tp1_close_pct")
    tp1r = _d(cfg, "quick_tp1_r")
    chase = _d(cfg, "quick_chase_r")
    ttl = int(cfg["quick_confirm_ttl_s"])
    feer = _d(cfg, "quick_max_fee_r")
    lev = int(cfg["quick_max_leverage"])
    universe = str(cfg["quick_exec_universe"])
    night_start = str(cfg["quick_night_start"])
    night_end = str(cfg["quick_night_end"])
    autobe = str(cfg["quick_auto_be_time"])
    main = Decimal(str(main_risk_pct))
    port = portfolio_risk_cap_pct(main)

    def hit(*keys: str) -> bool:
        if only_keys is None:
            return True
        return bool(only_keys & set(keys))

    out: list[Advice] = []

    # 1 risk > cap
    if hit("quick_risk_pct", "quick_risk_cap_pct") and risk > cap:
        fix_cap = min(Decimal("5.0"), (risk * 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        out.append(
            Advice(
                "warn",
                f"cap จำกัด — ใช้ได้จริง = {_fmt_num(cap)}%",
                ("quick_risk_cap_pct", fix_cap),
            )
        )

    # 2 risk < counter
    if hit("quick_risk_pct", "quick_counter_risk_pct") and risk < counter:
        fix_c = max(Decimal("0.05"), (risk / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        out.append(
            Advice(
                "warn",
                "ความเสี่ยงสวนเทรนด์มากกว่าตามเทรนด์",
                ("quick_counter_risk_pct", fix_c),
            )
        )

    # 3 main + risk ≥ 0.8 × warn
    if hit("quick_risk_pct", "quick_combined_risk_warn_pct") and main + risk >= warn * Decimal("0.8"):
        out.append(
            Advice(
                "warn",
                "ไม้หลัก + quick ใกล้ถึงระดับเตือนรวม",
                None,
            )
        )

    # 4 cap/risk < 2
    if hit("quick_risk_pct", "quick_risk_cap_pct") and risk > 0 and cap / risk < 2:
        out.append(
            Advice(
                "suggest",
                "เปิดพร้อมกันได้ไม่ถึง 2 ไม้",
                None,
            )
        )

    # 5 cap < risk or cap < counter
    if hit("quick_risk_pct", "quick_risk_cap_pct", "quick_counter_risk_pct") and (
        cap < risk or cap < counter
    ):
        fix_cap = max(risk, counter)
        out.append(
            Advice(
                "warn",
                "cap ต่ำกว่า risk หรือ counter — จะถูก clamp",
                ("quick_risk_cap_pct", fix_cap),
            )
        )

    # 6 portfolio_cap(main) + cap > warn
    if hit("quick_risk_cap_pct", "quick_combined_risk_warn_pct") and port + cap > warn:
        out.append(
            Advice(
                "warn",
                "กรณีแย่สุด (เพดานหลัก+quick) อาจเกินระดับเตือน",
                None,
            )
        )

    # 7 counter ≥ risk
    if hit("quick_counter_risk_pct", "quick_risk_pct") and counter >= risk:
        fix_c = max(Decimal("0.05"), (risk / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        out.append(
            Advice(
                "warn",
                "counter ไม่เล็กกว่า risk แล้ว",
                ("quick_counter_risk_pct", fix_c),
            )
        )

    # 8 counter > cap
    if hit("quick_counter_risk_pct", "quick_risk_cap_pct") and counter > cap:
        out.append(Advice("warn", "counter สูงกว่า cap — จะถูก clamp", None))

    # 9 tp1pct
    if hit("quick_tp1_close_pct"):
        if tp1pct == 0:
            out.append(Advice("warn", "ไม่มี TP1", None))
        elif tp1pct == 100:
            out.append(Advice("warn", "ไม่เหลือไม้ให้ BE/trail", None))
        elif tp1pct < 25 or tp1pct > 75:
            out.append(
                Advice(
                    "warn",
                    "ขา TP มักต่ำกว่า minNotional → อาจไม่มี TP",
                    None,
                )
            )

    # 10 tp1r
    if hit("quick_tp1_r"):
        if tp1r <= Decimal("0.5"):
            out.append(Advice("warn", "TP1 อยู่ในโซน fee/noise", None))
        elif tp1r > Decimal("2.0"):
            out.append(Advice("warn", "TP1 ไกลกว่าเป้า 2R ของระบบหลัก", None))

    # 11 chase ≥ tp1r/2
    if hit("quick_chase_r", "quick_tp1_r") and chase >= tp1r / 2:
        out.append(Advice("warn", "chase กินพื้นที่ TP1 ไปครึ่งหนึ่ง", None))

    # 12 warn vs main+risk / portfolio+cap
    if hit("quick_combined_risk_warn_pct", "quick_risk_pct") and warn <= main + risk:
        out.append(
            Advice(
                "warn",
                "ไม้หลัก 1 + quick 1 ก็เตือนแล้ว",
                None,
            )
        )
    if hit("quick_combined_risk_warn_pct", "quick_risk_cap_pct") and warn > port + cap:
        out.append(Advice("warn", "ไม่มีวันเตือน", None))

    # 13 night window / autobe
    if hit("quick_night_start", "quick_night_end", "quick_auto_be_time"):
        mins = night_window_minutes(night_start, night_end)
        end_m = _minutes(night_end)
        if mins < 6 * 60 or end_m < _minutes("06:00"):
            out.append(
                Advice(
                    "warn",
                    "ช่วงคืนสั้นหรือจบก่อน 06:00 — บอทเข้าไม้ดึกได้",
                    None,
                )
            )
        if autobe != "night" and autobe != night_start:
            out.append(
                Advice(
                    "suggest",
                    "autobe ไม่ตรง nightstart",
                    ("quick_auto_be_time", "night"),
                )
            )

    # 14 ttl > 60
    if hit("quick_confirm_ttl_s") and ttl > 60:
        out.append(Advice("warn", "TTL ยืนยันนาน — ราคาอาจขยับก่อนกด", None))

    # 15 chase > 0.5
    if hit("quick_chase_r") and chase > Decimal("0.5"):
        out.append(Advice("warn", "chase สูง — ฟิลอาจไกลจากราคาที่โควต", None))

    # 16 feer > 0.5
    if hit("quick_max_fee_r") and feer > Decimal("0.5"):
        out.append(Advice("warn", "อนุญาต stop แน่นมาก — fee กินครึ่ง R", None))

    # 17 lev < 3
    if hit("quick_max_leverage") and lev < 3:
        out.append(Advice("suggest", "เลเวอเรจต่ำ — ขนาดอาจถูกเพดานมาร์จิ้น", None))

    # 18 universe == any
    if hit("quick_exec_universe") and universe == "any":
        out.append(
            Advice(
                "warn",
                "universe=any — ไม่ทราบเทรนด์ 4h ใช้ counter risk · หนังสือบาง",
                None,
            )
        )

    return out
