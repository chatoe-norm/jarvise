"""jarvise ingest CLI — paper analytics only. No order placement."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from jarvise_ingest.db import (
    append_derivatives,
    open_db,
    universe_as_of,
    upsert_macro_sentiment,
    upsert_market_technicals,
    upsert_order_book,
)
from jarvise_ingest.providers.binance_book import fetch_order_book_snapshot
from jarvise_ingest.providers.binance_klines import (
    MAX_PAGE_LIMIT,
    fetch_klines,
    fetch_klines_range,
)
from jarvise_ingest.providers.coingecko_global import fetch_global_macro
from jarvise_ingest.providers.derivatives import fetch_derivatives, select_provider
from jarvise_ingest.series import find_gaps, recompute_indicators
from jarvise_ingest.timeframes import ALLOWED_INTERVALS
from jarvise_ingest.universe import PAPER_CORE, seed_paper_core
from jarvise_risk import evaluate_from_db

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
        value = value.replace(tzinfo=UTC)
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
        "--skip-book",
        action="store_true",
        help="Skip Binance order-book microstructure ingest",
    )
    p.add_argument(
        "--skip-macro",
        action="store_true",
        help="Skip CoinGecko global macro ingest",
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

    universe_id = args.universe
    if universe_id:
        resolve_conn = open_db(args.db)
        try:
            if universe_id == PAPER_CORE:
                seed_paper_core(resolve_conn)
            as_of_ms = int((until or datetime.now(UTC)).timestamp() * 1000)
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
    book_summary: dict = {}
    macro_summary: dict = {}
    safety_summary: dict = {}
    skipped: list[str] = []
    errors: list[str] = []
    provider_errors: dict[str, list[str]] = {sym: [] for sym in symbols}

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
                coin = sym.split("USDT")[0] if "USDT" in sym else sym
                deriv_summary[coin] = {
                    "planned": min(limit, 30),
                    "provider": select_provider(),
                }
            if args.skip_book:
                skipped.append(f"book:{sym}")
            else:
                book_summary[sym] = {"planned": 1}
        if args.skip_macro:
            skipped.append("macro:global")
        else:
            macro_summary["global"] = {"planned": 1}
        payload = {
            "ok": True,
            "dry_run": True,
            "db": str(args.db),
            "universe": universe_id,
            "market_technicals": market_summary,
            "derivatives_analytics": deriv_summary,
            "order_book_microstructure": book_summary,
            "macro_onchain_sentiment": macro_summary,
            "market_safety": {},
            "skipped": skipped,
            "errors": errors,
            "duration_s": round(time.perf_counter() - started, 3),
            "note": "paper ingest only; no order placement",
        }
        _emit(payload, args.as_json, dry_run=True)
        return 0

    conn = open_db(args.db)
    try:
        if args.skip_macro:
            skipped.append("macro:global")
        else:
            try:
                macro_row = fetch_global_macro()
                n = upsert_macro_sentiment(conn, macro_row)
                macro_summary["global"] = {
                    "upserted": n,
                    "btc_dominance_pct": macro_row.get("btc_dominance_pct"),
                    "global_market_cap_usd": macro_row.get("global_market_cap_usd"),
                }
                if not args.as_json:
                    print(
                        "ingested macro_onchain_sentiment: "
                        f"btc_dom={macro_row.get('btc_dominance_pct')} "
                        f"mcap={macro_row.get('global_market_cap_usd')}"
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                for sym in symbols:
                    provider_errors[sym].append(f"macro:{exc}")
                print(f"Error: {exc}", file=sys.stderr)

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

            if args.skip_book:
                skipped.append(f"book:{sym}")
            else:
                try:
                    book_row = fetch_order_book_snapshot(sym)
                    n = upsert_order_book(conn, book_row)
                    book_summary[sym] = {
                        "upserted": n,
                        "bid_ask_spread": book_row.get("bid_ask_spread"),
                        "bid_depth_1pct_usd": book_row.get("bid_depth_1pct_usd"),
                        "ask_depth_1pct_usd": book_row.get("ask_depth_1pct_usd"),
                    }
                    if not args.as_json:
                        spread_bps = float(book_row["bid_ask_spread"]) * 10_000
                        print(
                            f"ingested order_book_microstructure: {sym} "
                            f"spread_bps={spread_bps:.2f}"
                        )
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
                    provider_errors[sym].append(f"book:{exc}")
                    print(f"Error: {exc}", file=sys.stderr)

            if args.skip_derivatives:
                skipped.append(f"derivatives:{sym}")
            else:
                try:
                    rows, provider = fetch_derivatives(
                        sym, args.timeframe, limit=min(30, limit)
                    )
                    n = append_derivatives(conn, rows)
                    coin = rows[0]["symbol"] if rows else sym
                    deriv_summary[coin] = {
                        "versions_appended": n,
                        "fetched": len(rows),
                        "derivatives_provider": provider,
                    }
                    if not args.as_json:
                        print(
                            f"ingested derivatives_analytics: {coin} "
                            f"provider={provider} versions={n} fetched={len(rows)}"
                        )
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
                    provider_errors[sym].append(f"derivatives:{exc}")
                    print(f"Error: {exc}", file=sys.stderr)

            safety = evaluate_from_db(
                conn,
                sym,
                skip_book=args.skip_book,
                skip_derivatives=args.skip_derivatives,
                skip_macro=args.skip_macro,
                provider_errors=provider_errors.get(sym) or None,
                engage_ks=True,
            )
            safety_summary[sym] = safety.as_dict()
            if safety.force_flat and not args.as_json:
                print(
                    f"market_safety {sym}: FLAT reasons={safety.reasons} "
                    f"critical={safety.critical} ks={safety.kill_switch_engaged}"
                )
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
        "order_book_microstructure": book_summary,
        "macro_onchain_sentiment": macro_summary,
        "market_safety": safety_summary,
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
        for sym, info in payload.get("order_book_microstructure", {}).items():
            print(f"  order_book_microstructure {sym} planned={info['planned']}")
        if payload.get("macro_onchain_sentiment"):
            print("  macro_onchain_sentiment global planned=1")
        print(f"db: {payload['db']}")
        print("paper ingest only; no order placement")


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
