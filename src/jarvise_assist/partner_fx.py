"""THB payout record and the USDT leftover carried outside the pool.

Data layer only. One ``transaction()`` per write. The futures ledger
(partner, partner_event, partner_request, NAV) is not read or written.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import uuid
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from jarvise_ingest.db import transaction

logger = logging.getLogger(__name__)

DEVIATION_THRESHOLD_BPS = 100
RESIDUAL_TOL_SATANG = 100
RATE_MIN_E4 = 200_000
RATE_MAX_E4 = 600_000
SELL_STEP_MICRO = 1_000_000
REASON_MIN_CHARS = 10
DRAFT_TTL_MS = 15 * 60 * 1000
_SATANG_DIVISOR = 100_000_000
_RATE_QUANT = Decimal("0.0001")
_SCALED_RE = re.compile(r"^\d+(\.\d+)?$")
_REF_OK = "ok"
_REF_STATUSES = frozenset({_REF_OK, "unavailable", "off"})


class _FxAbort(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _new_fx_id() -> str:
    return f"px_{uuid.uuid4().hex[:16]}"


def _new_carry_id() -> str:
    return f"pc_{uuid.uuid4().hex[:16]}"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _reason_ok(reason: str | None) -> bool:
    if reason is None:
        return False
    return len(str(reason).strip()) >= REASON_MIN_CHARS


def _rate_e4(rate: Decimal) -> int | None:
    """e4 when ``rate`` has at most 4 decimal places. Never rounds."""
    value = Decimal(rate)
    if not value.is_finite():
        return None
    if value != value.quantize(_RATE_QUANT):
        return None
    scaled = value * Decimal(10_000)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def _parse_scaled(text: str, places: int, scale: int) -> int | None:
    raw = str(text).strip()
    if not _SCALED_RE.fullmatch(raw):
        return None
    value = Decimal(raw)
    quantum = Decimal(1).scaleb(-places)
    if value != value.quantize(quantum):
        return None
    scaled = value * Decimal(scale)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def parse_thb(text: str) -> int | None:
    """Satang. More than 2 decimal places is refused, not rounded."""
    return _parse_scaled(text, 2, 100)


def parse_rate(text: str) -> int | None:
    """Rate ×1e4. More than 4 decimal places is refused, not rounded."""
    return _parse_scaled(text, 4, 10_000)


def parse_usdt(text: str) -> int | None:
    """USDT micro. More than 6 decimal places is refused, not rounded."""
    return _parse_scaled(text, 6, 1_000_000)


def compute(
    *,
    usdt_payout_micro: int,
    carry_in_micro: int,
    chain_fee_micro: int,
    usdt_sold_micro: int,
    rate: Decimal,
    trade_fee_satang: int,
    thb_wd_fee_satang: int,
    bank_fee_satang: int,
    net_satang: int,
    ref_rate: Decimal | None = None,
    correction: bool = False,
) -> dict[str, Any]:
    """Pure figures for one payout. Refusals are codes; inputs are not rounded."""
    refusals: list[str] = []
    rate_e4 = _rate_e4(Decimal(rate))
    if rate_e4 is None:
        refusals.append("too_many_decimals")
    elif rate_e4 < RATE_MIN_E4 or rate_e4 > RATE_MAX_E4:
        refusals.append("rate_out_of_bounds")
    leftover = int(usdt_payout_micro) + int(carry_in_micro) - int(chain_fee_micro) - int(usdt_sold_micro)
    if leftover < 0:
        refusals.append("sold_exceeds_available")
    if int(usdt_sold_micro) % SELL_STEP_MICRO != 0:
        refusals.append("sold_not_step")
    gross: int | None = None
    residual: int | None = None
    if rate_e4 is not None and rate_e4 > 0 and int(usdt_sold_micro) > 0:
        gross = (int(usdt_sold_micro) * rate_e4) // _SATANG_DIVISOR
        residual = (
            gross - int(trade_fee_satang) - int(thb_wd_fee_satang) - int(bank_fee_satang) - int(net_satang)
        )
    deviation: int | None = None
    if ref_rate is not None and rate_e4 is not None:
        ref = Decimal(ref_rate)
        ref_e4 = _rate_e4(ref)
        if ref_e4 is None:
            if "too_many_decimals" not in refusals:
                refusals.append("too_many_decimals")
        elif ref_e4 > 0:
            raw = (Decimal(rate) - ref) / ref * Decimal(10_000)
            deviation = int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if (
        correction
        or (residual is not None and abs(residual) > RESIDUAL_TOL_SATANG)
        or (deviation is not None and abs(deviation) > DEVIATION_THRESHOLD_BPS)
    ):
        refusals.append("reason_required")
    denom = int(usdt_payout_micro) + int(carry_in_micro) - leftover
    effective: Decimal | None = None
    if denom > 0:
        effective = (Decimal(int(net_satang)) * Decimal(10_000) / Decimal(denom)).quantize(
            _RATE_QUANT, rounding=ROUND_DOWN
        )
    return {
        "gross_satang": gross,
        "leftover_micro": leftover,
        "residual_satang": residual,
        "deviation_bps": deviation,
        "effective_rate": effective,
        "rate_e4": rate_e4,
        "refusals": refusals,
    }


def carry_balance(conn: sqlite3.Connection, partner_id: str) -> int:
    """Latest ``balance_after_micro`` for the partner, or 0."""
    row = conn.execute(
        """
        SELECT balance_after_micro
        FROM partner_usdt_carry
        WHERE partner_id = ?
        ORDER BY seq DESC
        LIMIT 1
        """,
        (str(partner_id),),
    ).fetchone()
    if row is None:
        return 0
    return int(row["balance_after_micro"])


def _log_refused(request_id: str | None, partner_id: str | None, reason: str) -> None:
    logger.info(
        "partner fx refused request_id=%s partner_id=%s reason=%s",
        request_id or "-",
        partner_id or "-",
        reason,
    )


def _fail(request_id: str | None, partner_id: str | None, reason: str) -> dict[str, Any]:
    _log_refused(request_id, partner_id, reason)
    return {"ok": False, "reason": reason}


def _hard_refusals(figured: dict[str, Any], reason: str | None) -> str | None:
    codes = [str(code) for code in figured["refusals"]]
    needs_reason = "reason_required" in codes
    hard = [code for code in codes if code != "reason_required"]
    if hard:
        return hard[0]
    if needs_reason and not _reason_ok(reason):
        return "reason_required"
    return None


def _ref_fields(
    *,
    ref_status: str,
    ref_rate: Decimal | None,
    ref_source: str | None,
    ref_fetched_at_ms: int | None,
    deviation_bps: int | None,
) -> dict[str, Any] | str:
    if ref_status not in _REF_STATUSES:
        return "ref_shape"
    if ref_status == _REF_OK:
        if ref_rate is None or not str(ref_source or "").strip() or ref_fetched_at_ms is None:
            return "ref_shape"
        ref_e4 = _rate_e4(Decimal(ref_rate))
        if ref_e4 is None or ref_e4 <= 0 or deviation_bps is None:
            return "ref_shape"
        return {
            "ref_status": ref_status,
            "ref_rate_e4": ref_e4,
            "ref_source": str(ref_source),
            "ref_fetched_at_ms": int(ref_fetched_at_ms),
            "deviation_bps": int(deviation_bps),
        }
    if ref_rate is not None:
        return "ref_shape"
    return {
        "ref_status": ref_status,
        "ref_rate_e4": None,
        "ref_source": ref_source,
        "ref_fetched_at_ms": ref_fetched_at_ms,
        "deviation_bps": None,
    }


def _insert_fx(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    partner_id: str,
    version: int,
    supersedes_id: str | None,
    paid_kyc_id: str | None,
    usdt_payout_micro: int,
    carry_in_micro: int,
    chain_fee_micro: int,
    usdt_sold_micro: int,
    usdt_leftover_micro: int,
    rate_e4: int,
    gross_satang: int,
    trade_fee_satang: int,
    thb_wd_fee_satang: int,
    bank_fee_satang: int,
    residual_satang: int,
    net_satang: int,
    ref: dict[str, Any],
    ref_error: str | None,
    reason: str | None,
    slip_ref: str | None,
    sale_ref: str | None,
    entered_by: str,
    now_ms: int,
) -> str:
    fx_id = _new_fx_id()
    conn.execute(
        """
        INSERT INTO partner_payout_fx (
            id, request_id, partner_id, version, supersedes_id, paid_kyc_id,
            usdt_payout_micro, carry_in_micro, chain_fee_micro, usdt_sold_micro,
            usdt_leftover_micro, rate_e4, gross_satang, trade_fee_satang,
            thb_wd_fee_satang, bank_fee_satang, residual_satang, residual_tol_satang,
            net_satang, ref_status, ref_rate_e4, ref_source, ref_fetched_at_ms,
            ref_error, deviation_bps, threshold_bps, reason, slip_ref, sale_ref,
            entered_by, entered_at_ms
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?
        )
        """,
        (
            fx_id,
            request_id,
            partner_id,
            int(version),
            supersedes_id,
            paid_kyc_id,
            int(usdt_payout_micro),
            int(carry_in_micro),
            int(chain_fee_micro),
            int(usdt_sold_micro),
            int(usdt_leftover_micro),
            int(rate_e4),
            int(gross_satang),
            int(trade_fee_satang),
            int(thb_wd_fee_satang),
            int(bank_fee_satang),
            int(residual_satang),
            RESIDUAL_TOL_SATANG,
            int(net_satang),
            ref["ref_status"],
            ref["ref_rate_e4"],
            ref["ref_source"],
            ref["ref_fetched_at_ms"],
            ref_error,
            ref["deviation_bps"],
            DEVIATION_THRESHOLD_BPS,
            reason,
            slip_ref,
            sale_ref,
            str(entered_by),
            int(now_ms),
        ),
    )
    return fx_id


def _post_carry(
    conn: sqlite3.Connection,
    *,
    partner_id: str,
    kind: str,
    amount_micro: int,
    fx_id: str | None,
    reverses_id: str | None,
    reason: str | None,
    entered_by: str,
    now_ms: int,
) -> int:
    row = conn.execute(
        """
        SELECT seq, balance_after_micro
        FROM partner_usdt_carry
        WHERE partner_id = ?
        ORDER BY seq DESC
        LIMIT 1
        """,
        (partner_id,),
    ).fetchone()
    seq = 1 if row is None else int(row["seq"]) + 1
    prev = 0 if row is None else int(row["balance_after_micro"])
    after = prev + int(amount_micro)
    if after < 0:
        raise _FxAbort("negative_balance")
    conn.execute(
        """
        INSERT INTO partner_usdt_carry (
            id, partner_id, seq, kind, amount_micro, balance_after_micro,
            fx_id, reverses_id, reason, entered_by, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_carry_id(),
            partner_id,
            seq,
            kind,
            int(amount_micro),
            after,
            fx_id,
            reverses_id,
            reason,
            str(entered_by),
            int(now_ms),
        ),
    )
    return after


