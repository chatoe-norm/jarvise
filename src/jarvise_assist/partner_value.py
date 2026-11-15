"""Read-only USDⓈ-M account value for the partner ledger.

Uses only the existing signed GET balance and positionRisk paths.
Decimal only; never float. Exception text from Binance is never stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")


@dataclass(frozen=True)
class AccountValue:
    wallet: Decimal
    unrealized: Decimal
    value: Decimal
    open_positions: int
    read_at_ms: int


@dataclass(frozen=True)
class AccountValueError:
    reason: str
    detail: str = ""


def _parse_decimal(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite():
        return None
    return value


def parse_account_value(
    balance_rows: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
    *,
    read_at_ms: int,
) -> AccountValue | AccountValueError:
    """Parse wallet + unrealized from raw Binance GET payloads."""
    usdt_balance: Decimal | None = None
    bad_assets: list[str] = []
    for row in balance_rows:
        if not isinstance(row, dict):
            return AccountValueError(reason="bad_payload")
        asset = str(row.get("asset") or "")
        if asset == "USDT":
            bal = _parse_decimal(row.get("balance"))
            if bal is None:
                return AccountValueError(reason="bad_payload")
            usdt_balance = bal
        else:
            bal = _parse_decimal(row.get("balance"))
            if bal is None:
                return AccountValueError(reason="bad_payload")
            if bal != ZERO:
                bad_assets.append(asset)
    if usdt_balance is None:
        return AccountValueError(reason="no_usdt_row")
    if bad_assets:
        return AccountValueError(
            reason="non_usdt_asset",
            detail=",".join(sorted(bad_assets)),
        )

    unrealized = ZERO
    open_positions = 0
    bad_symbols: list[str] = []
    for row in position_rows:
        if not isinstance(row, dict):
            return AccountValueError(reason="bad_payload")
        amt = _parse_decimal(row.get("positionAmt"))
        if amt is None:
            return AccountValueError(reason="bad_payload")
        if amt == ZERO:
            continue
        margin = row.get("marginAsset")
        margin_s = "USDT" if margin is None or str(margin) == "" else str(margin)
        if margin_s != "USDT":
            bad_symbols.append(str(row.get("symbol") or ""))
            continue
        upnl = _parse_decimal(row.get("unRealizedProfit"))
        if upnl is None:
            return AccountValueError(reason="bad_payload")
        unrealized += upnl
        open_positions += 1
    if bad_symbols:
        return AccountValueError(
            reason="non_usdt_position",
            detail=",".join(sorted(bad_symbols)),
        )

    value = usdt_balance + unrealized
    if value <= ZERO:
        return AccountValueError(reason="value_not_positive")
    return AccountValue(
        wallet=usdt_balance,
        unrealized=unrealized,
        value=value,
        open_positions=open_positions,
        read_at_ms=int(read_at_ms),
    )


def _usdt_balance(rows: list[dict[str, Any]]) -> Decimal | None:
    for row in rows:
        if isinstance(row, dict) and str(row.get("asset") or "") == "USDT":
            return _parse_decimal(row.get("balance"))
    return None


def read_account_value(
    client: Any,
    *,
    now_ms: int,
    attempts: int = 3,
) -> AccountValue | AccountValueError:
    """Bracket-read wallet balance around positionRisk; retry while it moves."""
    if client is None:
        return AccountValueError(reason="no_client")
    try:
        for _ in range(int(attempts)):
            first = client.balance_rows()
            positions = client.position_rows()
            second = client.balance_rows()
            if not isinstance(first, list) or not isinstance(second, list) or not isinstance(positions, list):
                return AccountValueError(reason="bad_payload")
            b1 = _usdt_balance(first)
            b2 = _usdt_balance(second)
            if b1 is None:
                return parse_account_value(first, positions, read_at_ms=now_ms)
            if b2 is None:
                return parse_account_value(second, positions, read_at_ms=now_ms)
            if b1 == b2:
                return parse_account_value(second, positions, read_at_ms=now_ms)
        return AccountValueError(reason="wallet_moving")
    except Exception as exc:  # noqa: BLE001 — map to typed reason; never store str(exc)
        return AccountValueError(reason="read_failed", detail=type(exc).__name__)
