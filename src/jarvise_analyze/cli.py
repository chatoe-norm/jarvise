"""jarvise analyze CLI — paper regime call. No order placement."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from jarvise_analyze.engine import CONFIDENCE_THRESHOLD, analyze_snapshot
from jarvise_ingest.db import (
    load_latest_candle,
    open_db,
    universe_as_of,
    upsert_analysis_output,
)
from jarvise_ingest.timeframes import ALLOWED_INTERVALS
from jarvise_ingest.universe import PAPER_CORE, seed_paper_core

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "analytics" / "jarvise.db"
EXAMPLE = (
    "jarvise analyze --symbol BTCUSDT --timeframe 4h --json"
)


def _parse_symbols(raw: list[str] | None) -> list[str]:
    symbols: list[str] = []
    for item in raw or []:
        for part in item.split(","):
            part = part.strip().upper()
            if part:
                symbols.append(part)
    return symbols


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jarvise analyze",
        description=(
            "Paper analyzer: regime / confidence / invalidation / size from "
            "stored candles. GET-only DB read + write analysis_output. No orders."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Examples:\n  {EXAMPLE}\n"
            "  jarvise analyze --universe paper_core --timeframe 4h --dry-run --json"
        ),
    )
    p.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Trading pair (repeatable or comma-separated)",
    )
    p.add_argument(
        "--universe",
        help=f"Analyze eligible symbols from a universe (seeded: {PAPER_CORE})",
    )
    p.add_argument(
        "--timeframe",
        default="4h",
        choices=sorted(ALLOWED_INTERVALS),
        help="Candle timeframe (default: 4h)",
    )
    p.add_argument(
        "--confidence-threshold",
        type=float,
        default=CONFIDENCE_THRESHOLD,
        help=f"FLAT below this confidence (default: {CONFIDENCE_THRESHOLD})",
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite path")
    p.add_argument("--dry-run", action="store_true", help="Compute; do not write")
    p.add_argument("--json", action="store_true", dest="as_json", help="JSON summary")
    return p


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.symbols and not args.universe:
        print(
            f"Error: --symbol or --universe is required.\n  {EXAMPLE}",
            file=sys.stderr,
        )
        return 2
    if not 0.0 < args.confidence_threshold <= 1.0:
        print(
            "Error: --confidence-threshold must be in (0, 1].\n  " + EXAMPLE,
            file=sys.stderr,
        )
        return 2

    started = time.perf_counter()
    conn = open_db(args.db)
    try:
        symbols = _parse_symbols(args.symbols)
        if args.universe:
            if args.universe == PAPER_CORE:
                seed_paper_core(conn)
            as_of_ms = int(time.time() * 1000)
            from_universe = universe_as_of(conn, args.universe, as_of_ms)
            seen: set[str] = set()
            merged: list[str] = []
            for sym in symbols + from_universe:
                if sym not in seen:
                    seen.add(sym)
                    merged.append(sym)
            symbols = merged
        if not symbols:
            print(
                f"Error: no symbols to analyze.\n  {EXAMPLE}",
                file=sys.stderr,
            )
            return 2

        analyses: list[dict] = []
        errors: list[str] = []
        for sym in symbols:
            candle = load_latest_candle(conn, sym, args.timeframe)
            if candle is None:
                errors.append(f"{sym} {args.timeframe}: no stored candles")
                continue
            result = analyze_snapshot(
                candle, confidence_threshold=args.confidence_threshold
            )
            if not args.dry_run:
                upsert_analysis_output(conn, result)
            analyses.append(result)
            if not args.as_json:
                print(
                    f"{result['symbol']} {args.timeframe}: "
                    f"{result['regime_state']} → {result['action']} "
                    f"conf={result['confidence_score']:.2f} "
                    f"size={result['size_pct_equity']}% "
                    f"inv={result['invalidation_price']}"
                )
    finally:
        conn.close()

    ok = bool(analyses) and not errors
    payload = {
        "ok": ok,
        "dry_run": args.dry_run,
        "db": str(args.db.resolve()),
        "universe": args.universe,
        "timeframe": args.timeframe,
        "analyses": analyses,
        "errors": errors,
        "duration_s": round(time.perf_counter() - started, 3),
        "note": "paper analyze only; no order placement",
    }
    if args.as_json:
        print(json.dumps(payload, separators=(",", ":")))
    elif not analyses and errors:
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