def _post_apply_and_leftover(
    conn: sqlite3.Connection,
    *,
    partner_id: str,
    fx_id: str,
    carry_in_micro: int,
    leftover_micro: int,
    entered_by: str,
    now_ms: int,
) -> int:
    balance = carry_balance(conn, partner_id)
    if carry_in_micro > 0:
        balance = _post_carry(
            conn,
            partner_id=partner_id,
            kind="apply",
            amount_micro=-int(carry_in_micro),
            fx_id=fx_id,
            reverses_id=None,
            reason=None,
            entered_by=entered_by,
            now_ms=now_ms,
        )
    if leftover_micro > 0:
        balance = _post_carry(
            conn,
            partner_id=partner_id,
            kind="leftover",
            amount_micro=int(leftover_micro),
            fx_id=fx_id,
            reverses_id=None,
            reason=None,
            entered_by=entered_by,
            now_ms=now_ms,
        )
    return balance


def _figures_or_reason(
    *,
    request_id: str,
    partner_id: str,
    usdt_payout_micro: int,
    carry_in_micro: int,
    chain_fee_micro: int,
    usdt_sold_micro: int,
    rate: Decimal,
    trade_fee_satang: int,
    thb_wd_fee_satang: int,
    bank_fee_satang: int,
    net_satang: int,
    ref_status: str,
    ref_rate: Decimal | None,
    ref_source: str | None,
    ref_fetched_at_ms: int | None,
    reason: str | None,
    correction: bool,
) -> tuple[dict[str, Any], dict[str, Any]] | dict[str, Any]:
    figured = compute(
        usdt_payout_micro=usdt_payout_micro,
        carry_in_micro=carry_in_micro,
        chain_fee_micro=chain_fee_micro,
        usdt_sold_micro=usdt_sold_micro,
        rate=rate,
        trade_fee_satang=trade_fee_satang,
        thb_wd_fee_satang=thb_wd_fee_satang,
        bank_fee_satang=bank_fee_satang,
        net_satang=net_satang,
        ref_rate=ref_rate if ref_status == _REF_OK else None,
        correction=correction,
    )
    code = _hard_refusals(figured, reason)
    if code is not None:
        return _fail(request_id, partner_id, code)
    if figured["gross_satang"] is None or figured["residual_satang"] is None or figured["rate_e4"] is None:
        return _fail(request_id, partner_id, "bad_input")
    if int(figured["leftover_micro"]) < 0 or int(figured["gross_satang"]) <= 0 or int(net_satang) <= 0:
        return _fail(request_id, partner_id, "bad_input")
    ref = _ref_fields(
        ref_status=ref_status,
        ref_rate=ref_rate,
        ref_source=ref_source,
        ref_fetched_at_ms=ref_fetched_at_ms,
        deviation_bps=figured["deviation_bps"],
    )
    if isinstance(ref, str):
        return _fail(request_id, partner_id, ref)
    return figured, ref


