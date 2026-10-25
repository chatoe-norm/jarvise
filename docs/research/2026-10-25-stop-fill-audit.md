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

Kline timing cannot be proven from the ledger, because klines are backfilled. The outage is inferred from the order book, derivatives, macro, and snapshot gaps. The host-side cause needs VPS logs.

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

## Proposed changes

1. **Freshness guard (no locked area):** when the newest closed bar is stale, the risk monitor skips stop and mark writes and sends one alert.
2. **Stop fill model (owner approval required, adjacent to the 1.5× ATR stop):** fill a stop as a resting stop order. A long fills at `stop`, or at the crossing bar's open when that bar opened below the stop (a gap). A short is the mirror. The rule must be the same in paper and replay. After it lands, re-run the frozen 10-17 replay grid with no new cells.
