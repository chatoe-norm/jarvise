"""Read-only Binance transfer vs partner-ledger reconciliation (P4).

Alert/report only. Never writes ledger rows. Never places orders.
Partners never see this output — admin Telegram only via default send_message.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jarvise_assist.partner_store import units_from_e8, usdt_from_micro, verify_ledger
from jarvise_assist.pause import BANGKOK, TRANSFER_INCOME_TYPES
from jarvise_assist.real_trades import time_windows

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

# Owner-tunable constants (defaults locked for P4).
EXACT_TOL = Decimal("0.01")
NEAR_ABS = Decimal("1")
NEAR_PCT = Decimal("0.005")
DEP_BEFORE_MS = 72 * HOUR_MS
DEP_AFTER_MS = 15 * 60_000
PAY_BEFORE_MS = 72 * HOUR_MS
PAY_AFTER_MS = 72 * HOUR_MS
GRACE_BINANCE_MS = 24 * HOUR_MS
GRACE_DEPOSIT_MS = 1 * HOUR_MS
GRACE_PAID_MS = 24 * HOUR_MS
GRACE_DUE_OUTFLOW_MS = 24 * HOUR_MS
JOB_DAYS = 14
CLI_DAYS_DEFAULT = 30
MAX_DAYS = 90
PAD_MS = 72 * HOUR_MS
BINANCE_HISTORY_MS = 90 * DAY_MS
FAIL_ALERT_AFTER_MS = 47 * HOUR_MS
MAX_GROUP = 4
MAX_GROUP_CANDIDATES = 8
ALERT_MAX_LINES = 10
PRUNE_MS = 120 * DAY_MS
GUARD_MAX_LINES = 6
GUARD_WARN_CODES = frozenset(
    {
        "transfer_in_unrecorded",
        "transfer_out_unrecorded",
        "deposit_no_transfer",
        "paid_no_outflow",
        "due_but_paid_out",
        "verify_mismatch",
        "this_deposit_not_seen",
        "this_deposit_amount_diff",
    }
)
_GUARD_FETCH_TH: dict[str, str] = {
    "no_client": "ไม่มีคีย์อ่าน Binance",
    "read_failed": "อ่าน Binance ไม่สำเร็จ",
    "incomplete": "ข้อมูลเกิน 1000 แถว",
    "bad_payload": "ข้อมูล Binance ผิดรูปแบบ",
    "error": "ระบบตรวจผิดพลาด",
}

SendFn = Callable[[str], Any]
_TWO_DP = Decimal("0.01")
_FORMULA_PREFIX = frozenset({"=", "+", "-", "@"})
_UTC = UTC


@dataclass(frozen=True)
class Transfer:
    time_ms: int
    amount: Decimal  # signed: +in / -out
    asset: str
    income_type: str
    tran_id: str


@dataclass(frozen=True)
class LedgerCash:
    ref: str  # "ev:<id>" | "pr:<id>"
    kind: str  # "deposit" | "payout" | "owner_redeem" | "owner_deposit"
    direction: int  # +1 in / -1 out
    partner_name: str
    amount: Decimal  # always > 0
    t_ref_ms: int
    t_lo_ms: int
    t_hi_ms: int
    payout_state: str | None
    paid_at_ms: int | None


@dataclass(frozen=True)
class Finding:
    code: str
    fingerprint: str
    time_ms: int
    amount: Decimal
    partner_name: str | None
    detail: str
    alertable: bool


@dataclass(frozen=True)
class AuditResult:
    status: str
    ledger_start_ms: int | None
    report_start_ms: int | None
    findings: list[Finding] = field(default_factory=list)
    matched_n: int = 0
    pending_n: int = 0
    pre_init_ignored_n: int = 0
    binance_in: Decimal = Decimal("0")
    binance_out: Decimal = Decimal("0")
    ledger_in: Decimal = Decimal("0")
    ledger_out: Decimal = Decimal("0")
    error: str | None = None


@dataclass(frozen=True)
class FetchResult:
    status: str
    transfers: list[Transfer] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class GuardResult:
    status: str  # ok | not_init | no_client | read_failed | incomplete | bad_payload | error
    checked_at_ms: int
    findings: list[Finding]
    warn: bool
    error: str | None = None


def _finite_decimal(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite():
        return None
    return value


def _near_tol(amount: Decimal) -> Decimal:
    pct = NEAR_PCT * amount
    return NEAR_ABS if NEAR_ABS >= pct else pct


def _fmt_amt(value: Decimal) -> str:
    return f"{abs(value).quantize(_TWO_DP)}"


def _fmt_bkk(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=BANGKOK).strftime("%d/%m %H:%M")


def _fmt_utc(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _csv_text(value: str) -> str:
    if value and value[0] in _FORMULA_PREFIX:
        return "'" + value
    return value


def ledger_start_ms(conn: sqlite3.Connection) -> int | None:
    """Created_at_ms of the first partner_event (p init), or None if not initialized."""
    row = conn.execute(
        """
        SELECT created_at_ms
        FROM partner_event
        ORDER BY created_at_ms ASC, rowid ASC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return int(row["created_at_ms"] if hasattr(row, "keys") else row[0])


def _init_event_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT id
        FROM partner_event
        ORDER BY created_at_ms ASC, rowid ASC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["id"] if hasattr(row, "keys") else row[0])


