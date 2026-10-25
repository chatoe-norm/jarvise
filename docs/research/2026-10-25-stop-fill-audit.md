# Paper stop fills past the stop (audit, 2026-10-25)

Read-only audit on a copy of `data/analytics/jarvise-vps-ledger-ro.db` (data to 2026-10-17 04:15 UTC). Research overlay only; no ledger writes.

## Question

Six closed paper trades lost more than 1R against the stop the position opened with. Did the stop fail, or does the measurement overstate the loss?

The stop at open is `analysis.invalidation_price`. `engine.apply_signal` writes the same value to `paper_positions.stop_price` (`stop_source="analysis"`), so it is the right risk base.

## Findings

### 1. The fill rule fills at the worst price since entry, not at the stop

`evaluate_stop` (`src/jarvise_paper/stops.py:56` and `:70`) fills a long at `min(stop, low_since_entry, mid)`, and a short at the matching max. Once a wick crosses the stop, the fill is the extreme of every stored bar since entry, minus slippage.

| Closed (UTC) | Trade | PnL USD | R actual | R if filled at stop | Extra loss USD |
| --- | --- | --- | --- | --- | --- |
| 10-06 07:15 | BNB long | -2.18 | -1.21 | -1.15 | 0.10 |
| 10-07 08:52 | ETH long | -4.62 | -2.68 | -1.16 | 2.62 |
| 10-07 08:52 | BNB long | -2.93 | -2.05 | -1.17 | 1.26 |
| 10-07 08:52 | SOL long | -3.87 | -1.76 | -1.13 | 1.39 |
| 10-11 16:00 | ETH short | -2.53 | -1.16 | -1.13 | 0.06 |
| 10-12 13:15 | BTC long | -2.02 | -1.65 | -1.23 | 0.51 |

"R if filled at stop" keeps the recorded slippage and fees. The extra loss totals 5.94 USD, which is 44% of the ledger's total loss of 13.41 USD across 18 trades. At the stop, each trade loses about 1.13–1.23R. The 0.13–0.23R above 1R is fees and slippage, which are large against a stop distance of 1.2–2.0%.

When the monitor fires on time, the rule still costs money. On BTC 10-12, the crossing 1h bar (12:00) closed at 13:00, the monitor fired at 13:15, and it filled at that bar's low (82,651 less slippage) instead of the stop (83,036).

`src/jarvise_analyze/replay.py:367` uses the same `evaluate_stop`, with the crossing bar's low and high. Replay stop exits are therefore also filled past the stop, and the replay results in `2026-10-17-ledger-cause-and-replay.md` are biased negative on stopped trades.

Live is different. The live path places a venue-side `STOP_LOSS_LIMIT` (`src/jarvise_trade/binance_market.py:90`), which fills near the stop. So paper and replay are harsher than live would be.

### 2. On 10-07 the stops fired about six hours late, after an outage

- Order book, derivatives, and macro rows stop on **10-06 10:30**. The next rows appear at **10-07 08:51**, a 22.4 h gap. A Deploy run failed at **10-06 10:22** (`d6cc6d6`).
- Paper equity snapshots (the risk monitor, every 15 min) kept going until **10-07 04:45**. Prices were stale by then, so no stop fired, although ETH, BNB, and SOL had already crossed their stops in the 02:00 1h bar.
- Snapshots stopped from 04:45 to 08:52. The first run after the restart closed all three positions at 08:52, at the low of the 02:00 bar.
- A second gap followed, from 10-07 08:52 to 10-08 11:45 (26.9 h, no snapshots or order book rows).

`run_risk_monitor` (`src/jarvise_paper/risk_monitor.py`) never checks data freshness. It marks equity and evaluates stops on whatever closes are stored, with no alert when they are stale.

Kline timing cannot be proven from the ledger, because klines are backfilled. The outage is inferred from the order book, derivatives, macro, and snapshot gaps.

**Root cause: the VPS disk filled up.** The owner's Telegram history for 2026-10-06 shows the following messages (UTC+7 times converted to UTC):

- 10:30 UTC: `KILL-SWITCH ENGAGED`, with reason `market_safety:provider_error: book:database or disk is full; provider_error: derivatives:database or disk is full`.
- From 13:00 UTC, every hour: `ingest health (4h)`, saying the newest 4h candle closed 300 min, then 360 min, ago.

The Deploy failure at 10:22 UTC ("Rebuild the paper stack") is most likely the same disk-full condition. The alerts fired as designed. What was missing was a warning before the disk was full. Container logs from that window are gone, because the containers were recreated on 2026-10-25.

### 3. Not a stop failure

Every trade with a stop on its position exited by `stop_hit`, or closed earlier by rule or by hand. The extra loss past 1R comes from the fill rule (finding 1) and the stale-data outage (finding 2).

## Frozen 2026-10-17 grid re-run on PR #225

Rule and window are unchanged from `2026-10-17-ledger-cause-and-replay.md`: `paper_core` (BTCUSDT, ETHUSDT), `4h`, `--paper-policy rules`, `--since 2024-01-01 --oos-since 2025-07-01 --until 2026-10-17`. The one decision cell is MTF on × `--stops trail`. It passes only if IS and OOS each have at least 8 closed trades, OOS EV ≥ 0, and OOS EV ≥ IS EV. The other cells are sensitivity only.

