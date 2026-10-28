# Assist explore list (2026-10-28)

Owner-managed coins for the trade assistant, separate from the seven core symbols. This PR is **D1 only** (spec, list, data, analysis). D2 is specified here and is not implemented.

## Decisions

a. The owner adds and removes coins from Telegram. At most 10. Each coin must be a Binance USD-M `PERPETUAL`, quote `USDT`, status `TRADING`, and not one of `ASSIST_SYMBOLS`. The core spot kline source must be able to serve it.
b. **D2.** Explore signals share the core risk budget and shared leverage (PR #238). Core candidates rank first. Explore candidates are dropped first by `risk_cap` / `margin_cap`.
c. **D2.** Explore has its own virtual track, reported on its own in the Monday summary.
d. Same analyzer and rules as core: floors 0.55/0.70, 1.5× ATR stop, 2R target, 0.3R chase, and the same MTF setting as the paper-run. **D2** messages are labelled `🧪 ทดลอง` and include one line that these coins have no evidence yet.

## D1 data flow

The list lives in `assist_explore_symbols` on the main analytics DB (`symbol`, `added_at_ms`). Candles, indicators, and `analysis_output` go only to `assist-explore.db` beside that file (no new env var).

`POST /jobs/assist-explore` and `jarvise assist explore` read the list, fetch closed klines with `jarvise_ingest.providers.binance_klines.fetch_klines` (the core ingest source, `https://api.binance.com/api/v3/klines`), store them in the explore file, call `recompute_indicators`, then call `analyze_mtf` when `mtf_enabled()` is true and the paper timeframe differs from `htf_timeframe()`, otherwise `analyze_snapshot`, with `CONFIDENCE_THRESHOLD`. Add time seeds 1000 closed `4h` bars and, when MTF is on, 1000 closed HTF bars (default `1d`, `JARVISE_ANALYZE_HTF`). Later runs refresh 200 closed bars, matching the core ingest refresh.

`jrvsAssistSignal4h` calls this endpoint and then `POST /jobs/assist-signal`. A failed explore node still starts the signal node. D1 sends no Telegram from the job. One symbol's fetch error is logged and skipped. The job does not engage the kill switch, does not mark a core stream unhealthy, and does not take the assist-signal lock.

Market safety stays on the paper path. The explore job does not call it, because an explore coin has no book row and that call would force FLAT.

## Untouched

Live stays off. The assistant never places orders. No new env vars. `ASSIST_SYMBOLS`, the universe, paper, shadow, Forward H3, the analyzer, indicator math, floors, the 1.5× ATR stop, `flat_exit`, and `same_side_hold` stay as they are. No writes to main `market_technicals`, `analysis_output`, paper, shadow, or approval tables.

## D2 (later, not this PR)

Signal scan, position watch, virtual track, and the Monday summary section. Ranking and the shared risk budget from decision (b). Message label from decision (d). Explore signals are not sent while the kill switch is engaged, or while core signals are held for a market-safety reason, because explore analysis skips market safety.

## Tests

List add/remove and Thai rejections (core symbol, past 10, non-perpetual, unknown, kline source). Empty `TELEGRAM_CHAT_ID` still ignores updates. Explore rows stay out of the main market and analysis tables. A short series analyzes FLAT. A warmed-up series writes `analysis_output`. One symbol's fetch failure does not drop the other.