def fetch_transfers(
    client: Any,
    start_ms: int,
    end_ms: int,
    *,
    now_ms: int | None = None,
) -> FetchResult:
    """Fetch transfer income rows. Fail-fast on the first problem."""
    if client is None:
        return FetchResult(status="no_client")
    now = int(now_ms if now_ms is not None else end_ms)
    clamped_start = max(int(start_ms), now - BINANCE_HISTORY_MS)
    end = int(end_ms)
    if end < clamped_start:
        return FetchResult(status="ok", transfers=[])

    seen: set[tuple[str, ...]] = set()
    out: list[Transfer] = []
    page_cap = 100
    pages = 0
    for income_type in sorted(TRANSFER_INCOME_TYPES):
        for win_start, win_end in time_windows(clamped_start, end + 1):
            cursor = int(win_start)
            win_end_incl = int(win_end) - 1
            while cursor <= win_end_incl:
                if pages >= page_cap:
                    return FetchResult(status="incomplete")
                try:
                    page = client.income_rows(
                        start_ms=cursor,
                        end_ms=win_end_incl,
                        income_type=income_type,
                        limit=1000,
                    )
                except Exception as exc:  # noqa: BLE001
                    return FetchResult(status="read_failed", error=type(exc).__name__)
                pages += 1
                if not page:
                    break
                max_time = cursor
                for row in page:
                    if not isinstance(row, dict):
                        return FetchResult(status="bad_payload")
                    time_raw = row.get("time")
                    if time_raw is None:
                        return FetchResult(status="bad_payload")
                    try:
                        time_val = int(time_raw)
                    except (TypeError, ValueError):
                        return FetchResult(status="bad_payload")
                    if time_val > max_time:
                        max_time = time_val
                    amount = _finite_decimal(row.get("income"))
                    if amount is None:
                        return FetchResult(status="bad_payload")
                    asset = str(row.get("asset") or "").strip()
                    if not asset:
                        return FetchResult(status="bad_payload")
                    row_type = str(row.get("incomeType") or "")
                    if row_type != income_type:
                        continue
                    if amount == 0:
                        continue
                    tran_raw = row.get("tranId")
                    tran_id = str(tran_raw) if tran_raw is not None and str(tran_raw) != "" else ""
                    if tran_id:
                        key: tuple[str, ...] = (income_type, tran_id)
                    else:
                        key = (income_type, str(time_val), str(amount))
                        tran_id = f"{time_val}:{amount}"
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(
                        Transfer(
                            time_ms=time_val,
                            amount=amount,
                            asset=asset,
                            income_type=income_type,
                            tran_id=tran_id,
                        )
                    )
                if len(page) < 1000:
                    break
                if max_time > cursor:
                    cursor = max_time
                    continue
                return FetchResult(status="incomplete")
    out.sort(key=lambda t: (t.time_ms, t.income_type, t.tran_id))
    return FetchResult(status="ok", transfers=out)


def ledger_cash(conn: sqlite3.Connection, from_ms: int, now_ms: int) -> list[LedgerCash]:
    """SELECT-only cash flows used for matching. Never writes."""
    init_id = _init_event_id(conn)
    items: list[LedgerCash] = []
    dep_rows = conn.execute(
        """
        SELECT e.id, e.created_at_ms, e.cash_micro, p.name AS partner_name
        FROM partner_event e
        JOIN partner p ON p.id = e.partner_id
        WHERE e.kind = 'deposit'
          AND e.created_at_ms >= ?
          AND e.created_at_ms <= ?
        ORDER BY e.created_at_ms ASC, e.rowid ASC
        """,
        (int(from_ms), int(now_ms)),
    ).fetchall()
    for row in dep_rows:
        eid = str(row["id"])
        if init_id is not None and eid == init_id:
            continue
        amount = usdt_from_micro(int(row["cash_micro"]))
        if amount <= 0:
            continue
        t_ref = int(row["created_at_ms"])
        items.append(
            LedgerCash(
                ref=f"ev:{eid}",
                kind="deposit",
                direction=1,
                partner_name=str(row["partner_name"]),
                amount=amount,
                t_ref_ms=t_ref,
                t_lo_ms=t_ref - DEP_BEFORE_MS,
                t_hi_ms=t_ref + DEP_AFTER_MS,
                payout_state=None,
                paid_at_ms=None,
            )
        )

    pay_rows = conn.execute(
        """
        SELECT r.id, r.decided_at_ms, r.paid_at_ms, r.payout_state, r.payout_micro,
               p.name AS partner_name
        FROM partner_request r
        JOIN partner p ON p.id = r.partner_id
        WHERE r.status = 'confirmed'
          AND r.payout_state IN ('due', 'paid')
          AND r.payout_micro IS NOT NULL
          AND r.payout_micro > 0
          AND (
            r.payout_state = 'due'
            OR (r.decided_at_ms IS NOT NULL AND r.decided_at_ms >= ? AND r.decided_at_ms <= ?)
            OR (r.paid_at_ms IS NOT NULL AND r.paid_at_ms >= ? AND r.paid_at_ms <= ?)
          )
        ORDER BY r.decided_at_ms ASC, r.id ASC
        """,
        (int(from_ms), int(now_ms), int(from_ms), int(now_ms)),
    ).fetchall()
    for row in pay_rows:
        decided = row["decided_at_ms"]
        if decided is None:
            continue
        t_ref = int(decided)
        paid_raw = row["paid_at_ms"]
        paid_at = int(paid_raw) if paid_raw is not None else None
        hi_base = paid_at if paid_at is not None else int(now_ms)
        amount = usdt_from_micro(int(row["payout_micro"]))
        if amount <= 0:
            continue
        items.append(
            LedgerCash(
                ref=f"pr:{str(row['id'])}",
                kind="payout",
                direction=-1,
                partner_name=str(row["partner_name"]),
                amount=amount,
                t_ref_ms=t_ref,
                t_lo_ms=t_ref - PAY_BEFORE_MS,
                t_hi_ms=hi_base + PAY_AFTER_MS,
                payout_state=str(row["payout_state"]),
                paid_at_ms=paid_at,
            )
        )

    owner_rows = conn.execute(
        """
        SELECT e.id, e.kind, e.cash_micro,
               COALESCE(e.effective_at_ms, e.created_at_ms) AS t_ref,
               p.name AS partner_name
        FROM partner_event e
        JOIN partner p ON p.id = e.partner_id
        WHERE e.kind IN ('owner_redeem', 'owner_deposit')
          AND COALESCE(e.effective_at_ms, e.created_at_ms) >= ?
          AND COALESCE(e.effective_at_ms, e.created_at_ms) <= ?
        ORDER BY t_ref ASC, e.rowid ASC
        """,
        (int(from_ms), int(now_ms)),
    ).fetchall()
    for row in owner_rows:
        amount = abs(usdt_from_micro(int(row["cash_micro"])))
        if amount == 0:
            continue
        kind = str(row["kind"])
        direction = -1 if kind == "owner_redeem" else 1
        t_ref = int(row["t_ref"])
        items.append(
            LedgerCash(
                ref=f"ev:{str(row['id'])}",
                kind=kind,
                direction=direction,
                partner_name=str(row["partner_name"]),
                amount=amount,
                t_ref_ms=t_ref,
                t_lo_ms=t_ref - DEP_BEFORE_MS,
                t_hi_ms=t_ref + DEP_AFTER_MS,
                payout_state=None,
                paid_at_ms=None,
            )
        )
    return items


