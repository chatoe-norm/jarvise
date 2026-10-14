# Notebook rules mapped onto the 13 Oct 2026 paper tape

**Research overlay — not trading doctrine.** Do not index this page into Qdrant `jarvise_doctrine`. Do not silent-merge it into SQLite or the paper ledger. Do not retune analyzer floors **0.55 / 0.70**, do not change `same_side_hold`, and do not set `JARVISE_LIVE_TRADING=true`.

Queried **2026-10-13** via Gemini Notebook MCP `notebook_query` on [Jarvise : Crypto Trader](https://notebook.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e) (`14e11c63-e2ee-4b49-898f-b0cc4c61cb4e`, alias `jarvise`, `nlm` profile `chatoe`). Conversation `236d4069-b07d-4cd2-b3ec-388e974165d0`.

`nlm login switch chatoe` then `jarvise rag sync-notebook` (profile `chatoe`). Current `nlm` has no `notebook export` command, so sync recorded `nlm source list jarvise --profile chatoe`. Sources already on disk under `data/analytics/sources/notebook/` stayed there; a source-list snapshot is `notebook-sources-jarvise.md` (catalog only, not an indexable doctrine extract). `jarvise rag index` then embedded the allowlisted Jarvise notes into local Qdrant `jarvise_doctrine`: **80** chunks, **30** with `kind=notebook` (core protocol, analyzer stack, expectancy/stops doctrine, crypto-trader note) and **50** `kind=doctrine`, including the existing pullback extract. That index is the local collection at `127.0.0.1:6333`. It does not replace the VPS collection.

---

## What the notebook actually says

Owner protocol already on file: [`data/analytics/sources/jarvise-doctrine-pullback-entry.txt`](../../data/analytics/sources/jarvise-doctrine-pullback-entry.txt) (notebook source `3c37e0b7-86d5-4576-bf25-01ee8ed2f4f3`). This sync does not add a second entry tactic.

**Long location (the only written EMA20 location rule).** Paper `approve_with_caution` only when all of these hold: regime `trend_up` (EMA20 > EMA200), action `long`, confidence at or above the **0.55** FLAT gate, shallow pullback (close below EMA20 by less than about **0.5× ATR**, one wick, not a breakdown), RSI about **45–70**. RSI below **30** is a momentum conflict and stays FLAT. A close well below EMA20 is a deep break and is refused. Price extended far above EMA20 is a chase, not this setup. **0.70** stays the doctrine-free auto-approve bar. The note does not change analyzer scores.

**Structure before oscillators.** Core protocol (`2f474ccb-ea2b-4653-8173-c94ded3586cd`) and the expectancy/stops note (`bf161c52-0fc1-4f63-a81c-f4c2e7b2a006`): read trend versus range, swings, and support/resistance first. RSI, MACD, and similar oscillators are context. Never execute on an oscillator alone. Capital preservation beats activity. Prefer FLAT over a low-confidence guess. Cut size in unclear regimes. Every trade needs a written invalidation. No add after a stop.

**Stops.** ATR stop no closer than **1.5×** the current volatility range (long: entry − 1.5× ATR; short: entry + 1.5× ATR). Risk per idea stays in the **0.25%–1%** equity band. The pullback note keeps the long stop at least **1.5× ATR** below entry and keeps the analyzer’s reduced size.

**Higher timeframe.** When MTF is on, HTF must confirm the same trend. HTF range, chaotic, or in conflict with the 4h bias forces FLAT. There is no pullback exception.

The notebook does **not** state a short-side twin of the EMA20 pullback rule (no `trend_down` RSI band, no “sell the rally into a falling EMA20” protocol). Shorts are covered only by the general rules: structure first, no isolated oscillator, stop at least 1.5× ATR, FLAT when the regime is unclear.

---

## Map onto the 4h tape (13 Oct 2026)

Paper book that day was about **9974** from a **10000** start. The closed-trade loss was mostly **longs that stopped** (BNB, ETH, SOL, BTC), not a missing strategy library. Those longs are the case the pullback extract already describes: a `trend_up` long is a caution entry only on a shallow EMA20 dip with RSI still in **45–70**. A deep close through EMA20, or a chase far above it, is a refuse. The extract already says the stop stays at least **1.5× ATR** and that a stopped idea is not re-added. Nothing in this sync changes that file or the stop distance in code.

Open paper shorts the same day were **ETHUSDT** and **SOLUSDT** (`auto_claude`), with 4h RSI roughly **35–50**, while **BTC** (and BNB) were `range` and FLAT near confidence **0.4**. The notebook’s long RSI band **45–70** does not license those shorts. Mid-range RSI without a written short location rule is an oscillator-as-context reading, and the core protocol says not to trade an oscillator by itself. BTC in `range` is the unclear higher-timeframe regime the same notes tell the book to treat as FLAT **when MTF is on**.

`JARVISE_ANALYZE_MTF` is still **false** on the VPS, so that HTF block is not applied on the 4h pass. Turning it back on is outside this note. The existing gate still stands: wait until about **10** closed `auto_*` outcomes, with stops counted in expectancy, before restoring MTF or touching floors.

On 2026-10-13 the higher-timeframe block also could not see BTC or ETH daily structure. Those 1d series had **209** bars, so EMA200 was withheld and `analyze_mtf` would have treated the higher timeframe as chaotic and forced FLAT. BNB and SOL daily history was already long enough. BTC and ETH were backfilled from 2021-01-01 (**2111** bars, 0 gaps). After that, the latest daily candle reads BTC `trend_up` long at confidence **0.65** (close 81850, EMA200 about 75530) and ETH `trend_up` but FLAT at confidence **0.50**. MTF remains off.

`same_side_hold` (one position per symbol, no pyramiding) is the capital-preservation rule the notebook’s “no add after a stop / no revenge add” language already matches. This note does not change it.

---

## What this pass does not do

No analyzer formula change, no floor change, no `same_side_hold` change, no live flag, no compose change, and no new doctrine file. Freqtrade, NautilusTrader, Hummingbot, ccxt, and generic web strategies stay research overlays and are not imported into `jarvise_doctrine`. The local `stop_hit` expectancy change is a separate measurement fix and is not part of this sync.
