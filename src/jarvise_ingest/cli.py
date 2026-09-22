"""jarvise ingest CLI — paper analytics only. No order placement."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from jarvise_ingest.db import (
    append_derivatives,
    open_db,
    universe_as_of,
    upsert_market_technicals,
)
from jarvise_ingest.providers.binance_klines import (
    MAX_PAGE_LIMIT,
    fetch_klines,
    fetch_klines_range,
)
from jarvise_ingest.providers.coinglass import fetch_derivatives, resolve_api_key
from jarvise_ingest.series import find_gaps, recompute_indicators
from jarvise_ingest.timeframes import ALLOWED_INTERVALS
from jarvise_ingest.universe import PAPER_CORE, seed_paper_core

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "analytics" / "jarvise.db"
DEFAULT_LIMIT = 200
EXAMPLE = "jarvise ingest --symbol BTCUSDT --timeframe 1h --skip-derivatives --json"
BACKFILL_EXAMPLE = (
    "jarvise ingest --symbol BTCUSDT --timeframe 4h --since 2021-01-01 --skip-derivatives"
)
UNIVERSE_EXAMPLE = (
    "jarvise ingest --universe paper_core --timeframe 4h --skip-derivatives --json"
)


def _parse_instant(raw: str) -> datetime:
    """ISO-8601 date or datetime; a bare date means midnight UTC."""
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


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
        f"  {BACKFILL_EXAMPLE}\n"
        f"  {UNIVERSE_EXAMPLE}",
    )
    p.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Trading pair, e.g. BTCUSDT (repeatable or comma-separated)",
    )
    p.add_argument(
        "--universe",
        help=(
            f"Resolve symbols from a point-in-time universe "
            f"(seeded: {PAPER_CORE}); may combine with --symbol"
        ),
    )
    p.add_argument(
        "--timeframe",
        default="1h",
        choices=sorted(ALLOWED_INTERVALS),
        help="Candle timeframe (default: 1h)",
    )
    p.add_argument(
        "--limit",
        type=int,
        help=(
            f"Candles to fetch, 1..{MAX_PAGE_LIMIT} "
            f"(default: {DEFAULT_LIMIT}; page size when --since is set, "
            f"defaulting to {MAX_PAGE_LIMIT})"
        ),
    )
    p.add_argument(
        "--since",
        help="Backfill from this ISO-8601 instant, paging past the request cap",
    )
    p.add_argument(
        "--until",
        help="Stop the backfill here (requires --since; default: now)",
    )
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
    if not args.symbols and not args.universe:
        print(
            f"Error: --symbol or --universe is required.\n  {EXAMPLE}\n  {UNIVERSE_EXAMPLE}",
            file=sys.stderr,
        )
        return 2
    symbols = _parse_symbols(args.symbols or [])
    if args.until and not args.since:
        print(
            f"Error: --until requires --since.\n  {BACKFILL_EXAMPLE}", file=sys.stderr
        )
        return 2
    since: datetime | None = None
    until: datetime | None = None
    for flag, raw in (("--since", args.since), ("--until", args.until)):
        if not raw:
            continue
        try:
            parsed = _parse_instant(raw)
        except ValueError:
            print(
                f"Error: {flag} must be ISO-8601, e.g. 2021-01-01 or "
                f"2021-01-01T00:00:00Z.\n  {BACKFILL_EXAMPLE}",
                file=sys.stderr,
            )
            return 2
        if flag == "--since":
            since = parsed
        else:
            until = parsed
    if since and until and since >= until:
        print(
            f"Error: --since must be before --until.\n  {BACKFILL_EXAMPLE}",
            file=sys.stderr,
        )
        return 2

    limit = args.limit
    if limit is None:
        limit = MAX_PAGE_LIMIT if since else DEFAULT_LIMIT
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        print(f"Error: --limit must be 1..{MAX_PAGE_LIMIT}\n  {EXAMPLE}", file=sys.stderr)
        return 2

    if not args.skip_derivatives and not resolve_api_key():
        print(
            "Error: COINGLASS_API_KEY not set.\n"
            "  export COINGLASS_API_KEY=... or jarvise ingest --symbol BTCUSDT --skip-derivatives",
            file=sys.stderr,
        )
        return 2

    # Resolve --universe against membership at the end of the window (or now).
    # Opens the DB briefly to seed paper_core and query; closed before ingest.
    universe_id = args.universe
    if universe_id:
        resolve_conn = open_db(args.db)
        try:
            if universe_id == PAPER_CORE:
                seed_paper_core(resolve_conn)
            as_of_ms = int((until or datetime.now(timezone.utc)).timestamp() * 1000)
            from_universe = universe_as_of(resolve_conn, universe_id, as_of_ms)
        finally:
            resolve_conn.close()
        if not from_universe and not symbols:
            print(
                f"Error: universe {universe_id!r} has no eligible symbols at "
                f"as_of={as_of_ms}.\n  {UNIVERSE_EXAMPLE}",
                file=sys.stderr,
            )
            return 2
        seen: set[str] = set()
        merged: list[str] = []
        for sym in symbols + from_universe:
            if sym not in seen:
                seen.add(sym)
                merged.append(sym)
        symbols = merged
    elif not symbols:
        print(
            f"Error: --symbol or --universe is required.\n  {EXAMPLE}\n  {UNIVERSE_EXAMPLE}",
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
            if since:
                market_summary[sym] = {
                    "timeframe": args.timeframe,
                    "since": since.isoformat(),
                    "until": until.isoformat() if until else None,
                    "page_limit": limit,
                }
            else:
                market_summary[sym] = {
                    "timeframe": args.timeframe,
                    "planned": limit,
                }
            if args.skip_derivatives:
                skipped.append(f"derivatives:{sym}")
            else:
                deriv_summary[sym.split("USDT")[0] if "USDT" in sym else sym] = {
                    "planned": min(limit, 30)
                }
        payload = {
            "ok": True,
            "dry_run": True,
            "db": str(args.db),
            "universe": universe_id,
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
                if since:
                    candles = fetch_klines_range(
                        sym,
                        args.timeframe,
                        int(since.timestamp() * 1000),
                        until_ms=int(until.timestamp() * 1000) if until else None,
                        page_limit=limit,
                    )
                else:
                    candles = fetch_klines(sym, args.timeframe, limit)
                n = upsert_market_technicals(conn, candles)
                derived = recompute_indicators(conn, sym, args.timeframe)
                gaps = find_gaps(conn, sym, args.timeframe)
                market_summary[sym] = {
                    "timeframe": args.timeframe,
                    "upserted": n,
                    "indicators_recomputed": derived,
                    "gaps": len(gaps),
                }
                if not args.as_json:
                    print(
                        f"ingested market_technicals: {sym} {args.timeframe} rows={n} "
                        f"(indicators recomputed over {derived} stored candles)"
                    )
                    if gaps:
                        first = gaps[0]
                        print(
                            f"  warning: {len(gaps)} gap(s) in the stored series; "
                            f"{first[2]} candles missing after {first[0]}"
                        )
            except Exception as exc:  # noqa: BLE001 — surface provider errors
                errors.append(str(exc))
                print(f"Error: {exc}", file=sys.stderr)

            if args.skip_derivatives:
                skipped.append(f"derivatives:{sym}")
                continue
            try:
                rows = fetch_derivatives(sym, args.timeframe, limit=min(30, args.limit))
                # One knowledge-time stamp per ingest run so the batch is a unit.
                n = append_derivatives(conn, rows)
                coin = rows[0]["symbol"] if rows else sym
                deriv_summary[coin] = {"versions_appended": n, "fetched": len(rows)}
                if not args.as_json:
                    print(
                        f"ingested derivatives_analytics: {coin} "
                        f"versions={n} fetched={len(rows)}"
                    )
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
        "universe": universe_id,
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
            if "since" in info:
                print(
                    f"  market_technicals {sym} {info['timeframe']} backfill "
                    f"since={info['since']} until={info['until'] or 'now'} "
                    f"page={info['page_limit']}"
                )
            else:
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