def _xfer_dir(t: Transfer) -> int:
    return 1 if t.amount > 0 else -1


def _abs_amt(t: Transfer) -> Decimal:
    return abs(t.amount)


def _in_window(t: Transfer, item: LedgerCash) -> bool:
    return item.t_lo_ms <= t.time_ms <= item.t_hi_ms and _xfer_dir(t) == item.direction


def _pair_diff(t: Transfer, item: LedgerCash) -> Decimal:
    return abs(_abs_amt(t) - item.amount)


def _time_gap(t: Transfer, item: LedgerCash) -> int:
    return abs(int(t.time_ms) - int(item.t_ref_ms))


def _group_time_gap(transfers: list[Transfer], items: list[LedgerCash]) -> int:
    t_avg = sum(x.time_ms for x in transfers) // len(transfers)
    i_avg = sum(x.t_ref_ms for x in items) // len(items)
    return abs(t_avg - i_avg)


def _ids_key(transfers: list[Transfer], items: list[LedgerCash]) -> tuple[str, ...]:
    return tuple(sorted(t.tran_id for t in transfers) + sorted(i.ref for i in items))


def reconcile(
    transfers: list[Transfer],
    items: list[LedgerCash],
    *,
    now_ms: int,
    ledger_start_ms: int,
    report_start_ms: int,
    coverage_start_ms: int | None = None,
) -> AuditResult:
    """Pure deterministic matching. PAD-zone rows match but do not create findings."""
    pre_init = 0
    usable: list[Transfer] = []
    findings: list[Finding] = []

    for t in transfers:
        if t.time_ms < ledger_start_ms:
            pre_init += 1
            continue
        if t.asset.upper() != "USDT":
            if t.time_ms >= report_start_ms:
                findings.append(
                    Finding(
                        code="non_usdt",
                        fingerprint=f"asset:{t.income_type}:{t.tran_id}",
                        time_ms=t.time_ms,
                        amount=_abs_amt(t),
                        partner_name=None,
                        detail=t.asset,
                        alertable=True,
                    )
                )
            continue
        usable.append(t)

    unmatched_t = set(range(len(usable)))
    unmatched_i = set(range(len(items)))
    matched_n = 0
    due_outflow_ms: dict[str, int] = {}

    def mark_pair(ti: int, ii: int, *, near: bool) -> None:
        nonlocal matched_n
        unmatched_t.discard(ti)
        unmatched_i.discard(ii)
        matched_n += 1
        t = usable[ti]
        item = items[ii]
        if item.kind == "payout" and item.payout_state == "due":
            due_outflow_ms[item.ref] = t.time_ms
        if near and max(t.time_ms, item.t_ref_ms) >= report_start_ms:
            diff = _abs_amt(t) - item.amount
            findings.append(
                Finding(
                    code="near_amount",
                    fingerprint=f"near:{t.tran_id}:{item.ref}",
                    time_ms=t.time_ms,
                    amount=diff,
                    partner_name=item.partner_name,
                    detail=str(diff),
                    alertable=True,
                )
            )

    # Pass 1: exact 1:1
    exact_cands: list[tuple[Decimal, int, str, str, int, int]] = []
    for ti in unmatched_t:
        t = usable[ti]
        for ii in unmatched_i:
            item = items[ii]
            if not _in_window(t, item):
                continue
            diff = _pair_diff(t, item)
            if diff <= EXACT_TOL:
                exact_cands.append((diff, _time_gap(t, item), t.tran_id, item.ref, ti, ii))
    exact_cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    for _d, _g, _tid, _ref, ti, ii in exact_cands:
        if ti in unmatched_t and ii in unmatched_i:
            mark_pair(ti, ii, near=False)

    def run_group_pass(*, exact_only: bool) -> None:
        nonlocal matched_n
        group_cands: list[tuple[Decimal, int, tuple[str, ...], list[int], list[int], bool]] = []

        def consider_group(t_idxs: list[int], i_idxs: list[int]) -> None:
            ts = [usable[i] for i in t_idxs]
            its = [items[i] for i in i_idxs]
            if len(ts) == 1:
                if not all(_in_window(ts[0], it) for it in its):
                    return
            else:
                if not all(_in_window(t, its[0]) for t in ts):
                    return
            sum_t = sum((_abs_amt(t) for t in ts), Decimal("0"))
            sum_i = sum((it.amount for it in its), Decimal("0"))
            diff = abs(sum_t - sum_i)
            exact = diff <= EXACT_TOL
            near = (not exact) and diff <= _near_tol(sum_i if sum_i > 0 else sum_t)
            if exact_only:
                if not exact:
                    return
                near = False
            elif not exact and not near:
                return
            gap = _group_time_gap(ts, its)
            group_cands.append((diff, gap, _ids_key(ts, its), list(t_idxs), list(i_idxs), near))

        for ti in list(unmatched_t):
            t = usable[ti]
            pool = [ii for ii in unmatched_i if _in_window(t, items[ii])]
            pool.sort(key=lambda ii: (_time_gap(t, items[ii]), items[ii].ref))
            pool = pool[:MAX_GROUP_CANDIDATES]
            for k in range(2, min(MAX_GROUP, len(pool)) + 1):
                for combo in itertools.combinations(pool, k):
                    consider_group([ti], list(combo))

        for ii in list(unmatched_i):
            item = items[ii]
            pool = [ti for ti in unmatched_t if _in_window(usable[ti], item)]
            pool.sort(key=lambda ti: (_time_gap(usable[ti], item), usable[ti].tran_id))
            pool = pool[:MAX_GROUP_CANDIDATES]
            for k in range(2, min(MAX_GROUP, len(pool)) + 1):
                for combo in itertools.combinations(pool, k):
                    consider_group(list(combo), [ii])

        group_cands.sort(key=lambda c: (c[0], c[1], c[2]))
        for diff, _gap, _ids, t_idxs, i_idxs, near in group_cands:
            if any(ti not in unmatched_t for ti in t_idxs):
                continue
            if any(ii not in unmatched_i for ii in i_idxs):
                continue
            for ti in t_idxs:
                unmatched_t.discard(ti)
            for ii in i_idxs:
                unmatched_i.discard(ii)
                item = items[ii]
                if item.kind == "payout" and item.payout_state == "due":
                    due_outflow_ms[item.ref] = min(usable[ti].time_ms for ti in t_idxs)
            matched_n += 1
            if near:
                t0 = usable[t_idxs[0]]
                i0 = items[i_idxs[0]]
                if max(t0.time_ms, i0.t_ref_ms) >= report_start_ms:
                    findings.append(
                        Finding(
                            code="near_amount",
                            fingerprint=f"near:{t0.tran_id}:{i0.ref}",
                            time_ms=t0.time_ms,
                            amount=diff,
                            partner_name=i0.partner_name,
                            detail=str(diff),
                            alertable=True,
                        )
                    )

    # Pass 2: exact groups
    run_group_pass(exact_only=True)

    # Pass 3: near 1:1
    near_cands: list[tuple[Decimal, int, str, str, int, int]] = []
    for ti in unmatched_t:
        t = usable[ti]
        for ii in unmatched_i:
            item = items[ii]
            if not _in_window(t, item):
                continue
            diff = _pair_diff(t, item)
            if diff <= _near_tol(item.amount):
                near_cands.append((diff, _time_gap(t, item), t.tran_id, item.ref, ti, ii))
    near_cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    for _d, _g, _tid, _ref, ti, ii in near_cands:
        if ti in unmatched_t and ii in unmatched_i:
            mark_pair(ti, ii, near=True)

    # Pass 4: near groups (exact sums still allowed; nothing matched twice)
    run_group_pass(exact_only=False)

    pending_n = 0

    for ti in sorted(unmatched_t, key=lambda i: (usable[i].time_ms, usable[i].tran_id)):
        t = usable[ti]
        if t.time_ms < report_start_ms:
            continue
        age = now_ms - t.time_ms
        alertable = age >= GRACE_BINANCE_MS
        if not alertable:
            pending_n += 1
        code = "transfer_in_unrecorded" if t.amount > 0 else "transfer_out_unrecorded"
        findings.append(
            Finding(
                code=code,
                fingerprint=f"bx:{t.income_type}:{t.tran_id}",
                time_ms=t.time_ms,
                amount=_abs_amt(t),
                partner_name=None,
                detail="",
                alertable=alertable,
            )
        )

    for ii in sorted(unmatched_i, key=lambda i: (items[i].t_ref_ms, items[i].ref)):
        item = items[ii]
        if item.kind == "deposit":
            if item.t_ref_ms < report_start_ms:
                continue
            age = now_ms - item.t_ref_ms
            alertable = age >= GRACE_DEPOSIT_MS
            if not alertable:
                pending_n += 1
            findings.append(
                Finding(
                    code="deposit_no_transfer",
                    fingerprint=f"dep:{item.ref[3:]}",
                    time_ms=item.t_ref_ms,
                    amount=item.amount,
                    partner_name=item.partner_name,
                    detail="",
                    alertable=alertable,
                )
            )
        elif item.kind == "payout" and item.payout_state == "paid":
            paid_at = item.paid_at_ms if item.paid_at_ms is not None else item.t_ref_ms
            if paid_at < report_start_ms:
                continue
            # Transfers before ledger start are pre_init and never usable, so the window that matters starts at ledger start.
            if coverage_start_ms is not None and max(item.t_lo_ms, ledger_start_ms) < coverage_start_ms:
                continue
            age = now_ms - paid_at
            alertable = age >= GRACE_PAID_MS
            if not alertable:
                pending_n += 1
            findings.append(
                Finding(
                    code="paid_no_outflow",
                    fingerprint=f"paid:{item.ref[3:]}",
                    time_ms=paid_at,
                    amount=item.amount,
                    partner_name=item.partner_name,
                    detail="",
                    alertable=alertable,
                )
            )

    for item in items:
        if item.kind != "payout" or item.payout_state != "due":
            continue
        if item.ref not in due_outflow_ms:
            continue
        outflow_ms = due_outflow_ms[item.ref]
        if outflow_ms < report_start_ms:
            continue
        age = now_ms - outflow_ms
        alertable = age >= GRACE_DUE_OUTFLOW_MS
        if not alertable:
            pending_n += 1
        findings.append(
            Finding(
                code="due_but_paid_out",
                fingerprint=f"due:{item.ref[3:]}",
                time_ms=outflow_ms,
                amount=item.amount,
                partner_name=item.partner_name,
                detail=_fmt_bkk(outflow_ms),
                alertable=alertable,
            )
        )

    binance_in = Decimal("0")
    binance_out = Decimal("0")
    for t in usable:
        if t.time_ms < report_start_ms:
            continue
        if t.amount > 0:
            binance_in += t.amount
        else:
            binance_out += abs(t.amount)

    ledger_in = Decimal("0")
    ledger_out = Decimal("0")
    for item in items:
        if item.t_ref_ms < report_start_ms:
            continue
        if item.direction > 0:
            ledger_in += item.amount
        else:
            ledger_out += item.amount

    findings.sort(key=lambda f: (f.time_ms, f.fingerprint))
    return AuditResult(
        status="ok",
        ledger_start_ms=ledger_start_ms,
        report_start_ms=report_start_ms,
        findings=findings,
        matched_n=matched_n,
        pending_n=pending_n,
        pre_init_ignored_n=pre_init,
        binance_in=binance_in,
        binance_out=binance_out,
        ledger_in=ledger_in,
        ledger_out=ledger_out,
    )


