"""jarvise paper CLI — simulated fills only. No exchange orders."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from jarvise_analyze.engine import CONFIDENCE_THRESHOLD, analyze_snapshot
from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_account,
    list_approvals,
    list_paper_orders,
    list_paper_positions,
    load_latest_analysis,
    load_latest_candle,
    open_db,
    universe_as_of,
    upsert_analysis_output,
)
from jarvise_ingest.timeframes import ALLOWED_INTERVALS
from jarvise_ingest.universe import PAPER_CORE, seed_paper_core
from jarvise_paper.approval import (
    approve_approval,
    enqueue_approval,
    expire_approvals,
    reject_approval,
)
from jarvise_paper.engine import apply_signal

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "analytics" / "jarvise.db"
EXAMPLE = "jarvise paper run --symbol BTCUSDT --timeframe 4h --json"


def _parse_symbols(raw: list[str] | None) -> list[str]:
    symbols: list[str] = []
    for item in raw or []:
        for part in item.split(","):
            part = part.strip().upper()
            if part:
                symbols.append(part)
    return symbols


def kill_switch_engaged() -> bool:
    url = os.environ.get("REDIS_URL")
    if not url:
        return False
    try:
        import redis

        val = redis.Redis.from_url(url, decode_responses=True).get("jarvise:kill_switch")
        return val in {"1", "true", "on", "yes"}
    except Exception:
        return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jarvise paper",
        description=(
            "Paper auto-trade: simulated fills from analyze + closed candles. "
            "No exchange order placement."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Examples:\n  {EXAMPLE}\n"
            "  jarvise paper run --universe paper_core --timeframe 4h --json\n"
            "  jarvise paper status --json"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser(
        "run",
        help="Analyze (unless skipped); default enqueues for approve",
    )
    run_p.add_argument("--symbol", action="append", dest="symbols")
    run_p.add_argument("--universe", help=f"Universe id (seeded: {PAPER_CORE})")
    run_p.add_argument(
        "--timeframe",
        default="4h",
        choices=sorted(ALLOWED_INTERVALS),
    )
    run_p.add_argument(
        "--confidence-threshold",
        type=float,
        default=CONFIDENCE_THRESHOLD,
    )
    run_p.add_argument(
        "--skip-analyze",
        action="store_true",
        help="Use existing analysis_output only",
    )
    run_p.add_argument("--db", type=Path, default=DEFAULT_DB)
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument(
        "--auto-fill",
        action="store_true",
        help="Apply paper fills immediately (skip approval queue)",
    )
    run_p.add_argument("--json", action="store_true", dest="as_json")

    q = sub.add_parser("queue", help="List approval queue")
    q.add_argument("--db", type=Path, default=DEFAULT_DB)
    q.add_argument("--all", action="store_true", help="Include recent non-pending")
    q.add_argument("--json", action="store_true", dest="as_json")
    q.add_argument("--limit", type=int, default=50)

    ap = sub.add_parser("approve", help="Approve pending → paper fill")
    ap.add_argument("approval_id")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true", dest="as_json")

    rj = sub.add_parser("reject", help="Reject pending (no fill)")
    rj.add_argument("approval_id")
    rj.add_argument("--reason", default=None)
    rj.add_argument("--db", type=Path, default=DEFAULT_DB)
    rj.add_argument("--json", action="store_true", dest="as_json")

    ex = sub.add_parser("expire", help="Mark timed-out pendings (no FLAT)")
    ex.add_argument("--db", type=Path, default=DEFAULT_DB)
    ex.add_argument("--json", action="store_true", dest="as_json")

    st = sub.add_parser("status", help="Show paper account, positions, recent fills")
    st.add_argument("--db", type=Path, default=DEFAULT_DB)
    st.add_argument("--json", action="store_true", dest="as_json")
    st.add_argument("--limit", type=int, default=20)
    return p


def _analysis_summary(analysis: dict) -> dict:
    return {
        "analysis_id": analysis.get("analysis_id"),
        "action": analysis.get("action"),
        "regime_state": analysis.get("regime_state"),
        "confidence_score": analysis.get("confidence_score"),
        "size_pct_equity": analysis.get("size_pct_equity"),
    }


def _resolve_symbols(conn, args) -> list[str]:
    symbols = _parse_symbols(getattr(args, "symbols", None))
    universe = getattr(args, "universe", None)
    if universe:
        if universe == PAPER_CORE:
            seed_paper_core(conn)
        as_of_ms = int(time.time() * 1000)
        from_universe = universe_as_of(conn, universe, as_of_ms)
        seen: set[str] = set()
        merged: list[str] = []
        for sym in symbols + from_universe:
            if sym not in seen:
                seen.add(sym)
                merged.append(sym)
        symbols = merged
    return symbols


def cmd_status(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        ensure_paper_account(conn)
        account = get_paper_account(conn)
        positions = list_paper_positions(conn)
        orders = list_paper_orders(conn, limit=args.limit)
        pending_count = len(list_approvals(conn, status="pending"))
    finally:
        conn.close()
    payload = {
        "ok": True,
        "paper_only": True,
        "db": str(args.db.resolve()),
        "account": account,
        "positions": positions,
        "orders": orders,
        "pending_count": pending_count,
        "note": "paper ledger only; no exchange orders",
    }
    if args.as_json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print(
            f"equity={account['equity']:.2f} cash={account['cash']:.2f} "
            f"positions={len(positions)} orders={len(orders)}"
        )
        for p in positions:
            print(
                f"  {p['symbol']} {p['side']} qty={p['qty']:.6g} "
                f"entry={p['entry_price']:.6g} uPnL={p['unrealized_pnl']}"
            )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if kill_switch_engaged():
        payload = {
            "ok": False,
            "skipped": True,
            "reason": "kill_switch engaged",
            "paper_only": True,
        }
        if args.as_json:
            print(json.dumps(payload, separators=(",", ":")))
        else:
            print("Error: kill_switch engaged", file=sys.stderr)
        return 3

    if not args.symbols and not args.universe:
        print(f"Error: --symbol or --universe is required.\n  {EXAMPLE}", file=sys.stderr)
        return 2

    started = time.perf_counter()
    conn = open_db(args.db)
    results: list[dict] = []
    errors: list[str] = []
    try:
        ensure_paper_account(conn)
        symbols = _resolve_symbols(conn, args)
        if not symbols:
            print(f"Error: no symbols.\n  {EXAMPLE}", file=sys.stderr)
            return 2

        for sym in symbols:
            candle = load_latest_candle(conn, sym, args.timeframe)
            if candle is None:
                errors.append(f"{sym} {args.timeframe}: no stored candles")
                continue
            mid = float(candle["close"])

            if args.skip_analyze:
                analysis = load_latest_analysis(conn, sym, args.timeframe)
                if analysis is None:
                    errors.append(f"{sym} {args.timeframe}: no analysis_output")
                    continue
            else:
                analysis = analyze_snapshot(
                    candle, confidence_threshold=args.confidence_threshold
                )
                if not args.dry_run:
                    upsert_analysis_output(conn, analysis)

            summary = _analysis_summary(analysis)
            if args.auto_fill:
                applied = apply_signal(
                    conn,
                    analysis=analysis,
                    mid_price=mid,
                    timeframe=args.timeframe,
                    dry_run=args.dry_run,
                )
                applied["analysis"] = summary
                results.append(applied)
                if not args.as_json:
                    print(
                        f"{sym}: {analysis.get('action')} fills={len(applied['fills'])} "
                        f"equity={applied['equity']:.2f}"
                    )
            else:
                if args.dry_run:
                    queued = {
                        "dry_run": True,
                        "would_enqueue": True,
                        "analysis": summary,
                    }
                else:
                    queued = enqueue_approval(
                        conn, analysis=analysis, timeframe=args.timeframe
                    )
                results.append({"queued": queued, "analysis": summary})
                if not args.as_json:
                    print(f"{sym}: {analysis.get('action')} queued for approval")
    finally:
        conn.close()

    ok = bool(results) and not errors
    payload = {
        "ok": ok,
        "dry_run": args.dry_run,
        "paper_only": True,
        "db": str(args.db.resolve()),
        "timeframe": args.timeframe,
        "results": results,
        "errors": errors,
        "duration_s": round(time.perf_counter() - started, 3),
        "note": (
            "paper fills only; no exchange order placement"
            if args.auto_fill
            else "enqueued for approval; use jarvise paper approve"
        ),
    }
    if args.as_json:
        print(json.dumps(payload, separators=(",", ":")))
    elif errors and not results:
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
    return 0 if ok else 1


def cmd_queue(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        status = None if args.all else "pending"
        rows = list_approvals(conn, status=status, limit=args.limit)
    finally:
        conn.close()
    payload = {
        "ok": True,
        "paper_only": True,
        "db": str(args.db.resolve()),
        "approvals": rows,
        "count": len(rows),
    }
    if args.as_json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        for row in rows:
            print(
                f"{row['id']} {row['symbol']} {row['action']} "
                f"status={row['status']} expires={row['expires_at_ms']}"
            )
    return 0


def _approve_exit_code(result: dict) -> int:
    if result.get("ok"):
        return 0
    err = result.get("error") or ""
    if err == "kill_switch engaged":
        return 3
    if err == "approval not pending":
        return 2
    return 1


def cmd_approve(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        ensure_paper_account(conn)
        result = approve_approval(
            conn,
            args.approval_id,
            kill_switch=kill_switch_engaged(),
        )
    finally:
        conn.close()
    code = _approve_exit_code(result)
    payload = {"ok": result.get("ok"), "paper_only": True, **result}
    if args.as_json:
        print(json.dumps(payload, separators=(",", ":")))
    elif not result.get("ok"):
        print(f"Error: {result.get('error')}", file=sys.stderr)
    return code


def cmd_reject(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        result = reject_approval(
            conn, args.approval_id, reason=args.reason
        )
    finally:
        conn.close()
    code = 0 if result.get("ok") else 2
    payload = {"ok": result.get("ok"), "paper_only": True, **result}
    if args.as_json:
        print(json.dumps(payload, separators=(",", ":")))
    elif not result.get("ok"):
        print(f"Error: {result.get('error')}", file=sys.stderr)
    return code


def cmd_expire(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        result = expire_approvals(conn)
    finally:
        conn.close()
    payload = {"ok": True, "paper_only": True, **result}
    if args.as_json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print(f"expired={result.get('expired', 0)}")
    return 0


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "queue":
        return cmd_queue(args)
    if args.cmd == "approve":
        return cmd_approve(args)
    if args.cmd == "reject":
        return cmd_reject(args)
    if args.cmd == "expire":
        return cmd_expire(args)
    return 2


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
