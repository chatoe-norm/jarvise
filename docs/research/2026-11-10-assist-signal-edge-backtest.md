# Does any entry signal have an edge? Pre-registered backtest, arms A (4h, control) and B (daily trend)

Date: 2026-11-10. Status: **run complete; arms A and B fail in-sample; no OOS run.** Research only. This note changes no production code, no VPS setting and no ledger. Verdict prose is for Claude.

## 1. Why

The trade assistant sends 4h futures signals from the analyzer, and the owner taps to execute. After two months the owner asks whether the system can make money.

The note `2026-11-10-assist-breakeven-rule.md` showed that the stop rule is not the lever. Three stop variants gave +0.05R to +0.12R per signal on 39 signals, and none could be told apart from zero. The open question is the entry signal.

A second reviewer (with a literature check) agreed with that reading and made these points, all adopted here:

- compare against random entries;
- test exits without the 2R cap;
- use a higher cost;
- model the tap delay and funding;
- test daily trend signals beside the 4h signal;
- keep to a small, counted number of configurations.

### 1.1 What is already known (disclosed before the run)

- **4h signal, paper exits:** `2026-10-17-ledger-cause-and-replay.md` §2 replayed this signal with paper exits (no TP, exit when confidence drops). The coins were BTC+ETH and SOL+BNB, 2024-01-01 to 2026-10-17. Every cell was negative. Gross R before costs was −0.003R per trade (fixed stops, BTC+ETH, in-sample).
- **Daily signals, long only:** the same note §3 tested daily `trend_long` and `donchian_long` (single channel, fixed stops). The coins were the seven assist coins, in three windows from 2022-11-01 to 2026-10-17. Both failed. The rules in arm B differ (long and short, three-channel ensemble, volatility targeting, channel exits), but the period 2022-11 to 2026-10 has been seen for daily trend on these coins.
- `2026-10-25-donchian-trail-forward.md` registered a daily Donchian forward test. Its results are not read for this note.
- 2026-09-29 to 2026-11-10 was examined on 2026-11-10 and is not used as evidence.
- **Literature (from the reviewer; not all verified by Claude):** support for trend in crypto is at daily-to-monthly horizons, and none was found for 4h net of costs. The reviewer marked several items as abstract-only. The summary of Hudson & Urquhart (2021) should be checked against the paper before it is cited.
- **Stated expectations:** arm A is more likely to fail than to pass. It is the control. Arm B has better prior support, but its later period is partly seen.

## 2. Arm A: the current 4h signal

### 2.1 Data

- Candles and indicators come from `market_technicals` `4h` and `1d` in a copy of `data/analytics/jarvise-vps-ledger-ro.db`.
- Usable range: 4h bars opening **2022-11-01 to 2026-10-24**. Daily `ema_200` exists only from 2022-10-23.
- Symbols: BTCUSDT ETHUSDT SOLUSDT BNBUSDT ADAUSDT DOGEUSDT XRPUSDT. This equals the active list from `jarvise universe show` on the VPS on 2026-11-10.
- Fill prices come from Binance USDⓈ-M public 15m mark-price klines, and funding from Binance public funding history. Both exist back to 2022-11-01.

### 2.2 Signals and take rule

- Signals are regenerated with the project's code, as `replay_range` does it: `analyze_mtf(candle, closed_htf_candle(...), confidence_threshold=0.55)`.
- A signal needs action long or short, confidence ≥ 0.55 and an invalidation price.
- A signal is taken when no trade is open on that symbol in the cell, and no signal for that symbol and side was taken in the previous 24 hours.
- Portfolio caps are not applied.

### 2.3 Trade model

