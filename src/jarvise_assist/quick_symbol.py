"""Typed Quick symbol validation for the coin-first picker (A0b). No orders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from jarvise_assist.explore import (
    REASON_CONTRACT,
    REASON_FORMAT,
    contract_rejection,
    normalize_explore_symbol,
)

LookupFn = Callable[[str], dict[str, Any] | None]

Outcome = Literal["ok", "format", "hint_1000", "not_found", "contract", "lookup_error"]


@dataclass(frozen=True)
class QuickSymbolResult:
    outcome: Outcome
    symbol: str | None = None
    hint_symbol: str | None = None
    message: str | None = None


def coerce_quick_symbol(raw: str) -> str | None:
    """Normalise typed input to SYMBOLUSDT (same as parse_quick_command)."""
    text = (raw or "").strip().upper().replace("/", "").replace("-", "").replace(" ", "").replace(",", "")
    if not text:
        return None
    if not text.endswith("USDT"):
        text = f"{text}USDT"
    return normalize_explore_symbol(text)


def validate_quick_symbol(raw: str, *, lookup: LookupFn) -> QuickSymbolResult:
    """Validate a typed Quick symbol. Never raises."""
    symbol = coerce_quick_symbol(raw)
    if symbol is None:
        return QuickSymbolResult(outcome="format", message=REASON_FORMAT)

    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    try:
        row = lookup(symbol)
    except Exception:  # noqa: BLE001
        return QuickSymbolResult(
            outcome="lookup_error",
            symbol=symbol,
            message="อ่าน Binance ไม่ได้ — ลองพิมพ์ใหม่",
        )

    reason = contract_rejection(row)
    if reason is None:
        return QuickSymbolResult(outcome="ok", symbol=symbol)

    if reason == REASON_CONTRACT:
        return QuickSymbolResult(outcome="contract", symbol=symbol, message=REASON_CONTRACT)

    # Not found (or unknown). Offer 1000BASEUSDT when the base does not already start with 1000.
    if not base.startswith("1000"):
        hint = f"1000{base}USDT"
        if normalize_explore_symbol(hint) is not None:
            try:
                hint_row = lookup(hint)
            except Exception:  # noqa: BLE001
                hint_row = None
            if contract_rejection(hint_row) is None:
                return QuickSymbolResult(
                    outcome="hint_1000",
                    symbol=symbol,
                    hint_symbol=hint,
                    message=f"ไม่พบ {symbol} — หมายถึง {hint} ?",
                )

    return QuickSymbolResult(
        outcome="not_found",
        symbol=symbol,
        message=f"ไม่พบ {symbol} บน Binance USDⓈ-M — พิมพ์ใหม่ หรือกด ✖️",
    )