def format_alert_th(new_findings: list[Finding], still_open_n: int) -> str:
    n = len(new_findings)
    lines = [f"⚠️ ตรวจยอดโอน futures กับสมุดพอร์ตร่วม — พบ {n} รายการใหม่"]
    bullets: list[str] = []
    for f in new_findings:
        dt = _fmt_bkk(f.time_ms)
        x = _fmt_amt(f.amount)
        name = f.partner_name or "?"
        if f.code == "transfer_in_unrecorded":
            bullets.append(f"• {dt} เงินเข้า +{x} USDT ไม่มีในสมุด — ยังไม่บันทึก รอจัดประเภท อย่าบันทึกเองด้วยคำสั่งฝาก/ถอน")
        elif f.code == "transfer_out_unrecorded":
            bullets.append(f"• {dt} เงินออก −{x} USDT ไม่มีในสมุด — ยังไม่บันทึก รอจัดประเภท อย่าบันทึกเองด้วยคำสั่งฝาก/ถอน")
        elif f.code == "deposit_no_transfer":
            bullets.append(f"• {name}: บันทึกฝาก {x} แต่ไม่เห็นเงินเข้า futures")
        elif f.code == "paid_no_outflow":
            bullets.append(f"• {name}: กดโอนแล้ว {x} แต่ไม่เห็นเงินออกจาก futures — ยังไม่บันทึก รอจัดประเภท")
        elif f.code == "due_but_paid_out":
            bullets.append(f"• {name}: ค้างจ่าย {x} แต่เงินออกแล้ว {f.detail} — กด “โอนแล้ว”")
        elif f.code == "near_amount":
            bullets.append(f"• {name}: ยอดต่างจาก Binance {_fmt_amt(f.amount)} USDT")
        elif f.code == "non_usdt":
            bullets.append(f"• {dt} โอน {f.detail} เข้า/ออก futures — ระบบนับเฉพาะ USDT")
        elif f.code == "verify_mismatch":
            bullets.append("• สมุดไม่ตรงกับประวัติ (verify_ledger) — รัน jarvise assist partner-audit")
        else:
            bullets.append(f"• {dt} {f.code} {x}")
    if len(bullets) > ALERT_MAX_LINES:
        extra = len(bullets) - ALERT_MAX_LINES
        bullets = bullets[:ALERT_MAX_LINES]
        bullets.append(f"และอีก {extra} รายการ")
    lines.extend(bullets)
    if still_open_n > 0:
        lines.append(f"ยังค้างจากก่อนหน้า {still_open_n} รายการ")
    lines.append("ระบบไม่ได้แก้สมุดให้ — ดูทั้งหมด: jarvise assist partner-audit")
    return "\n".join(lines)