- **Entry:** a bar opening at T is published at T + 5h. Entry is the open of the 15m bar at T + 5h + 15 min (tap delay).
- **Stop:** the invalidation price, so 1R = |entry − stop|. A signal is dropped when the stop is less than 0.2% beyond the entry.
- **Fills:** within a 15m bar, the stop is checked before the TP. A gap through the stop fills at the bar open.
- **Analysis exit:** when the newest analysis is flat or opposite, the trade exits at the 15m open 15 minutes after publication.
- **Stop moves:** computed at each 1h boundary from the full 1h bars closed since entry. They take effect 15 minutes later.
- **Cost:** 20 bps round trip plus actual 8h funding. 12 and 30 bps are reported as sensitivity; the nine real trades measured about 13 bps.
- **End of data:** a trade still open is closed at the mark. Results are in R.

### 2.4 Cells

- **A1 (decision):** current assist exits. TP at 2R. Breakeven at +0.5R to entry ∓ 20 bps, then a 1R trail from +1R. Analysis exit on.
- **A2 (secondary):** no TP and no breakeven stage. A 1R trail runs from the first closed hour, never looser than the initial stop. Analysis exit on.

### 2.5 Random-entry null

- **Exits:** signals and random entries both use price-only exits (the cell's stops, TP and stop moves, no analysis exit, 30-day cap at the mark).
- **Draws:** for each taken signal, one random entry is drawn on the same symbol and side. Its publication time is uniform within ±14 days, and its stop is 1.5× `atr_14` of the drawn bar.
- **Repetitions:** 1,000 sets, seed 20261110.
- **Diagnostics only:** the sign-flipped signal; how often MFE reaches +0.5R, +1R and +2R before MAE reaches −1R; long versus short beside holding the coins; per year; per symbol.

### 2.6 Windows and pass criteria

- **Windows:** in-sample (IS) is 2022-11-01 to 2025-10-24, with three IS years starting each November. Out-of-sample (OOS) is 2025-10-25 to 2026-10-24, run once and only for a cell that passes IS.
- **IS pass** needs all six:
  1. at least 300 trades;
  2. mean net R > 0, and the lower end of a 95% bootstrap interval > 0 (resampled by ISO week of entry, 10,000 times);
  3. the price-only mean above the 95th percentile of the null;
  4. longs and shorts each ≥ 0 (a side with fewer than 30 trades is not gated);
  5. positive in at least two of three IS years;
  6. still > 0 after removing the best month.
- **OOS pass:** at least 50 trades, mean net R > 0, and a price-only mean above the null median.

## 3. Arm B: daily trend portfolio

Arm B is a portfolio rule, not a trade-by-trade signal. It is judged on daily portfolio returns, not on R per trade.

### 3.1 Data

- Signals use Binance **spot** daily klines (public `/api/v3/klines`) from 2017-08-17, or from a coin's listing date, to 2026-10-24.
- Execution prices use the 15m spot kline open at 00:15 UTC.
- A coin enters the portfolio once it has 120 daily closes. SOL starts in late 2020 and DOGE in late 2019.
- Funding is the actual perp rate on signed notional at each 8h settlement. Before a coin's perp listed, a flat +0.01% per 8h is assumed: longs pay it and shorts receive it.

### 3.2 Cells

Both cells are long and short, run on the seven coins, and fixed as written. No parameter is chosen after a run.

- **B1, time-series momentum ensemble:** the signal on each coin is the mean of sign(close / close 20 days earlier − 1), and the same for 60 and 120 days. It takes the values −1, −1/3, +1/3 or +1. Signals are computed on the Monday 00:00 UTC close, and trades happen at 00:15.
- **B2, Donchian ensemble:** for each n in {20, 55, 100}:
  - a sub-position goes long when the daily close exceeds the highest high of the prior n days, and short when it falls below the prior n-day lowest low;
  - a long exits when the close falls below the prior n/2-day lowest low, and a short exits symmetrically;
  - the signal is the mean of the three sub-positions, checked daily and traded at 00:15 the next day.

### 3.3 Sizing and costs (both cells)

- **Weight per coin:** signal × (0.20 / realised volatility). Realised volatility is the annualised standard deviation of the last 60 daily returns.
- **Portfolio:** coin weights are divided by the number of active coins. Gross exposure is capped at 2× equity.
- **Cost:** 10 bps per side on traded notional, plus funding. 6 and 15 bps per side are reported as sensitivity.

### 3.4 Windows

- **IS:** 2018-01-01 to 2022-10-31. No study in this repo has used this period for daily trend.
- **OOS:** 2022-11-01 to 2026-10-24, run once and only for a cell that passes IS. This period is partly seen (§1.1). An OOS pass is consistency, not proof. Proof needs a forward record.

### 3.5 Pass criteria (IS, all four)

1. Annualised net Sharpe > 0, and the lower end of a 95% interval > 0. The interval comes from a stationary block bootstrap of daily returns with a mean block of 20 days, resampled 10,000 times.
2. Deflated Sharpe probability ≥ 0.95 (Bailey and López de Prado 2014). It is computed with N = 6, the total number of configurations in this study: A1, A2, B1, B2, and the long-only variants of B1 and B2 that are reported as sensitivity.
3. Net return > 0 in at least three of the five IS calendar years (2022 counted to October).
4. Still positive after removing the best calendar month.

- **OOS pass:** net Sharpe > 0.
- **Reported beside the result:** an equal-weight buy-and-hold of the same coins (Sharpe, CAGR, max drawdown); the long-only variants; and turnover, cost and funding as shares of gross return.

## 4. What follows

- **A1 passes IS and OOS:** the current signal has evidence. The next step is a forward record at fixed risk.
- **A1 fails and A2 passes:** A2 becomes a candidate exit set. It needs the owner's approval, a PR and a forward test.
- **B1 or B2 passes IS and OOS:** a daily portfolio becomes the candidate.
  - It needs a forward paper record and the owner's decision to build it.
  - The Telegram assist would carry weekly or daily rebalances, not per-trade signals.
  - If the cell does not beat buy-and-hold on Sharpe, that is stated plainly, because holding is the simpler choice.
- **Everything fails:** the 4h signal and these two daily rules are closed. The next ideas need their own pre-registration and count against the same configuration budget.
- **After results are seen:** no cell, threshold, window, symbol or cost is added or changed.
- **Expected money:** passing shows an edge exists. It does not by itself reach any income target; the money follows from the edge size times the capital.

## 5. Tool checks before any result is read

The run stops and reports if any check fails.

- **T1, signal parity:** regenerated 4h rows for 2026-09-29 to 2026-10-24 match the stored `analysis_output`. The action must match on ≥ 99% of rows, and the invalidation must be within 0.01%.
- **T2, simulator parity:** with zero tap delay and 10 bps, the simulator reproduces the four real moved-stop trades within 0.03R.
- **T3, no look-ahead (arm A):** the daily bar joined to a 4h bar at T must satisfy `open + 1 day ≤ T`. The trade loop must read no 15m bar before entry (the PR #179 class).
- **T4, no look-ahead (arm B):** a signal computed from the close of day D trades no earlier than day D+1 00:15. A unit test shifts a price spike by one day and checks that the position moves one day later.
- **T5, data:** no gap longer than one bar in any coin's daily or 15m series after its start. Gaps are listed if found.

## 6. Limits

- The seven coins are large today, so the sample has survivorship bias.
- The night hold, tick rounding and lot steps are not modelled.
- Arm A uses mark prices for both stops and TP.
- Arm B uses spot prices with perp funding, and assumes funding before perps listed.
- Correlated coins make trades cluster. The bootstraps address this only in part.

## 7. Result

### 7.1 Tool checks run by Claude Code (2026-11-10)

**T3a passes.** The 4h signals were regenerated for 2022-11-01 to 2026-10-24: 61,068 rows over seven symbols, with `analyze_mtf` and `closed_htf_candle` on a ledger copy. No joined daily bar was unclosed at the 4h open.

**T1 fails as written, and the cause is production history, not the tool.** On 2026-09-29 to 10-24, the action matched on 580 of 659 rows (88.0%); invalidation matched on 146 of 146. The 79 misses:

- 44: production ran with MTF off until the 2026-10-15 08:00 bar (stored short, regenerated flat).
- 36: production stored "Insufficient indicators (warm-up)" before its daily EMA200 existed (2026-09-29 to 10-01).
- 4: HTF chaotic on one side only, all before 10-15.
- 1: a production-only market safety veto (`derivatives_stale_ms`).
- 1: an HTF join timing difference (ADA 10-18 20:00).

After the last non-MTF row (2026-10-15 08:00), the action matched on **377 of 379 rows (99.47%)**. That clears the 99% bar on the period whose configuration equals today's. This restriction was chosen after T1 was seen, so it is a disclosed deviation; no outcome had been computed when it was made.

Two known divergences remain and are not modelled:

- the market safety veto;
- for 4h bars opening at 20:00, production joins the daily bar that closed at 00:00, while the replay join uses the bar closed before the 4h open. The difference touches at most 1 in 6 bars.

### 7.2 Runs

Run 2026-11-10 by Cursor in `D:\Projects\jarvise-research\2026-11-10\` (scripts and data outside the repo). Arm A IS unchanged from the first pass. Arm B IS was re-run after the look-ahead and funding fixes below; OOS was not run. No cell, threshold, window, symbol or cost was changed after results were seen.

#### Tool checks (before outcomes)

| Check | Result |
| --- | --- |
| T2 (zero tap, 10 bps, four moved-stop trades) | Pass. Sim R: +0.040 / +0.078 / +0.049 / +0.054 vs targets +0.03 / +0.10 / +0.04 / +0.03 (all within 0.03R). |
| T3b (no 15m bar before entry) | Pass. |
| T4 (arm B date arithmetic) | Pass on the first pass. It only checked that a spike-day signal fills the next day; it did not catch the MTM look-ahead. |
| T4a (truncation, five D, seed `20261110`) | Pass after the fix. Weights, vol-target values, and Donchian sub-positions at each D+1 00:15 fill match the full-data run. |
| T4b (+50% close on day D) | Old order fails (returns ending at D+1 00:15 change); fixed order passes (those returns unchanged). |
| T6 (funding interval) | Pass. Synthetic rows at 08:00:00.020 and at a 4h offset are each charged once on pre-rebalance notional; long pays a positive rate. |
| T5 (gaps) | Listed: one missing 15m mark bar at 2023-11-10 03:30–04:00 UTC on all seven coins; one missing spot 00:15 day on BTC/ETH/BNB at 2018-02-08→10. No other post-start gaps. |

#### Tool failures found in review

1. **Look-ahead in arm B MTM.** `run_portfolio_clean` rebalanced at 00:15 and then marked the interval that just ended with the new units, so new units earned day D’s return. Fixed by snapshotting `units_prev` before rebalance; price PnL and funding use `units_prev`; rebalance cost stays on the fill day’s equity change.
2. **Exact 8h funding match.** Funding charged only when `fundingTime` equalled the 00/08/16 grid. About 43% of BTC funding times are a few milliseconds late; SOL off-grid settlements were skipped. Fixed by charging every funding row with `prev_0015 < fundingTime <= this_0015` on pre-rebalance notional (assumed +0.01% per 8h before the coin’s first funding row).

The first arm B numbers on PR #321 (B1 Sharpe ~~1.381~~ / OOS ~~1.039~~; B2 Sharpe ~~2.374~~ / OOS ~~2.177~~) used the buggy code and are struck. The OOS window 2022-11-01 to 2026-10-24 was evaluated once under that buggy arm B code and cannot be unseen data for a later arm B variant. Arm A is unchanged (A1 −0.0665R, A2 −0.0611R); `sim_a._funding_r` already used the interval rule.

#### Arm A (4h signal) — IS 2022-11-01 to 2025-10-24; 20 bps + funding

| Cell | n | Mean net R | Boot 95% lo | Price-only mean | Null 95th | Long mean (n) | Short mean (n) | Year means (3) | Without best month | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | 2740 | −0.0665 | −0.1117 | −0.0658 | −0.0013 | −0.0537 (1690) | −0.0871 (1050) | −0.119, −0.064, −0.024 | −0.0786 | **Fail** |
| A2 | 2583 | −0.0611 | −0.1120 | −0.0305 | +0.0330 | −0.0411 (1609) | −0.0943 (974) | −0.104, −0.049, −0.038 | −0.0725 | **Fail** |

A1 criteria: (1) pass n≥300; (2) fail mean and boot lo > 0; (3) fail price-only > null 95th; (4) fail sides ≥0; (5) fail ≥2 of 3 years > 0; (6) fail without best month > 0. Null: observed price-only mean is below the null 95th percentile.

A2 criteria: same pattern — only (1) passes; (2)–(6) fail. OOS not run (IS fail).

Sensitivity (mean net R): A1 at 12 bps −0.0345, at 30 bps −0.1065. A2 at 12 bps −0.0284, at 30 bps −0.1020.

Diagnostics (A1): MFE before MAE−1R rates +0.5R 57.8%, +1R 28.8%, +2R 9.3%. Sign-flipped gross mean −0.0169R. Exits: stop 1831, flat/opposite 653, TP 255, end 1.

#### Arm B (daily portfolio) — corrected IS 2018-01-01 to 2022-10-31; 10 bps/side + funding

| Series | Days | Sharpe | Boot 95% lo | DSR (N=6) | Years >0 | Without best month | Max DD | vs B&H Sharpe | Pass IS | OOS |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B1 | 1764 | 1.016 | 0.068 | 0.935 | 3/5 | +0.628 (cum) | −0.153 | B&H 1.054 | **Fail** | not run |
| B2 | 1764 | 1.042 | 0.091 | 0.942 | 5/5 | +0.587 (cum) | −0.113 | B&H 1.054 | **Fail** | not run |
| B1 long-only | 1764 | 1.550 | 0.568 | 0.997 | 3/5 | +0.842 | −0.102 | — | Pass (sens.; not promoted) | not run |
| B2 long-only | 1764 | 1.343 | 0.278 | 0.988 | 3/5 | +0.611 | −0.115 | — | Pass (sens.; not promoted) | not run |

B1 and B2 fail criterion 2 (DSR ≥ 0.95). Criteria 1, 3, and 4 pass for both. Long-only variants are sensitivity only; they are not promoted and OOS is not run.

Equal-weight buy-and-hold (IS): Sharpe 1.054, CAGR 0.729, max DD −0.827.

Sensitivity Sharpe (corrected): B1 6 bps 1.030 / 15 bps 1.000; B2 6 bps 1.057 / 15 bps 1.024. Max \|drifted − target\|: B1 0.080, B2 0.003. Cost share of gross: B1 3.0%, B2 3.0%. Funding share of gross: B1 −11.9%, B2 −12.1%.

#### Deviations (disclosed)

- IS/OOS open trades marked at the window end (no OOS prices in IS).
- Bootstrap and arm-B RNG seed `20261110` (note seeds only the null explicitly).
- Realised vol annualised with √365.
- Long-only via `max(signal, 0)` with the same active-coin divisor.
- B1 fill at Monday 00:15 from the Monday 00:00 close.
- Deflated-Sharpe variance uses the four arm-B daily Sharpes; N = 6 counts A1/A2 as well.
- T2 used the four §1.1 fixtures with their ledger entry/exit timestamps on a schema-v13 ledger copy (no `assist_real_trade`).
- T5 one-bar gaps listed above; series otherwise continuous after each coin’s start.
- Corrected arm B OOS gated off after the look-ahead/funding fix; the 2022-11-01 to 2026-10-24 window was already seen under the buggy code.