def record_payout_fx(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    carry_in_micro: int,
    chain_fee_micro: int,
    usdt_sold_micro: int,
    rate: Decimal,
    trade_fee_satang: int,
    thb_wd_fee_satang: int,
    bank_fee_satang: int,
    net_satang: int,
    ref_status: str,
    ref_rate: Decimal | None = None,
    ref_source: str | None = None,
    ref_fetched_at_ms: int | None = None,
    ref_error: str | None = None,
    reason: str | None = None,
    slip_ref: str | None = None,
    sale_ref: str | None = None,
    paid_kyc_id: str | None = None,
    entered_by: str,
    now_ms: int,
) -> dict[str, Any]:
    """Version 1 for a request that is already paid. Does not call mark_paid.

    ``carry_in_micro`` must equal the partner's carry balance. The whole
    balance is applied, then any new leftover is posted.
    """
    try:
        with transaction(conn):
            req = conn.execute(
                "SELECT * FROM partner_request WHERE id = ?",
                (str(request_id),),
            ).fetchone()
            if req is None:
                return _fail(request_id, None, "not_found")
            partner_id = str(req["partner_id"])
            if str(req["status"]) != "confirmed" or str(req["payout_state"]) != "paid":
                return _fail(request_id, partner_id, "not_paid")
            if str(req["kind"]) not in ("withdraw_profit", "close"):
                return _fail(request_id, partner_id, "bad_kind")
            payout = req["payout_micro"]
            if payout is None or int(payout) <= 0:
                return _fail(request_id, partner_id, "bad_kind")
            balance = carry_balance(conn, partner_id)
            if int(carry_in_micro) != balance:
                return _fail(request_id, partner_id, "carry_mismatch")
            prepared = _figures_or_reason(
                request_id=str(request_id),
                partner_id=partner_id,
                usdt_payout_micro=int(payout),
                carry_in_micro=int(carry_in_micro),
                chain_fee_micro=int(chain_fee_micro),
                usdt_sold_micro=int(usdt_sold_micro),
                rate=rate,
                trade_fee_satang=int(trade_fee_satang),
                thb_wd_fee_satang=int(thb_wd_fee_satang),
                bank_fee_satang=int(bank_fee_satang),
                net_satang=int(net_satang),
                ref_status=ref_status,
                ref_rate=ref_rate,
                ref_source=ref_source,
                ref_fetched_at_ms=ref_fetched_at_ms,
                reason=reason,
                correction=False,
            )
            if not isinstance(prepared, tuple):
                return prepared
            figured, ref = prepared
            fx_id = _insert_fx(
                conn,
                request_id=str(request_id),
                partner_id=partner_id,
                version=1,
                supersedes_id=None,
                paid_kyc_id=paid_kyc_id,
                usdt_payout_micro=int(payout),
                carry_in_micro=int(carry_in_micro),
                chain_fee_micro=int(chain_fee_micro),
                usdt_sold_micro=int(usdt_sold_micro),
                usdt_leftover_micro=int(figured["leftover_micro"]),
                rate_e4=int(figured["rate_e4"]),
                gross_satang=int(figured["gross_satang"]),
                trade_fee_satang=int(trade_fee_satang),
                thb_wd_fee_satang=int(thb_wd_fee_satang),
                bank_fee_satang=int(bank_fee_satang),
                residual_satang=int(figured["residual_satang"]),
                net_satang=int(net_satang),
                ref=ref,
                ref_error=ref_error,
                reason=reason,
                slip_ref=slip_ref,
                sale_ref=sale_ref,
                entered_by=entered_by,
                now_ms=now_ms,
            )
            carry_after = _post_apply_and_leftover(
                conn,
                partner_id=partner_id,
                fx_id=fx_id,
                carry_in_micro=int(carry_in_micro),
                leftover_micro=int(figured["leftover_micro"]),
                entered_by=entered_by,
                now_ms=now_ms,
            )
            logger.info(
                "partner fx recorded request_id=%s partner_id=%s version=%s",
                request_id,
                partner_id,
                1,
            )
            return {"ok": True, "fx_id": fx_id, "version": 1, "carry_after": carry_after}
    except _FxAbort as exc:
        return _fail(request_id, None, exc.reason)
    except sqlite3.IntegrityError:
        return _fail(request_id, None, "db_constraint")


