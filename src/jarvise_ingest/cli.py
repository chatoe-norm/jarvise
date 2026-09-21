"""jarvise ingest CLI — paper analytics only. No order placement."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from jarvise_ingest.db import open_db, upsert_derivatives, upsert_market_technicals
from jarvise_ingest.indicators import enrich_candles
from jarvise_ingest.providers.binance_klines import ALLOWED_INTERVALS, fetch_klines
from jarvise_ingest.providers.coinglass import fetch_derivatives, resolve_api_key

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "analytics" / "jarvise.db"
EXAMPLE = "jarvise ingest --symbol BTCUSDT --timeframe 1h --skip-derivatives --json"


def _parse_symbols(raw: list[str]) -> list[str]:
    symbols: list[str] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip().upper()
            if part:
                symbols.append(part)
    return symbols


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jarvise ingest",
        description="Paper market ingest into SQLite. GET-only. No order placement.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Examples:\n  {EXAMPLE}\n"
        "  jarvise ingest --symbol BTCUSDT,ETHUSDT --dry-run --json\n"
        "  jarvise ingest --symbol BTCUSDT --timeframe 1h --limit 200",
    )
    p.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Trading pair, e.g. BTCUSDT (repeatable or comma-separated)",
    )
    p.add_argument(
        "--timeframe",
        default="1h",
        choices=sorted(ALLOWED_INTERVALS),
        help="Candle timeframe (default: 1h)",
    )
    p.add_argument("--limit", type=int, default=200, help="Candles to fetch (1..1000)")
    p.add_argument(
        "--skip-derivatives",
        action="store_true",
        help="Skip CoinGlass derivatives ingest",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="SQLite path (default: data/analytics/jarvise.db)",
    )
    p.add_argument("--dry-run", action="store_true", help="Print plan; write nothing")
    p.add_argument("--json", action="store_true", dest="as_json", help="JSON summary")
    p.add_argument(
        "--yes",
        action="store_true",
        help="Reserved; no interactive prompts",
    )
    return p


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.symbols:
        print(f"Error: --symbol is required.\n  {EXAMPLE}", file=sys.stderr)
        return 2
    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print(f"Error: --symbol is required.\n  {EXAMPLE}", file=sys.stderr)
        return 2
    if args.limit < 1 or args.limit > 1000:
        print("Error: --limit must be 1..1000\n  " + EXAMPLE, file=sys.stderr)
        return 2

    if not args.skip_derivatives and not resolve_api_key():
        print(
            "Error: COINGLASS_API_KEY not set.\n"
            "  export COINGLASS_API_KEY=... or jarvise ingest --symbol BTCUSDT --skip-derivatives",
            file=sys.stderr,
        )
        return 2

    started = time.perf_counter()
    market_summary: dict = {}
    deriv_summary: dict = {}
    skipped: list[str] = []
    errors: list[str] = []

    if args.dry_run:
        for sym in symbols:
            market_summary[sym] = {
                "timeframe": args.timeframe,
                "planned": args.limit,
            }
            if args.skip_derivatives:
                skipped.append(f"derivatives:{sym}")
            else:
                deriv_summary[sym.split("USDT")[0] if "USDT" in sym else sym] = {
                    "planned": min(args.limit, 30)
                }
        payload = {
            "ok": True,
            "dry_run": True,
            "db": str(args.db),
            "market_technicals": market_summary,
            "derivatives_analytics": deriv_summary,
            "skipped": skipped,
            "errors": errors,
            "duration_s": round(time.perf_counter() - started, 3),
            "note": "paper ingest only; no order placement",
        }
        _emit(payload, args.as_json, dry_run=True)
        return 0

    conn = open_db(args.db)
    try:
        for sym in symbols:
            try:
                candles = fetch_klines(sym, args.timeframe, args.limit)
                enrich_candles(candles)
                n = upsert_market_technicals(conn, candles)
                market_summary[sym] = {"timeframe": args.timeframe, "upserted": n}
                if not args.as_json:
                    print(
                        f"ingested market_technicals: {sym} {args.timeframe} rows={n}"
                    )
            except Exception as exc:  # noqa: BLE001 — surface provider errors
                errors.append(str(exc))
                print(f"Error: {exc}", file=sys.stderr)

            if args.skip_derivatives:
                skipped.append(f"derivatives:{sym}")
                continue
            try:
                rows = fetch_derivatives(sym, args.timeframe, limit=min(30, args.limit))
                n = upsert_derivatives(conn, rows)
                coin = rows[0]["symbol"] if rows else sym
                deriv_summary[coin] = {"upserted": n}
                if not args.as_json:
                    print(f"ingested derivatives_analytics: {coin} rows={n}")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                print(f"Error: {exc}", file=sys.stderr)
    finally:
        conn.close()

    duration = round(time.perf_counter() - started, 3)
    ok = len(errors) == 0 and bool(market_summary)
    payload = {
        "ok": ok,
        "dry_run": False,
        "db": str(args.db.resolve()),
        "market_technicals": market_summary,
        "derivatives_analytics": deriv_summary,
        "skipped": skipped,
        "errors": errors,
        "duration_s": duration,
        "note": "paper ingest only; no order placement",
    }
    _emit(payload, args.as_json, dry_run=False)
    if not args.as_json:
        print(f"db: {args.db.resolve()}")
        print(f"duration: {duration}s")
    return 0 if ok else 1


def _emit(payload: dict, as_json: bool, *, dry_run: bool) -> None:
    if as_json:
        print(json.dumps(payload, separators=(",", ":")))
        return
    if dry_run:
        print("dry-run: would ingest")
        for sym, info in payload["market_technicals"].items():
            print(
                f"  market_technicals {sym} {info['timeframe']} planned={info['planned']}"
            )
        for coin, info in payload["derivatives_analytics"].items():
            print(f"  derivatives_analytics {coin} planned={info['planned']}")
        print(f"db: {payload['db']}")
        print("paper ingest only; no order placement")


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