Data is a fresh copy of the 2026-10-17 VPS ledger backup for each run. The measuring tool was checked first: `main` (`39bf510`) reproduces all six cells of §2.1 exactly, for example 423 / −0.613 and 411 / −0.625 on the decision cell.

| Cell | main IS n / EV | main OOS n / EV | PR IS EV | PR OOS EV | PR + mid fix IS EV | PR + mid fix OOS EV |
| --- | --- | --- | --- | --- | --- | --- |
| MTF off, stops off | 349 / −0.248 | 305 / −0.298 | −0.248 | −0.298 | −0.248 | −0.298 |
| MTF off, fixed | 396 / −0.457 | 347 / −0.435 | −0.343 | −0.334 | −0.343 | −0.334 |
| MTF off, trail | 691 / −0.773 | 598 / −0.660 | −0.363 | −0.274 | −0.394 | −0.316 |
| MTF on, stops off | 220 / −0.111 | 215 / −0.155 | −0.111 | −0.155 | −0.111 | −0.155 |
| MTF on, fixed | 248 / −0.321 | 241 / −0.306 | −0.190 | −0.192 | −0.190 | −0.192 |
| **MTF on, trail (decision)** | 423 / −0.613 | 411 / −0.625 | **−0.219** | **−0.245** | **−0.247** | **−0.280** |

Trade counts are identical across all three columns. Only the fill price changes, and the `--stops off` cells do not move.

**Decision cell: fails** on every column. OOS EV is below 0 and below IS EV. The resting-stop fill cuts the loss per trade by about 60%, but no cell turns positive. The analyzer and the exit rules stay as they are.

### Review finding on PR #225: the `mid` fallback fills at the stop when price is already through it

In `resting_stop_fill`, when no bar after `stop_moved_ts` crossed the stop, the fallback fills at `stop_f` even when the newest close is already beyond the stop. In replay trail mode this happens whenever the stop moves on a bar that closes through the new stop. The decision cell has 51 such fills out of 450 stop exits, and every one had the close beyond the stop. A resting stop placed after that close cannot fill at a better price than the close, so the PR is optimistic there. `main` filled these at the close.

The "PR + mid fix" columns fill the fallback at the close when the close is beyond the stop. That is the correct rule. It costs 0.03–0.04 EV on the trail cells and changes no verdict.

## Re-run of the daily long-only study with the corrected fill (registered before running)

The §3 study in `2026-10-17-ledger-cause-and-replay.md` decided on `--stops fixed` cells, and those exits used the old worst-extreme fill. This is a measurement correction of the same kind as §2.1 there, not a new hypothesis. It is run once.

- The rule is unchanged from §3. Same data copy (the 2026-10-17 VPS ledger backup), same seven symbols, `1d`, `--paper-policy rules`, `--stops fixed`, no `--mtf`, and the same windows W0, W1, W2, each a separate replay with a fresh ledger copy.
- The only decision cells are H1 `trend_long` and H2 `donchian_long`. A cell passes only if every window has at least 8 closed trades and EV ≥ 0.
- Tool check first: `39bf510` (before PR #225) must reproduce the §3 decision table. The corrected run uses `main` at `0775e9f`.
- Reference only, never selected: the trail cells and `current`.
- If neither cell passes, the long-only question stays closed. If one passes, it earns forward paper confirmation only. It does not earn live trading, and it does not change `analyze_snapshot`.

### Result

The tool check passed: `39bf510` reproduces every cell of the §3 tables exactly. Trade counts are identical before and after the correction. The table shows EV in USD per closed trade, old fill → corrected fill.

| Cell | W0 n / EV | W1 n / EV | W2 n / EV | Verdict |
| --- | --- | --- | --- | --- |
| **H1 trend_long fixed** | 90 / 0.234 → **0.686** | 212 / −2.181 → **−1.656** | 77 / −1.865 → **−1.292** | **fails** |
| **H2 donchian_long fixed** | 50 / −2.991 → **−1.602** | 63 / 10.493 → **12.437** | 60 / −2.175 → **−1.548** | **fails** |
| ref: trend_long trail | 159 / −0.869 → 0.162 | 341 / −2.583 → −1.034 | 134 / −2.077 → −0.972 | not selectable |
| ref: donchian_long trail | 88 / −1.635 → 0.508 | 109 / −2.083 → 1.371 | 86 / −1.186 → 0.504 | not selectable |
| ref: current fixed | 170 / −0.010 → 0.339 | 272 / −2.264 → −1.798 | 191 / −0.121 → 0.218 | not selectable |

Neither decision cell passes, and the daily long-only study stays closed.

The `donchian_long trail` reference cell is now ≥ 0 in all three windows. It was picked out after seeing five cells under a changed tool, so it cannot be adopted from this data. At most it can be registered as a new hypothesis and tested on forward data only.

## Proposed changes

1. **Freshness guard (no locked area):** when the newest closed bar is stale, the risk monitor skips stop and mark writes and sends one alert.
2. **Stop fill model (owner approval required, adjacent to the 1.5× ATR stop):** fill a stop as a resting stop order. A long fills at `stop`, or at the crossing bar's open when that bar opened below the stop (a gap). A short is the mirror. The rule must be the same in paper and replay. After it lands, re-run the frozen 10-17 replay grid with no new cells.