def _base_carry_rows(conn: sqlite3.Connection, fx_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM partner_usdt_carry
            WHERE fx_id = ? AND kind IN ('apply', 'leftover')
            ORDER BY seq DESC
            """,
            (fx_id,),
        ).fetchall()
    )


def record_correction(
    conn: sqlite3.Connection,
    *,
    base_fx_id: str,
    carry_in_micro: int,
    chain_fee_micro: int,
    usdt_sold_micro: int,
    rate: Decimal,
    trade_fee_satang: int,
    thb_wd_fee_satang: int,
    bank_fee_satang: int,
    net_satang: int,
    ref_status: str,
    ref_rate: Decimal | None = None,
    ref_source: str | None = None,
    ref_fetched_at_ms: int | None = None,
    ref_error: str | None = None,
    reason: str | None = None,
    slip_ref: str | None = None,
    sale_ref: str | None = None,
    paid_kyc_id: str | None = None,
    entered_by: str,
    now_ms: int,
) -> dict[str, Any]:
    """Append version n+1. Reverse the base row's carry, then post the new one.

    A later payout that already used this leftover blocks the correction
    (``carry_consumed``) unless carry_in and leftover stay the same. In that
    case the carry ledger is left as it is: appending a reverse after the
    later payout would drive the running balance negative, and the net
    carry effect of an unchanged pair is zero.
    """
    try:
        with transaction(conn):
            base = conn.execute(
                "SELECT * FROM partner_payout_fx WHERE id = ?",
                (str(base_fx_id),),
            ).fetchone()
            if base is None:
                return _fail(None, None, "not_found")
            request_id = str(base["request_id"])
            partner_id = str(base["partner_id"])
            current = conn.execute(
                "SELECT MAX(version) AS v FROM partner_payout_fx WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if current is None or int(current["v"]) != int(base["version"]):
                return _fail(request_id, partner_id, "not_current")
            prepared = _figures_or_reason(
                request_id=request_id,
                partner_id=partner_id,
                usdt_payout_micro=int(base["usdt_payout_micro"]),
                carry_in_micro=int(carry_in_micro),
                chain_fee_micro=int(chain_fee_micro),
                usdt_sold_micro=int(usdt_sold_micro),
                rate=rate,
                trade_fee_satang=int(trade_fee_satang),
                thb_wd_fee_satang=int(thb_wd_fee_satang),
                bank_fee_satang=int(bank_fee_satang),
                net_satang=int(net_satang),
                ref_status=ref_status,
                ref_rate=ref_rate,
                ref_source=ref_source,
                ref_fetched_at_ms=ref_fetched_at_ms,
                reason=reason,
                correction=True,
            )
            if not isinstance(prepared, tuple):
                return prepared
            figured, ref = prepared
            new_leftover = int(figured["leftover_micro"])
            last = conn.execute(
                "SELECT MAX(seq) AS s FROM partner_usdt_carry WHERE fx_id = ?",
                (str(base_fx_id),),
            ).fetchone()
            last_seq = None if last is None or last["s"] is None else int(last["s"])
            later = False
            if last_seq is not None:
                later_row = conn.execute(
                    """
                    SELECT 1 FROM partner_usdt_carry
                    WHERE partner_id = ? AND seq > ?
                    LIMIT 1
                    """,
                    (partner_id, last_seq),
                ).fetchone()
                later = later_row is not None
            same_carry = int(carry_in_micro) == int(base["carry_in_micro"]) and new_leftover == int(
                base["usdt_leftover_micro"]
            )
            if later and not same_carry:
                return _fail(request_id, partner_id, "carry_consumed")
            version = int(base["version"]) + 1
            fx_id = _insert_fx(
                conn,
                request_id=request_id,
                partner_id=partner_id,
                version=version,
                supersedes_id=str(base["id"]),
                paid_kyc_id=paid_kyc_id if paid_kyc_id is not None else base["paid_kyc_id"],
                usdt_payout_micro=int(base["usdt_payout_micro"]),
                carry_in_micro=int(carry_in_micro),
                chain_fee_micro=int(chain_fee_micro),
                usdt_sold_micro=int(usdt_sold_micro),
                usdt_leftover_micro=new_leftover,
                rate_e4=int(figured["rate_e4"]),
                gross_satang=int(figured["gross_satang"]),
                trade_fee_satang=int(trade_fee_satang),
                thb_wd_fee_satang=int(thb_wd_fee_satang),
                bank_fee_satang=int(bank_fee_satang),
                residual_satang=int(figured["residual_satang"]),
                net_satang=int(net_satang),
                ref=ref,
                ref_error=ref_error,
                reason=reason,
                slip_ref=slip_ref,
                sale_ref=sale_ref,
                entered_by=entered_by,
                now_ms=now_ms,
            )
            if not (later and same_carry):
                for entry in _base_carry_rows(conn, str(base["id"])):
                    _post_carry(
                        conn,
                        partner_id=partner_id,
                        kind="reverse",
                        amount_micro=-int(entry["amount_micro"]),
                        fx_id=fx_id,
                        reverses_id=str(entry["id"]),
                        reason=None,
                        entered_by=entered_by,
                        now_ms=now_ms,
                    )
                carry_after = _post_apply_and_leftover(
                    conn,
                    partner_id=partner_id,
                    fx_id=fx_id,
                    carry_in_micro=int(carry_in_micro),
                    leftover_micro=new_leftover,
                    entered_by=entered_by,
                    now_ms=now_ms,
                )
            else:
                carry_after = carry_balance(conn, partner_id)
            logger.info(
                "partner fx recorded request_id=%s partner_id=%s version=%s",
                request_id,
                partner_id,
                version,
            )
            return {"ok": True, "fx_id": fx_id, "version": version, "carry_after": carry_after}
    except _FxAbort as exc:
        return _fail(None, None, exc.reason)
    except sqlite3.IntegrityError:
        return _fail(None, None, "db_constraint")


def settle_carry(
    conn: sqlite3.Connection,
    partner_id: str,
    amount_micro: int,
    reason: str | None,
    entered_by: str,
    now_ms: int,
) -> dict[str, Any]:
    """Owner settlement of a remaining leftover. ``amount_micro`` is positive."""
    try:
        with transaction(conn):
            amount = _as_int(amount_micro)
            if amount is None or amount <= 0:
                return _fail(None, partner_id, "bad_amount")
            if not _reason_ok(reason):
                return _fail(None, partner_id, "reason_required")
            balance = carry_balance(conn, partner_id)
            if amount > balance:
                return _fail(None, partner_id, "settle_exceeds")
            after = _post_carry(
                conn,
                partner_id=str(partner_id),
                kind="settle",
                amount_micro=-amount,
                fx_id=None,
                reverses_id=None,
                reason=str(reason).strip(),
                entered_by=entered_by,
                now_ms=now_ms,
            )
            logger.info("partner fx settled partner_id=%s reason=settle", partner_id)
            return {"ok": True, "carry_after": after}
    except _FxAbort as exc:
        return _fail(None, partner_id, exc.reason)
    except sqlite3.IntegrityError:
        return _fail(None, partner_id, "db_constraint")


def _dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def current_fx(conn: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM partner_payout_fx
        WHERE request_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (str(request_id),),
    ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def fx_history(conn: sqlite3.Connection, request_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM partner_payout_fx
        WHERE request_id = ?
        ORDER BY version
        """,
        (str(request_id),),
    ).fetchall()
    return _dicts(list(rows))


