"""Display-only coin-trend ranking for the Telegram 🔎 menu.

Pure helpers: no Telegram sends, no DB writes, no admission changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from jarvise_assist import ASSIST_SYMBOLS, ASSIST_TIMEFRAME
from jarvise_assist.futures import find_protective_stop
from jarvise_assist.quick_tags import MSG_QUICK_OPEN
from jarvise_assist.sizing import (
    MAX_OPEN_IDEAS,
    ExistingIdea,
    SignalCandidate,
    analysis_is_fresh,
    passes_signal_floor,
    portfolio_risk_cap_pct,
)
from jarvise_assist.store import ensure_settings
from jarvise_ingest.db import load_latest_analysis

RiskKind = Literal["slot_cap", "risk_budget", "no_backfill"]

_SKIP_RANK_REASONS = frozenset({"open_position", "quick_open"})

_REASON_TH: dict[str, str] = {
    "flat": "ไม่มีแนวโน้ม",
    "low_confidence": "ความมั่นใจต่ำ",
    "stale_analysis": "ข้อมูลยังไม่อัปเดต",
    "open_position": "ถืออยู่แล้ว",
    "risk_cap": "เต็มเพดานความเสี่ยง",
    "below_min_notional": "ไม้เล็กกว่าขั้นต่ำ",
    "margin_cap": "margin ไม่พอ",
    "size_failed": "คำนวณขนาดไม้ไม่ได้",
    "same_idea": "ส่งไปแล้วใน 24 ชม.",
    "requote_cap_zero": "จำกัดตามไอเดียแรกแล้วขนาดเหลือ 0 — ไม่ส่ง",
    "requote_no_baseline": "ไม่มีขนาดอ้างอิงจากไอเดียแรก",
    "quick_open": MSG_QUICK_OPEN,
}


@dataclass(frozen=True)
class RiskExplain:
    kind: RiskKind
    used_pct: Decimal
    this_pct: Decimal
    cap_pct: Decimal
    max_ideas: int


@dataclass(frozen=True)
class TrendRow:
    rank: int
    symbol: str
    side: str
    confidence: float
    stop_pct: Decimal | None
    risk_pct_of_balance: Decimal | None
    status: str


def _pct_of_balance(amount: Decimal, balance: Decimal) -> Decimal:
    if balance <= 0:
        return Decimal("0")
    return (amount / balance) * Decimal("100")


def _fmt_pct1(value: Decimal | float) -> str:
    return f"{float(value):.1f}"


def _fmt_cap(value: Decimal | float) -> str:
    f = float(value)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.1f}"


def _reason_label(reason: str) -> str:
    label = _REASON_TH.get(reason)
    if label is None and reason.startswith("mark:"):
        return "อ่านราคาไม่ได้"
    if label is None:
        return reason
    return label


def explain_risk_rejections(
    existing: list[ExistingIdea],
    candidates: list[SignalCandidate],
    rejected: list[SignalCandidate],
    balance: Decimal,
    *,
    max_ideas: int = MAX_OPEN_IDEAS,
    max_risk_pct: Decimal,
) -> dict[str, RiskExplain]:
    """Classify each rejected candidate without re-running the admit loop.

    Pre-margin admitted = candidates not in ``rejected`` (by identity). Uses the
    same slot/budget arithmetic ``admit_signals`` would have after those admits.
    """
    rejected_ids = {id(c) for c in rejected}
    admitted = [c for c in candidates if id(c) not in rejected_ids]
    used = sum((e.risk_usdt for e in existing), Decimal("0")) + sum(
        (c.sized.loss_at_stop for c in admitted), Decimal("0")
    )
    slots = len(existing) + len(admitted)
    budget = balance * (max_risk_pct / Decimal("100")) if balance > 0 else Decimal("0")
    used_pct = _pct_of_balance(used, balance)
    out: dict[str, RiskExplain] = {}
    for cand in rejected:
        loss = cand.sized.loss_at_stop
        this_pct = _pct_of_balance(loss, balance)
        if slots + 1 > max_ideas:
            kind: RiskKind = "slot_cap"
        elif used + loss > budget:
            kind = "risk_budget"
        else:
            kind = "no_backfill"
        out[cand.sized.symbol] = RiskExplain(
            kind=kind,
            used_pct=used_pct,
            this_pct=this_pct,
            cap_pct=max_risk_pct,
            max_ideas=max_ideas,
        )
    return out


def _format_risk_status(explain: RiskExplain) -> str:
    if explain.kind == "slot_cap":
        return f"⛔ ครบ {explain.max_ideas} ไม้แล้ว"
    if explain.kind == "risk_budget":
        return (
            f"⛔ ความเสี่ยงรวมเกิน {_fmt_cap(explain.cap_pct)}%: "
            f"ใช้อยู่ {_fmt_pct1(explain.used_pct)}% + ไม้นี้ {_fmt_pct1(explain.this_pct)}%"
        )
    return "⛔ ไม่ข้ามลำดับ — ไม้ที่มั่นใจกว่าถูกบล็อกก่อน"


def _stop_distance_pct(*, entry: Decimal, stop: Decimal) -> Decimal | None:
    if entry <= 0:
        return None
    return (abs(entry - stop) / entry) * Decimal("100")


def _candidate_by_symbol(candidates: list[SignalCandidate]) -> dict[str, SignalCandidate]:
    return {c.sized.symbol: c for c in candidates}


def _existing_by_symbol(existing: list[ExistingIdea]) -> dict[str, ExistingIdea]:
    return {e.symbol: e for e in existing}


def _position_by_symbol(positions: list[Any]) -> dict[str, Any]:
    return {str(p.symbol).upper(): p for p in positions}


def _held_stop_pct(pos: Any, *, orders: list[dict[str, Any]]) -> Decimal | None:
    """Stop distance % for a held/quick row from the protective stop only."""
    entry = Decimal(str(pos.entry_price))
    protective = find_protective_stop(
        orders,
        symbol=pos.symbol,
        position_side=pos.position_side,
        trade_side=pos.side,
    )
    if protective is None:
        return None
    return _stop_distance_pct(entry=entry, stop=protective)


def build_trend_rows(
    conn: Any,
    scan: dict[str, Any],
    idea_status: dict[str, str],
    now_ms: int,
) -> tuple[list[TrendRow], str]:
    """Build ranked rows and a footer line for non-passing coins.

    Ranking key matches ``admit_signals``: (-confidence, universe index, symbol).
    """
    balance = Decimal(str(scan.get("balance_usdt") or 0))
    candidates: list[SignalCandidate] = list(scan.get("candidates") or [])
    existing: list[ExistingIdea] = list(scan.get("existing") or [])
    rejected: list[SignalCandidate] = list(scan.get("rejected") or [])
    admitted: list[SignalCandidate] = list(scan.get("admitted") or [])
    margin_dropped: list[SignalCandidate] = list(scan.get("margin_dropped") or [])
    skipped: list[dict[str, Any]] = list(scan.get("skipped") or [])
    positions = list(scan.get("positions") or [])
    orders = list(scan.get("orders") or [])

    max_risk_pct = portfolio_risk_cap_pct(Decimal(str(ensure_settings(conn)["risk_pct"])))
    explains = explain_risk_rejections(
        existing,
        candidates,
        rejected,
        balance,
        max_ideas=MAX_OPEN_IDEAS,
        max_risk_pct=max_risk_pct,
    )

    cand_by_sym = _candidate_by_symbol(candidates)
    existing_by_sym = _existing_by_symbol(existing)
    pos_by_sym = _position_by_symbol(positions)
    admitted_syms = {c.sized.symbol for c in admitted}
    margin_syms = {c.sized.symbol for c in margin_dropped}
    rejected_syms = {c.sized.symbol for c in rejected}
    skip_by_sym = {str(row.get("symbol") or "").upper(): str(row.get("reason") or "") for row in skipped}
    symbol_rank = {sym: i for i, sym in enumerate(ASSIST_SYMBOLS)}

    ranked_raw: list[tuple[float, int, str, TrendRow]] = []
    footer_parts: list[str] = []

    for symbol in ASSIST_SYMBOLS:
        reason = skip_by_sym.get(symbol)
        analysis = load_latest_analysis(conn, symbol, ASSIST_TIMEFRAME)

        # Admitted / rejected / margin / candidate-path skips that already passed the floor.
        if symbol in cand_by_sym:
            cand = cand_by_sym[symbol]
            sized = cand.sized
            stop_pct = _stop_distance_pct(entry=sized.entry, stop=sized.stop)
            risk_pct = _pct_of_balance(sized.loss_at_stop, balance)
            if symbol in admitted_syms:
                status = idea_status.get(symbol) or "✅ เปิดได้ ↓"
            elif symbol in margin_syms:
                status = _REASON_TH["margin_cap"]
            elif symbol in rejected_syms:
                expl = explains.get(symbol)
                status = _format_risk_status(expl) if expl is not None else _REASON_TH["risk_cap"]
            elif reason == "below_min_notional":
                status = _REASON_TH["below_min_notional"]
            elif reason == "margin_cap":
                status = _REASON_TH["margin_cap"]
            elif reason is not None and reason.startswith("mark:"):
                status = _reason_label(reason)
            elif reason == "size_failed":
                status = _REASON_TH["size_failed"]
            else:
                status = _reason_label(reason or "risk_cap")
            row = TrendRow(
                rank=0,
                symbol=symbol,
                side=sized.side.upper(),
                confidence=float(cand.confidence),
                stop_pct=stop_pct,
                risk_pct_of_balance=risk_pct,
                status=status,
            )
            ranked_raw.append((-cand.confidence, symbol_rank[symbol], symbol, row))
            continue

        # Held / quick: rank only when analysis still passes floor + fresh.
        if reason in _SKIP_RANK_REASONS:
            if (
                analysis is not None
                and passes_signal_floor(analysis)
                and analysis_is_fresh(analysis, now_ms=now_ms)
            ):
                side = str(analysis["action"]).lower()
                conf = float(analysis.get("confidence_score") or 0.0)
                held_stop: Decimal | None = None
                held_risk: Decimal | None = None
                pos = pos_by_sym.get(symbol)
                if pos is not None:
                    held_stop = _held_stop_pct(pos, orders=orders)
                ex = existing_by_sym.get(symbol)
                if ex is not None:
                    held_risk = _pct_of_balance(ex.risk_usdt, balance)
                if reason == "open_position":
                    if pos is not None:
                        status = f"📌 ถืออยู่แล้ว ({pos.side.upper()})"
                    else:
                        status = "📌 ถืออยู่แล้ว"
                else:
                    status = _REASON_TH["quick_open"]
                if reason in {"open_position", "quick_open"} and pos is not None and held_stop is None:
                    status = f"{status} · ⚠️ ไม่มี stop"
                row = TrendRow(
                    rank=0,
                    symbol=symbol,
                    side=side.upper(),
                    confidence=conf,
                    stop_pct=held_stop,
                    risk_pct_of_balance=held_risk,
                    status=status,
                )
                ranked_raw.append((-conf, symbol_rank[symbol], symbol, row))
            else:
                footer_parts.append(f"{symbol} {_reason_label(reason or 'flat')}")
            continue

        # mark: / size_failed / below_min without a candidate entry (should be rare).
        if reason is not None and (
            reason.startswith("mark:")
            or reason in {"size_failed", "below_min_notional", "margin_cap", "same_idea"}
        ):
            if (
                analysis is not None
                and passes_signal_floor(analysis)
                and analysis_is_fresh(analysis, now_ms=now_ms)
            ):
                side = str(analysis["action"]).lower()
                conf = float(analysis.get("confidence_score") or 0.0)
                row = TrendRow(
                    rank=0,
                    symbol=symbol,
                    side=side.upper(),
                    confidence=conf,
                    stop_pct=None,
                    risk_pct_of_balance=None,
                    status=_reason_label(reason),
                )
                ranked_raw.append((-conf, symbol_rank[symbol], symbol, row))
            else:
                footer_parts.append(f"{symbol} {_reason_label(reason)}")
            continue

        # flat / low_confidence / stale / missing → footer.
        footer_parts.append(f"{symbol} {_reason_label(reason or 'flat')}")

    ranked_raw.sort(key=lambda t: (t[0], t[1], t[2]))
    rows: list[TrendRow] = []
    for i, (_neg_c, _rank, _sym, row) in enumerate(ranked_raw, start=1):
        rows.append(
            TrendRow(
                rank=i,
                symbol=row.symbol,
                side=row.side,
                confidence=row.confidence,
                stop_pct=row.stop_pct,
                risk_pct_of_balance=row.risk_pct_of_balance,
                status=row.status,
            )
        )
    footer = ""
    if footer_parts:
        # Keep universe order for the footer pieces we collected in ASSIST_SYMBOLS walk.
        footer = "ไม่มีสัญญาณ: " + " · ".join(footer_parts)
    return rows, footer


def _format_row(row: TrendRow) -> str:
    parts = [f"{row.rank}. {row.symbol} {row.side} · มั่นใจ {row.confidence:.2f}"]
    if row.stop_pct is not None:
        parts.append(f"stop ห่าง {_fmt_pct1(row.stop_pct)}%")
    if row.risk_pct_of_balance is not None:
        parts.append(f"เสี่ยง {_fmt_pct1(row.risk_pct_of_balance)}%")
    mid = " · ".join(parts)
    return f"{mid} → {row.status}"


def render_trend_ranking(rows: list[TrendRow], footer: str) -> str:
    header = "🔎 เทรนด์เหรียญ · เรียงตามความมั่นใจ (สูง→ต่ำ)\nความมั่นใจไม่ใช่โอกาสชนะ · เป้า 2R ทุกไม้"
    body_lines = [_format_row(r) for r in rows]
    chunks = [header]
    if body_lines:
        chunks.append("\n".join(body_lines))
    if footer:
        chunks.append(footer)
    return "\n".join(chunks)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fit_telegram(text: str, limit: int = 3900) -> str:
    """Trim ranking text to ``limit`` UTF-16 units. Drops footer first, then lowest rows."""
    if _utf16_len(text) <= limit:
        return text
    lines = text.split("\n")
    if len(lines) < 2:
        return text
    header = "\n".join(lines[:2])
    rest = lines[2:]
    footer = ""
    body = list(rest)
    if body and body[-1].startswith("ไม่มีสัญญาณ:"):
        footer = body[-1]
        body = body[:-1]

    def assemble(rows: list[str], *, foot: str = "", extra: str = "") -> str:
        parts = [header]
        if rows:
            parts.append("\n".join(rows))
        if extra:
            parts.append(extra)
        if foot:
            parts.append(foot)
        return "\n".join(parts)

    with_footer = assemble(body, foot=footer)
    if _utf16_len(with_footer) <= limit:
        return with_footer

    # Drop footer first.
    no_footer = assemble(body)
    if _utf16_len(no_footer) <= limit:
        return no_footer

    kept = list(body)
    dropped = 0
    while kept:
        dropped += 1
        kept.pop()
        trial = assemble(kept, extra=f"… อีก {dropped} เหรียญ")
        if _utf16_len(trial) <= limit:
            return trial
    return header