def format_failure_th(reason: str) -> str:
    return f"⚠️ ตรวจยอดโอน futures ไม่ได้มา 2 วัน ({reason}) — ยังไม่รู้ว่ามีโอนที่ไม่ได้บันทึกหรือไม่"


def format_recovery_th() -> str:
    return "✅ ตรวจยอดโอน futures กลับมาทำงานแล้ว"


def _empty_state() -> dict[str, Any]:
    return {
        "v": 1,
        "alerted": {},
        "open_unrecorded": [],
        "last_ok_ms": None,
        "fail_since_ms": None,
        "fail_alerted": False,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("partner_audit state corrupt; treating as empty")
        return _empty_state()
    if not isinstance(data, dict):
        logger.warning("partner_audit state corrupt; treating as empty")
        return _empty_state()
    alerted = data.get("alerted")
    if not isinstance(alerted, dict):
        alerted = {}
    out = _empty_state()
    clean_alerted: dict[str, int] = {}
    for k, v in alerted.items():
        try:
            clean_alerted[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    out["alerted"] = clean_alerted
    raw_open = data.get("open_unrecorded")
    if isinstance(raw_open, list):
        out["open_unrecorded"] = [str(x) for x in raw_open if str(x)]
    # Old state files without open_unrecorded stay as [].
    lok = data.get("last_ok_ms")
    out["last_ok_ms"] = int(lok) if isinstance(lok, (int, float)) else None
    fs = data.get("fail_since_ms")
    out["fail_since_ms"] = int(fs) if isinstance(fs, (int, float)) else None
    out["fail_alerted"] = bool(data.get("fail_alerted"))
    return out


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix="partner_audit_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def remove_open_unrecorded_for_tran(path: Path, tran_id: str) -> None:
    """Best-effort drop booked ``bx:*:{tranId}`` fingerprints from the state file.

    Never raises — booking must not fail when the audit-state write fails.
    """
    tid = str(tran_id or "").strip()
    if not tid:
        return
    try:
        from jarvise_assist.partner_json_store import locked_json_update

        def mutate(state: dict[str, Any]) -> None:
            open_list = list(state.get("open_unrecorded") or [])
            if not open_list:
                return
            kept = [
                fp
                for fp in open_list
                if not (
                    str(fp) == f"bx:TRANSFER:{tid}"
                    or (str(fp).startswith("bx:") and str(fp).endswith(f":{tid}"))
                )
            ]
            state["open_unrecorded"] = kept

        locked_json_update(path, mutate, load=load_state, save=save_state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("partner_audit open_unrecorded cleanup failed type=%s", type(exc).__name__)


def _verify_finding(conn: sqlite3.Connection) -> Finding | None:
    result = verify_ledger(conn)
    if result.get("ok") is True:
        return None
    mismatches = result.get("mismatches") or []
    digest = hashlib.sha256(json.dumps(mismatches, sort_keys=True, default=str).encode("utf-8")).hexdigest()[
        :16
    ]
    return Finding(
        code="verify_mismatch",
        fingerprint=f"verify:{digest}",
        time_ms=0,
        amount=Decimal("0"),
        partner_name=None,
        detail="",
        alertable=True,
    )


def _fetch_from_ms(
    items: list[LedgerCash],
    *,
    pad_from: int,
    report_start: int,
    ledger_start: int,
) -> int:
    """Extend fetch back to paid payout match windows still in the report."""
    paid_los = [
        item.t_lo_ms
        for item in items
        if item.kind == "payout"
        and item.payout_state == "paid"
        and item.paid_at_ms is not None
        and item.paid_at_ms >= report_start
    ]
    fetch_from = int(pad_from)
    if paid_los:
        fetch_from = min(fetch_from, min(paid_los))
    return max(int(ledger_start), fetch_from)


def run_partner_audit_job(
    conn: sqlite3.Connection,
    *,
    client: Any,
    now_ms: int,
    send: SendFn,
    state_path: Path,
) -> dict[str, Any]:
    """Daily job. Returns counts only — never names, amounts, or ids."""
    start = ledger_start_ms(conn)
    if start is None:
        return {
            "ok": True,
            "status": "not_init",
            "new": 0,
            "open": 0,
            "pending": 0,
            "matched": 0,
            "alert_sent": False,
            "error": None,
        }

    report_start = max(start, int(now_ms) - JOB_DAYS * DAY_MS)
    pad_from = max(start, report_start - PAD_MS)
    alert_sent = False

    items = ledger_cash(conn, pad_from, int(now_ms))
    fetch_from = _fetch_from_ms(items, pad_from=pad_from, report_start=report_start, ledger_start=start)
    fetched = fetch_transfers(client, fetch_from, int(now_ms), now_ms=int(now_ms))
    if fetched.status != "ok":
        from jarvise_assist.partner_json_store import locked_json_update

        reason = fetched.status if fetched.error is None else f"{fetched.status}/{fetched.error}"

        def mutate_fail_claim(state: dict[str, Any]) -> bool:
            if state.get("fail_since_ms") is None:
                state["fail_since_ms"] = int(now_ms)
            anchor = state.get("last_ok_ms")
            if anchor is None:
                anchor = state.get("fail_since_ms")
            return bool(
                anchor is not None
                and int(now_ms) - int(anchor) >= FAIL_ALERT_AFTER_MS
                and not state.get("fail_alerted")
            )

        should_fail_alert = bool(
            locked_json_update(state_path, mutate_fail_claim, load=load_state, save=save_state)
        )
        if should_fail_alert:
            mid = send(format_failure_th(reason))
            if mid is not None:

                def mutate_fail_finalize(state: dict[str, Any]) -> None:
                    state["fail_alerted"] = True

                locked_json_update(state_path, mutate_fail_finalize, load=load_state, save=save_state)
                alert_sent = True
        return {
            "ok": False,
            "status": fetched.status,
            "new": 0,
            "open": 0,
            "pending": 0,
            "matched": 0,
            "alert_sent": alert_sent,
            "error": fetched.error,
        }

    coverage_start = max(fetch_from, int(now_ms) - BINANCE_HISTORY_MS)
    result = reconcile(
        fetched.transfers,
        items,
        now_ms=int(now_ms),
        ledger_start_ms=start,
        report_start_ms=report_start,
        coverage_start_ms=coverage_start,
    )
    findings = list(result.findings)
    vf = _verify_finding(conn)
    if vf is not None:
        findings.append(vf)

    alertable = [f for f in findings if f.alertable]
    from jarvise_assist.partner_json_store import locked_json_update

    def mutate_ok_claim(state: dict[str, Any]) -> tuple[list[Finding], int, bool, int]:
        alerted_map: dict[str, int] = dict(state.get("alerted") or {})
        new = [f for f in alertable if f.fingerprint not in alerted_map]
        still_open_n = len([f for f in alertable if f.fingerprint in alerted_map])
        need_recovery = bool(state.get("fail_alerted"))

        # Non-send state always advances under the lock (same as pre-split semantics).
        if need_recovery:
            state["fail_alerted"] = False
        state["last_ok_ms"] = int(now_ms)
        state["fail_since_ms"] = None
        state["alerted"] = {fp: ms for fp, ms in alerted_map.items() if int(now_ms) - int(ms) <= PRUNE_MS}
        state["open_unrecorded"] = sorted(
            {
                str(f.fingerprint)
                for f in findings
                if f.code in ("transfer_in_unrecorded", "transfer_out_unrecorded")
            }
        )
        return new, still_open_n, need_recovery, len(new)

    new_findings, still_open_n, need_recovery, new_n = locked_json_update(
        state_path, mutate_ok_claim, load=load_state, save=save_state
    )

    if new_findings:
        mid = send(format_alert_th(new_findings, still_open_n))
        if mid is not None:
            fps = [str(f.fingerprint) for f in new_findings]

            def mutate_alert_finalize(state: dict[str, Any]) -> None:
                alerted_map: dict[str, int] = dict(state.get("alerted") or {})
                for fp in fps:
                    alerted_map[fp] = int(now_ms)
                state["alerted"] = {
                    fp: ms for fp, ms in alerted_map.items() if int(now_ms) - int(ms) <= PRUNE_MS
                }

            locked_json_update(state_path, mutate_alert_finalize, load=load_state, save=save_state)
            alert_sent = True

    if need_recovery:
        mid = send(format_recovery_th())
        if mid is not None:
            alert_sent = True

    return {
        "ok": True,
        "status": "ok",
        "new": new_n,
        "open": len(alertable),
        "pending": result.pending_n,
        "matched": result.matched_n,
        "alert_sent": alert_sent,
        "error": None,
    }


def audit_report(
    conn: sqlite3.Connection,
    client: Any | None,
    *,
    now_ms: int,
    days: int,
) -> AuditResult:
    """CLI reconciliation. No state file and no Telegram send."""
    start = ledger_start_ms(conn)
    if start is None:
        return AuditResult(status="not_init", ledger_start_ms=None, report_start_ms=None)
    use_days = max(1, min(int(days), MAX_DAYS))
    report_start = max(start, int(now_ms) - use_days * DAY_MS)
    pad_from = max(start, report_start - PAD_MS)
    if client is None:
        return AuditResult(
            status="no_client",
            ledger_start_ms=start,
            report_start_ms=report_start,
        )
    items = ledger_cash(conn, pad_from, int(now_ms))
    fetch_from = _fetch_from_ms(items, pad_from=pad_from, report_start=report_start, ledger_start=start)
    fetched = fetch_transfers(client, fetch_from, int(now_ms), now_ms=int(now_ms))
    if fetched.status != "ok":
        return AuditResult(
            status=fetched.status,
            ledger_start_ms=start,
            report_start_ms=report_start,
            error=fetched.error,
        )
    coverage_start = max(fetch_from, int(now_ms) - BINANCE_HISTORY_MS)
    result = reconcile(
        fetched.transfers,
        items,
        now_ms=int(now_ms),
        ledger_start_ms=start,
        report_start_ms=report_start,
        coverage_start_ms=coverage_start,
    )
    findings = list(result.findings)
    vf = _verify_finding(conn)
    if vf is not None:
        findings.append(vf)
        findings.sort(key=lambda f: (f.time_ms, f.fingerprint))
    return AuditResult(
        status=result.status,
        ledger_start_ms=result.ledger_start_ms,
        report_start_ms=result.report_start_ms,
        findings=findings,
        matched_n=result.matched_n,
        pending_n=result.pending_n,
        pre_init_ignored_n=result.pre_init_ignored_n,
        binance_in=result.binance_in,
        binance_out=result.binance_out,
        ledger_in=result.ledger_in,
        ledger_out=result.ledger_out,
        error=result.error,
    )


def guard_check(
    conn: sqlite3.Connection,
    client: Any | None,
    *,
    now_ms: int,
    deposit_request_id: str | None = None,
    deposit_amount: Decimal | None = None,
    deposit_partner: str | None = None,
) -> GuardResult:
    """Confirm-time transfer check. SELECT-only; never touches the state file."""
    now = int(now_ms)
    start = ledger_start_ms(conn)
    if start is None:
        return GuardResult(status="not_init", checked_at_ms=now, findings=[], warn=False, error=None)
    report_start = max(start, now - JOB_DAYS * DAY_MS)
    pad_from = max(start, report_start - PAD_MS)
    if client is None:
        return GuardResult(status="no_client", checked_at_ms=now, findings=[], warn=True, error=None)

    items = list(ledger_cash(conn, pad_from, now))
    if deposit_request_id is not None:
        rid = str(deposit_request_id)
        items.append(
            LedgerCash(
                ref=f"rq:{rid}",
                kind="deposit",
                direction=1,
                partner_name=deposit_partner or "?",
                amount=deposit_amount if deposit_amount is not None else Decimal("0"),
                t_ref_ms=now,
                t_lo_ms=now - DEP_BEFORE_MS,
                t_hi_ms=now + DEP_AFTER_MS,
                payout_state=None,
                paid_at_ms=None,
            )
        )

    fetch_from = _fetch_from_ms(items, pad_from=pad_from, report_start=report_start, ledger_start=start)
    fetched = fetch_transfers(client, fetch_from, now, now_ms=now)
    if fetched.status != "ok":
        return GuardResult(
            status=fetched.status,
            checked_at_ms=now,
            findings=[],
            warn=True,
            error=fetched.error,
        )

    coverage_start = max(fetch_from, now - BINANCE_HISTORY_MS)
    result = reconcile(
        fetched.transfers,
        items,
        now_ms=now,
        ledger_start_ms=start,
        report_start_ms=report_start,
        coverage_start_ms=coverage_start,
    )
    findings = list(result.findings)
    vf = _verify_finding(conn)
    if vf is not None:
        findings.append(vf)

    if deposit_request_id is not None:
        rid = str(deposit_request_id)
        remapped: list[Finding] = []
        for f in findings:
            if f.fingerprint == f"dep:{rid}":
                remapped.append(replace(f, code="this_deposit_not_seen"))
            elif f.code == "near_amount" and f.fingerprint.endswith(f":rq:{rid}"):
                remapped.append(replace(f, code="this_deposit_amount_diff"))
            else:
                remapped.append(f)
        findings = remapped

    findings.sort(key=lambda f: (f.time_ms, f.fingerprint))
    status = result.status
    warn = status != "ok" or any(f.code in GUARD_WARN_CODES for f in findings)
    return GuardResult(
        status=status,
        checked_at_ms=now,
        findings=findings,
        warn=warn,
        error=result.error,
    )


def format_guard_th(result: GuardResult, *, last_ok_ms: int | None) -> str:
    """Thai admin card block for the confirm-time transfer guard."""
    if result.status != "ok":
        reason = _GUARD_FETCH_TH.get(result.status, result.status)
        if result.error:
            reason = f"{reason}/{result.error}"
        lines = [
            f"⚠️ ตรวจยอดโอนไม่ได้ตอนนี้ ({reason}) — ยังไม่รู้ว่ามีโอนที่ไม่ได้บันทึกหรือไม่",
        ]
        if last_ok_ms is not None:
            lines.append(f"ตรวจสำเร็จล่าสุด (รอบเช้า) {_fmt_bkk(int(last_ok_ms))}")
        else:
            lines.append("ยังไม่เคยตรวจสำเร็จ")
        return "\n".join(lines)

    findings = list(result.findings)
    n = len(findings)
    lines = [f"⚠️ ตรวจยอดโอนก่อนยืนยัน — พบ {n} เรื่อง"]
    warn_bullets: list[str] = []
    info_bullets: list[str] = []
    for f in findings:
        dt = _fmt_bkk(f.time_ms)
        x = _fmt_amt(f.amount)
        name = f.partner_name or "?"
        if f.code == "transfer_in_unrecorded":
            warn_bullets.append(f"• {dt} เงินเข้า +{x} USDT ยังไม่อยู่ในสมุด")
        elif f.code == "transfer_out_unrecorded":
            warn_bullets.append(f"• {dt} เงินออก −{x} USDT ยังไม่อยู่ในสมุด")
        elif f.code == "this_deposit_not_seen":
            warn_bullets.append(f"• ยังไม่เห็นเงินฝาก {x} USDT ของรายการนี้เข้า futures wallet")
        elif f.code == "this_deposit_amount_diff":
            warn_bullets.append(
                f"• ยอดเข้าจริงต่างจากที่จะบันทึก {x} USDT — ถ้าไม่ตรง ให้ยกเลิกแล้วใช้ p dep {name} <ยอดจริง>"
            )
        elif f.code == "deposit_no_transfer":
            warn_bullets.append(f"• {name}: บันทึกฝาก {x} แต่ไม่เห็นเงินเข้า futures")
        elif f.code == "paid_no_outflow":
            warn_bullets.append(f"• {name}: กดโอนแล้ว {x} แต่ไม่เห็นเงินออกจาก futures")
        elif f.code == "due_but_paid_out":
            warn_bullets.append(f"• {name}: ค้างจ่าย {x} แต่เงินออกแล้ว {f.detail}")
        elif f.code == "verify_mismatch":
            warn_bullets.append("• สมุดไม่ตรงกับประวัติ (verify_ledger)")
        elif f.code == "near_amount":
            info_bullets.append(f"• ℹ️ {name}: ยอดต่างจาก Binance {x} USDT")
        elif f.code == "non_usdt":
            asset = f.detail or "?"
            info_bullets.append(f"• ℹ️ {dt} โอน {asset} — ระบบนับเฉพาะ USDT")
        else:
            warn_bullets.append(f"• {dt} {f.code} {x}")

    bullets = warn_bullets + info_bullets
    if len(bullets) > GUARD_MAX_LINES:
        extra = len(bullets) - GUARD_MAX_LINES
        bullets = bullets[:GUARD_MAX_LINES]
        bullets.append(f"และอีก {extra} รายการ")
    lines.extend(bullets)
    lines.append(
        "ยืนยันตอนนี้ = ล็อกตัวเลขด้วยยอดพอร์ตที่รวมเงินก้อนนี้ · เงินที่ยังไม่อยู่ในสมุดรอจัดประเภท อย่าบันทึกเองด้วยคำสั่งฝาก/ถอน · ดูทั้งหมด: jarvise assist partner-audit"
    )
    return "\n".join(lines)


def export_events_csv(conn: sqlite3.Connection, path: Path) -> None:
    """Write one CSV row per partner_event. Fails if path already exists."""
    headers = [
        "event_id",
        "created_at_utc",
        "created_at_bkk",
        "partner_name",
        "is_admin",
        "kind",
        "units_delta",
        "units_after",
        "cost_delta",
        "cost_after",
        "cash",
        "fee",
        "nav",
        "account_value",
        "request_id",
        "payout_state",
        "payout",
        "paid_at_utc",
        "actor",
        "note",
    ]
    rows = conn.execute(
        """
        SELECT e.id, e.created_at_ms, e.kind, e.units_delta_e8, e.units_after_e8,
               e.cost_delta_micro, e.cost_after_micro, e.cash_micro, e.fee_micro,
               e.nav, e.account_value_micro, e.request_id, e.actor, e.note,
               p.name AS partner_name, p.is_admin,
               r.payout_state, r.payout_micro, r.paid_at_ms
        FROM partner_event e
        JOIN partner p ON p.id = e.partner_id
        LEFT JOIN partner_request r ON r.id = e.request_id
        ORDER BY e.created_at_ms ASC, e.rowid ASC
        """
    ).fetchall()
    with open(path, "x", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            created = int(row["created_at_ms"])
            payout_micro = row["payout_micro"]
            paid_at = row["paid_at_ms"]
            note = str(row["note"] or "")
            name = str(row["partner_name"] or "")
            writer.writerow(
                [
                    str(row["id"]),
                    _fmt_utc(created),
                    datetime.fromtimestamp(created / 1000.0, tz=BANGKOK).strftime("%Y-%m-%d %H:%M:%S"),
                    _csv_text(name),
                    int(row["is_admin"]),
                    str(row["kind"]),
                    str(units_from_e8(int(row["units_delta_e8"]))),
                    str(units_from_e8(int(row["units_after_e8"]))),
                    str(usdt_from_micro(int(row["cost_delta_micro"]))),
                    str(usdt_from_micro(int(row["cost_after_micro"]))),
                    str(usdt_from_micro(int(row["cash_micro"]))),
                    str(usdt_from_micro(int(row["fee_micro"]))),
                    str(row["nav"]),
                    str(usdt_from_micro(int(row["account_value_micro"]))),
                    str(row["request_id"] or ""),
                    str(row["payout_state"] or ""),
                    str(usdt_from_micro(int(payout_micro))) if payout_micro is not None else "",
                    _fmt_utc(int(paid_at) if paid_at is not None else None),
                    str(row["actor"] or ""),
                    _csv_text(note),
                ]
            )
