# Assist exit: scale out 50% at +1R? (pre-registered 2026-11-09)

Question from the owner after ADA and SOL shorts on 2026-11-09 reached about +1.1R, then closed on the trail stop at +0.10R and +0.04R. The owner's own rule says "scale out and move the stop near BE around +1R". The assistant has no scale-out. It uses breakeven at +0.5R, a 1R trail after +1R, and a 2R target.

This note fixes the rule before any result is looked at. Read-only work on a copy of the ledger. Nothing in production changes from this note.

## Data

- Ledger copy `data/analytics/jarvise-vps-ledger-ro.db`, online backup taken 2026-11-09 16:14 Bangkok, sha256 `4967700ed3722e864c423cab10ef3306de33dee056645949921773635c36b4d8`. Never the VPS file.
- Signals: `analysis_output`, timeframe `4h`, the seven assist symbols (BTC, ETH, SOL, BNB, ADA, DOGE, XRP USDT).
- Price path: stored closed `1h` candles in `market_technicals` (spot klines, as the virtual track uses).

## Window and decision cell

- Signal bars with timestamp from 2026-10-08 00:00 UTC up to but not including 2026-11-07 00:00 UTC. Exits may run until the last stored 1h bar. A trade still open at the end of data is excluded and counted.
- One decision cell: all seven symbols, one open position per symbol (a new entry only after that symbol's simulated trade has exited). No limit across symbols.
- The entry set is fixed by variant A's timeline (one position per symbol under A). B and C are evaluated on the same entries, so every comparison is paired trade by trade. (Clarified before the run.)

## Entry

- A row passes `jarvise_assist.sizing.passes_signal_floor` (long/short, confidence ≥ 0.55, invalidation price present).
- Available at bar timestamp + 5 h (`track.ANALYSIS_LAG_MS`). Entry price is the open of the 1h bar that starts then. Stop is `invalidation_price`. R = |entry − stop|. A stop on the wrong side of the entry is skipped.
- The path starts on the next 1h bar (strictly after the entry, as `track._resolve_one` does).

## Exits (per 1h bar, in this order)

1. Stop check with the stop in force at the bar's start, gap-aware (`track.gap_aware_stop_fill`). Stop before anything else in the same bar (conservative).
2. Update best high and best low.
3. Variant scale-out check (B and C only).
4. Stop update (A, and C's remainder) with `jarvise_paper.profit_lock.trail_stop_with_source` (read-only import).
5. 2R target.
6. Analysis exit at the bar close: flat or opposite (`track._analysis_exit_reason` rule, no lookahead).

## Variants

- **A (current):** exactly the rules above, no scale-out.
- **B:** on the first bar whose favourable extreme reaches entry ± 1R, close 50% at entry ± 1R. The other 50% gets a fixed stop at entry (BE) from the next bar, no further trailing, and keeps the 2R target and analysis exit.
- **C:** same 50% close at +1R. The other 50% keeps variant A's rules (breakeven, 1R trail, 2R target, analysis exit).

## Costs and units

- 5 bps fee plus 1 bps slippage per side per fill, on the notional of each fill (`track.FEE_BPS_PER_SIDE`, `track.SLIPPAGE_BPS_PER_SIDE`). Funding is ignored.
- Net R per trade = net PnL / (qty × R), with planned R as the base.

## Metrics

n, mean net R per trade, sum R, win rate, max drawdown of cumulative R in exit-time order, share of trades reaching +1R, and of those the share that later reach 2R.

## Decision rule

Recommend switching to B or C only if all of these hold for that variant against A in the decision cell:

1. n ≥ 30 closed trades.
2. Mean paired difference (variant − A) ≥ +0.10R per trade.
3. That mean is greater than 2 × its standard error.
4. Max drawdown is not worse than A's.

Otherwise: no change. Report n_required ≈ (2 × SD of the paired difference / 0.10)², keep collecting forward data, and re-run at the weekly review after the Forward H3 window (about 2026-11-25). One change at a time.

## Measuring-tool check (before reading results)

Run variant A on the stored `assist_track` rows (their entry, stop, `sent_at_ms`). Net R must match the stored `net_r` within 0.02R, funding aside. If it does not, fix the simulator first.

## Sensitivity (not part of the decision)

- At most two open positions across symbols.
- Target-before-stop inside the same bar.

## Results (run 2026-11-09)

Script: session scratchpad `scale_out.py` (not in the repo), run with `PYTHONPATH=src` on the ledger copy above.

### Measuring-tool check

- The pre-registered 0.02R tolerance failed on a cost line, not on prices. Worst |diff| was 0.040R.
- On all 15 stored `assist_track` exits, variant A gives the same exit bar, exit price, and gross R.
- The 8 `core7_4h_v2` rows match the stored net R exactly, with funding added back.
- The 7 `core7_4h_v1` rows were scored by the older cost model, before 1 bps slippage per side was added. Recomputing them at 6 bps per side gives the simulator's numbers. All of their differences are this cost gap.
- Judged sound for the comparison: A, B, and C share the same cost model.

### Decision cell (7 symbols, 2026-10-08 to 2026-11-07, one position per symbol)

The cell has 73 entries. One was still open at the end of data, so n = 72. By symbol: DOGE 25, ADA 20, XRP 11, ETH 9, BTC 4, SOL 3, BNB 1.

| Variant | Mean net R | Sum R | Win rate | Max DD |
| --- | --- | --- | --- | --- |
| A current | +0.038 | +2.71 | 68% | 5.55R |
| B 50% at +1R, rest BE | +0.009 | +0.68 | 68% | 5.64R |
| C 50% at +1R, rest trail | +0.017 | +1.23 | 68% | 5.31R |

- 17 of 72 trades reached +1R. Under A, 8 of those 17 went on to the 2R target.
- B − A: mean −0.028R, SE 0.025, SD 0.214. n_required ≈ 18.
- C − A: mean −0.021R, SE 0.024, SD 0.202. n_required ≈ 16.

### Decision

- **No change.** Neither variant meets rule 2: the mean difference is negative for both, where rule 2 needs ≥ +0.10R.
- What this rules out: a gain of +0.10R per trade or more. The upper end of the estimate (mean + 2 SE) is +0.022R for B and +0.027R for C.
- What it does not prove: that scale-out is worse. The variants differ only on the 17 trades that reached +1R. Per affected trade the difference is about −0.12R (B) and −0.09R (C). If about 3 of the 8 trades that ran to 2R had reversed instead, the sign would flip. The size of the gain would still be far below +0.10R per trade.
- n_required uses the SD of the paired per-trade difference (0.214 for B, 0.202 for C, zeros included), not the SD of R per trade.
- No further variants are searched. The same frozen A/B/C rule is re-run once there are 20–30 real assist trades closed after 2026-11-06 (planned-R base), at the weekly review after the Forward H3 window.
- Why scale-out loses here: about half of the trades that reach +1R go on to 2R. Taking half off at +1R gives up more on those trades than it saves on the ones that reverse, such as ADA and SOL on 2026-11-09.

### Sensitivity results

- Target before stop in the same bar: A +0.038, B +0.030, C +0.038. C − A is +0.000. Still no gain.
- At most two open positions across symbols: not run.

### Caveats

- Variant A's own mean, +0.038R per trade, is a thin edge. This study was not designed to measure edge. Edge belongs to the paper EV gate and the Forward H3 review.
- In-sample window of one month.
- Spot 1h candles.
- Funding ignored.
- Stops follow the rule, not the owner's actual taps.
- DOGE and ADA make up 45 of the 72 trades.

## Appendix: replay script

Kept here so the frozen rule can be re-run on new data. Project rule: scratch scripts stay out of `src/` and `tests/`. Save it to a scratch folder and run from the repo root:

```bash
PYTHONPATH=src .venv/bin/python scale_out.py data/analytics/jarvise-vps-ledger-ro.db
```

```python
"""Read-only replay for docs/research/2026-11-09-assist-scale-out.md. Run with PYTHONPATH=src."""
import datetime as dt
import math
import sqlite3
import statistics as st
import sys

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.sizing import passes_signal_floor
from jarvise_assist.track import (
    ANALYSIS_LAG_MS,
    FEE_BPS_PER_SIDE,
    SLIPPAGE_BPS_PER_SIDE,
    _analysis_exit_reason,
    gap_aware_stop_fill,
)
from jarvise_paper.profit_lock import trail_stop_with_source

DB = sys.argv[1]
H = 3_600_000
COST = (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / 10_000.0
W0 = int(dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc).timestamp() * 1000)
W1 = int(dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc).timestamp() * 1000)

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row


def bars_after(symbol, after_ms):
    rows = conn.execute(
        "SELECT timestamp, open, high, low, close FROM market_technicals "
        "WHERE symbol=? AND timeframe='1h' AND timestamp > ? ORDER BY timestamp",
        (symbol, after_ms),
    ).fetchall()
    return [dict(r) for r in rows]


def simulate(symbol, side, entry, stop, sent_at, variant, target_first=False):
    r = abs(entry - stop)
    sgn = 1 if side == "long" else -1
    target = entry + sgn * 2 * r
    one_r = entry + sgn * r
    legs = []
    q = 1.0
    cur = stop
    best_hi = best_lo = None
    scaled = False
    be_fixed = False
    reached_1r = False

    def done(exit_ms, reason):
        gross = sum(qq * (px - entry) * sgn for qq, px in legs)
        cost = sum(abs(entry) * qq * COST + abs(px) * qq * COST for qq, px in legs)
        return {"net_r": (gross - cost) / r, "exit_ms": exit_ms, "reason": reason, "reached_1r": reached_1r}

    for b in bars_after(symbol, sent_at):
        o, h, lo, c = (float(b[k]) for k in ("open", "high", "low", "close"))
        close_ms = int(b["timestamp"]) + H
        hit_target = (h >= target) if side == "long" else (lo <= target)
        if target_first and hit_target:
            legs.append((q, target))
            return done(close_ms, "target_2r")
        fill = gap_aware_stop_fill(side=side, stop_f=cur, open_px=o, low=lo, high=h)
        if fill is not None:
            legs.append((q, float(fill)))
            return done(close_ms, "stop")
        best_hi = h if best_hi is None or h > best_hi else best_hi
        best_lo = lo if best_lo is None or lo < best_lo else best_lo
        fav = (h >= one_r) if side == "long" else (lo <= one_r)
        if fav:
            reached_1r = True
        if variant in ("B", "C") and not scaled and fav:
            legs.append((0.5, one_r))
            q = 0.5
            scaled = True
            if variant == "B":
                cur = entry
                be_fixed = True
        if not be_fixed:
            pos = {"side": side, "entry_price": entry, "initial_stop_price": stop, "stop_price": cur}
            tr = trail_stop_with_source(pos, best_low=best_lo, best_high=best_hi)
            if tr is not None:
                cur = float(tr[0])
        if hit_target:
            legs.append((q, target))
            return done(close_ms, "target_2r")
        reason = _analysis_exit_reason(conn, symbol=symbol, side=side, asof_ms=close_ms)
        if reason is not None:
            legs.append((q, c))
            return done(close_ms, reason)
    return None


def check_tool():
    rows = conn.execute(
        "SELECT symbol, side, entry, stop, qty, sent_at_ms, exit_at_ms, exit_px, net_r, funding_usdt "
        "FROM assist_track WHERE exit_at_ms IS NOT NULL"
    ).fetchall()
    worst = 0.0
    for t in rows:
        t = dict(t)
        sim = simulate(t["symbol"], t["side"], float(t["entry"]), float(t["stop"]), int(t["sent_at_ms"]), "A")
        loss = abs(float(t["entry"]) - float(t["stop"])) * float(t["qty"])
        stored = float(t["net_r"]) + (float(t["funding_usdt"] or 0) / loss if loss else 0)
        got = sim["net_r"] if sim else None
        diff = None if got is None else got - stored
        worst = max(worst, abs(diff) if diff is not None else 9)
        print(f"  {t['symbol']} {t['side']} stored {stored:+.3f} sim {got} diff {diff}")
    print(f"tool check: n={len(rows)} worst |diff|={worst:.3f} -> {'PASS' if worst <= 0.02 else 'FAIL'}")


def entries():
    sig = conn.execute(
        "SELECT symbol, timestamp, action, confidence_score, invalidation_price FROM analysis_output "
        "WHERE timeframe='4h' AND timestamp >= ? AND timestamp < ? "
        "AND rowid IN (SELECT MAX(rowid) FROM analysis_output WHERE timeframe='4h' GROUP BY symbol, timestamp) "
        "ORDER BY timestamp",
        (W0, W1),
    ).fetchall()
    busy_until = {s: 0 for s in ASSIST_SYMBOLS}
    out, still_open = [], 0
    for s in sig:
        s = dict(s)
        sym = s["symbol"]
        if sym not in ASSIST_SYMBOLS or not passes_signal_floor(s):
            continue
        entry_bar = int(s["timestamp"]) + ANALYSIS_LAG_MS
        if entry_bar < busy_until[sym]:
            continue
        b = conn.execute(
            "SELECT open FROM market_technicals WHERE symbol=? AND timeframe='1h' AND timestamp=?", (sym, entry_bar)
        ).fetchone()
        if b is None:
            continue
        side = str(s["action"]).lower()
        entry = float(b["open"])
        stop = float(s["invalidation_price"])
        if (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
            continue
        sent_at = entry_bar + 25 * 60_000
        a = simulate(sym, side, entry, stop, sent_at, "A")
        if a is None:
            still_open += 1
            busy_until[sym] = 10**15
            continue
        busy_until[sym] = a["exit_ms"]
        out.append({"symbol": sym, "side": side, "entry": entry, "stop": stop, "sent_at": sent_at})
    return out, still_open


def max_dd(results):
    seq = sorted(results, key=lambda x: x["exit_ms"])
    peak = cum = dd = 0.0
    for x in seq:
        cum += x["net_r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def run(trades, target_first=False):
    res = {v: [] for v in "ABC"}
    for t in trades:
        for v in "ABC":
            res[v].append(simulate(t["symbol"], t["side"], t["entry"], t["stop"], t["sent_at"], v, target_first))
    keep = [i for i in range(len(trades)) if all(res[v][i] is not None for v in "ABC")]
    res = {v: [res[v][i] for i in keep] for v in "ABC"}
    n = len(keep)
    print(f"n={n}")
    for v in "ABC":
        rs = [x["net_r"] for x in res[v]]
        print(f"  {v}: mean {st.mean(rs):+.3f}R sum {sum(rs):+.2f}R win {sum(r > 0 for r in rs) / n:.0%} maxDD {max_dd(res[v]):.2f}R")
    hit1 = [x for x in res["A"] if x["reached_1r"]]
    to2 = [x for x in hit1 if x["reason"] == "target_2r"]
    print(f"  reached +1R: {len(hit1)}/{n}; of those hit 2R under A: {len(to2)}")
    for v in "BC":
        d = [res[v][i]["net_r"] - res["A"][i]["net_r"] for i in range(n)]
        m, sd = st.mean(d), (st.stdev(d) if n > 1 else 0.0)
        se = sd / math.sqrt(n) if n > 1 else float("inf")
        need = (2 * sd / 0.10) ** 2 if sd else 0
        ok = n >= 30 and m >= 0.10 and m > 2 * se and max_dd(res[v]) <= max_dd(res["A"])
        print(f"  {v}-A: mean {m:+.3f}R SE {se:.3f} SD {sd:.3f} n_required~{need:.0f} -> {'PASS' if ok else 'no change'}")


print("== measuring-tool check (variant A vs stored assist_track)")
check_tool()
trades, still_open = entries()
print(f"\n== decision cell: entries {len(trades)} (still open at data end, excluded: {still_open})")
run(trades)
print("\n== sensitivity: target before stop in the same bar")
run(trades, target_first=True)
```