def carry_entries(conn: sqlite3.Connection, partner_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM partner_usdt_carry
        WHERE partner_id = ?
        ORDER BY seq
        """,
        (str(partner_id),),
    ).fetchall()
    return _dicts(list(rows))


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        raise TypeError("float")
    return value


def stage_fx_draft(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    mode: str,
    payload: dict[str, Any],
    created_by: str,
    now_ms: int,
    base_fx_id: str | None = None,
    admin_message_id: int | None = None,
) -> dict[str, Any]:
    """Replace the confirm draft for one request. Numbers are stored as strings."""
    if mode not in ("paid", "fix"):
        return _fail(request_id, None, "bad_mode")
    try:
        body = json.dumps(_json_ready(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return _fail(request_id, None, "bad_payload")
    token = secrets.token_hex(4)
    expires = int(now_ms) + DRAFT_TTL_MS
    try:
        with transaction(conn):
            found = conn.execute(
                "SELECT partner_id FROM partner_request WHERE id = ?",
                (str(request_id),),
            ).fetchone()
            if found is None:
                return _fail(request_id, None, "not_found")
            conn.execute(
                """
                INSERT INTO partner_payout_fx_draft (
                    request_id, token, mode, base_fx_id, payload_json,
                    admin_message_id, created_by, created_at_ms, expires_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    token = excluded.token,
                    mode = excluded.mode,
                    base_fx_id = excluded.base_fx_id,
                    payload_json = excluded.payload_json,
                    admin_message_id = excluded.admin_message_id,
                    created_by = excluded.created_by,
                    created_at_ms = excluded.created_at_ms,
                    expires_at_ms = excluded.expires_at_ms
                """,
                (
                    str(request_id),
                    token,
                    mode,
                    base_fx_id,
                    body,
                    admin_message_id,
                    str(created_by),
                    int(now_ms),
                    expires,
                ),
            )
            return {"ok": True, "token": token, "expires_at_ms": expires}
    except sqlite3.IntegrityError:
        return _fail(request_id, None, "db_constraint")


def load_fx_draft(
    conn: sqlite3.Connection,
    request_id: str,
    token: str,
    now_ms: int,
) -> dict[str, Any] | None:
    """The matching draft, or None when it is missing, mismatched, or expired."""
    row = conn.execute(
        "SELECT * FROM partner_payout_fx_draft WHERE request_id = ?",
        (str(request_id),),
    ).fetchone()
    if row is None or str(row["token"]) != str(token) or int(row["expires_at_ms"]) <= int(now_ms):
        return None
    out = {key: row[key] for key in row.keys()}
    out["payload"] = json.loads(str(row["payload_json"]))
    return out


def delete_fx_draft(conn: sqlite3.Connection, request_id: str) -> bool:
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM partner_payout_fx_draft WHERE request_id = ?",
            (str(request_id),),
        )
        return int(cur.rowcount) == 1
