# Assist move-stop rule: breakeven at 0.5R vs breakeven at 1R with an ATR distance guard

Date: 2026-11-10. Status: pre-registered in §2 before the §3 run. Research only; no production code changed by this note.

## 1. Why

The owner's SOL short (entry 86.47, 1R = 2.02 USDT) got a breakeven move-stop advice to 86.30 and closed at +0.03R. The question is whether the current rule lowers expectancy.

Current rule (`src/jarvise_paper/profit_lock.py` `trail_stop_with_source`, used by `src/jarvise_assist/trail_advice.py`):

- A closed 1h bar's extreme since entry reaches +0.5R: stop moves to entry ∓ 20 bps.
- The extreme reaches +1R: stop trails 1R behind the best price.
- The trigger is the past extreme, not the current profit, and there is no minimum distance from the mark. On the SOL trade the MFE was 0.61R, the mark was near +0.3R when the advice was sent, and the new stop sat 0.29 ATR(4h) from the mark.

### 1.1 What was already seen before pre-registration (2026-11-10, descriptive only)

Source: VPS ledger read-only at 2026-11-10 05:17Z (user_version 26) and Binance public 5m mark-price klines. The scratch simulator reproduced the four real moved-stop outcomes within 0.03R.

Real closed `jv-` trades (n=9, 2026-11-05 to 11-10): win rate 78%, average win +0.88R, average loss −1.08R, expectancy +0.44R per trade, net about −0.12 USDT. The R sum is positive and the USDT sum is near zero because risk per trade grew from 0.5 to 2 USDT, and both full losses came at 2 USDT.

Expectancy per trade on the same sets (trades still open are marked to market):

| Set | Current rule | Hold stop and 2R TP | BE at 1R, ≥1 ATR, no trail | BE at 1R, ≥1 ATR, then trail |
| --- | --- | --- | --- | --- |
| 9 real trades | +0.44R | +0.25R | +0.31R | +0.33R |
| 20 virtual signals | +0.42R | +0.52R | +0.51R | +0.50R |
| Cohort v1 only (7) | −0.20R | +0.22R | +0.26R | +0.19R |

The four real trades with a moved stop: the rule saved two ADA trades that would have hit the initial stop (−1.04R each became +0.03R and +0.10R) and cut two SOL trades that are still open under "hold" (+0.48R and +0.16R marked to market).

These numbers do not decide the question. The gap is about 0.1R per trade and the standard error at n=20 is about 0.3R. This sample (2026-10-27 to 11-10) is therefore **already seen** and cannot serve as a clean out-of-sample test.

## 2. Pre-registration

### 2.1 Data

- Signals: `analysis_output` 4h rows in a copy of `data/analytics/jarvise-vps-ledger-ro.db` (rows 2026-09-29 to 2026-10-24). Kept when the action is long or short, confidence ≥ 0.55, `invalidation_price` is not null, and the symbol is one of the seven assist symbols.
- Same-idea filter, independent of the stop policy: a signal is taken only when no signal for the same symbol and side was taken in the previous 24 hours.
- Prices: Binance USDⓈ-M public 5m mark-price klines.

### 2.2 Trade model (the same for every cell)

- Entry time = bar open + 5h (the publication lag). Entry price = open of the first 5m bar at or after that time.
- Stop = stored `invalidation_price`. A signal is dropped when the stop is not at least 0.2% beyond the entry on the losing side.
- TP = entry ± 2R.
- Within a 5m bar the stop is checked before the TP. A gap through the stop fills at the bar open.
- Exit on flat or opposite: the newest 4h analysis available at each 1h boundary (bar open + 5h); exit at that 5m open.
- A trade still open at the last analysis time + 5h is closed at the mark at that time.
- Cost: 12 bps round trip (fees 10 bps, slippage 2 bps). Funding is ignored.
- Stop moves are evaluated at each 1h boundary on the extremes of the full 1h bars closed since entry, and take effect at once (no tap delay, no night hold).

### 2.3 Cells

- **CUR (baseline):** breakeven at 0.5R to entry ∓ 20 bps; at ≥1R trail 1R behind the best price.
- **PROP (the one decision cell):** no move before the extreme reaches 1R. Then the candidate is the tighter of entry ∓ 10 bps and best price ± 1R. It is applied only when it is tighter than the current stop and at least 1×ATR(4h) from the mark (the 5m open at that boundary). ATR is `atr_14` of the signal's 4h bar; when it is missing, |entry − stop| / 1.5.
- Sensitivity only: **HOLD** (no stop move) and **PROP-BE** (PROP without the trail).

### 2.4 Pass criteria

PROP passes in-sample when both hold:

1. mean net R of PROP > mean net R of CUR, on the same signals;
2. mean net R of PROP > 0.

The paired difference and a bootstrap 90% interval are reported for information. If PROP fails, the search stops: no other cell is promoted. If PROP passes, the rule change goes to a PR, and the forward test is the new virtual cohort with a "hold" shadow line, because the 2026-10-27 to 11-10 sample was already seen (§1.1).

## 3. Result (run 2026-11-10, after §2 was written)

**PROP fails criterion 1. The search stops and the move-stop rule stays as it is.**

In-sample: 39 signals from 2026-09-29 to 2026-10-24 (15 long, 24 short; BTC 8, ETH 11, SOL 4, BNB 2, ADA 6, DOGE 8). The same-idea filter removed 152 rows and 2 fell after the window end.

| Cell | n | Win rate | Avg win | Avg loss | Expectancy | Sum |
| --- | --- | --- | --- | --- | --- | --- |
| CUR (baseline) | 39 | 67% | +0.51R | −0.65R | **+0.119R** | +4.66R |
| PROP (decision cell) | 39 | 51% | +0.92R | −0.78R | **+0.094R** | +3.68R |
| HOLD (sensitivity) | 39 | 46% | +1.07R | −0.83R | +0.046R | +1.78R |
| PROP-BE (sensitivity) | 39 | 44% | +1.02R | −0.67R | +0.065R | +2.52R |

- PROP − CUR, paired: −0.025R per signal, bootstrap 90% interval [−0.192, +0.148]. The outcome differs on 15 of 39 signals.
- Exits under CUR: initial stop 7, breakeven stop 13, trail stop 5, TP 5, flat 9. Under PROP: initial stop 12, trail stop 6, TP 7, flat 13, window end 1.
- The current rule turns five initial-stop losses into scratches and gives up two TPs. On this sample the two effects cancel within noise.

Reading:

- No cell is distinguishable from another at n=39. The data neither shows that the current rule hurts nor that the proposed rule helps.
- Per §2.4 no rule change is proposed. The out-of-sample run is not done because the decision cell did not pass.
- The measurement gap is real: the ledger does not store what "hold" would have returned, so each week's question has to be re-simulated by hand. The follow-up is a "hold" shadow line on `assist_track` and in the weekly summary, with no change to advice.

Limits: signals cluster in time and across correlated coins, so the effective sample is smaller than 39. Tap delay and the night hold are not modelled. Stops and TP use mark-price 5m bars. Funding is ignored.

Scratch script: `be_rule_sim.py` in the session scratchpad (not in the repo).
