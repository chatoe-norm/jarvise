# Freqtrade pairlists → Jarvise static universe

**Research overlay — not trading doctrine.** Do not index this page, Freqtrade docs, or ccxt into Qdrant `jarvise_doctrine`. Do not add Freqtrade/ccxt/TA-Lib as dependencies. Do not loosen analyzer floors (0.55/0.70) or `same_side_hold`.

Queried **2026-10-05**. NotebookLM profile `chatoe`. Notebook [46cb8581](https://notebook.google.com/notebook/46cb8581-a191-4a90-a359-6e82dd1d4c04). Conversation `d482c676-2faf-4b6d-96e4-3ea282bbce05`.

Official sources used: [Freqtrade](https://www.freqtrade.io/en/stable/), [Configuration](https://www.freqtrade.io/en/stable/configuration/), Stoploss page (force_entry warning only).

---

## What Freqtrade actually does

| Piece | Docs |
|-------|------|
| `exchange.pair_whitelist` | Array or regex of pairs the bot may trade / backtest. **Not used by VolumePairList.** |
| `exchange.pair_blacklist` | Pairs the bot must never trade or backtest. |
| `pairlists` | Handler list. Default **`StaticPairList`** = the whitelist as written. |
| `VolumePairList` | Rank by volume/price. Does not use the whitelist. **Not available in backtesting.** |
| `max_open_trades` | Required cap on concurrent trades. **Only one open trade per pair.** Effective cap = min(max_open_trades, pairlist length). `-1` = unlimited except pairlist length. |
| `forcebuy` / `force_entry` | Telegram/REST inject a buy. Disabled by default; docs call it dangerous. |

---

## Steal vs skip for Jarvise

**Steal (StaticPairList only)**

1. A **named static list** of spot USDT symbols, separate from `paper_core`, plus **GET klines** for those symbols. That is Freqtrade’s whitelist, not their bot.
2. Keep **one position per symbol** (Jarvise already does this; Freqtrade states the same invariant).
3. Keep a **global open-position cap** (`JARVISE_MAX_OPEN_POSITIONS`, already there). Do not set it to `-1`. Do not raise it in this change just to grind sample size.

**Skip**

- `VolumePairList` / rotating “top volume” universes (not even valid in Freqtrade backtests).
- `forcebuy` / Telegram force-entry.
- Strategy plugins, pandas+TA-Lib, ccxt, importing Freqtrade.
- Regex whitelist, blacklist as a new product (not needed for four liquid spots).

Notebook suggested adding XRP too. **Out of scope:** members are BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT only.

---

## Mapping

| Freqtrade | Jarvise |
|-----------|---------|
| `pair_whitelist` + `StaticPairList` | Universe id `paper_liquid` in [`src/jarvise_ingest/universe.py`](../../src/jarvise_ingest/universe.py) |
| Download market data for those pairs | `JARVISE_INGEST_SYMBOLS` must include the same four; 4h SoT unchanged |
| `max_open_trades` | Existing `JARVISE_MAX_OPEN_POSITIONS` (default **2** — unchanged here) |
| One trade per pair | Existing `same_side_hold` + one paper position per symbol |

`paper_core` stays BTC+ETH from 2021. Opt in with `JARVISE_PAPER_UNIVERSE=paper_liquid`. Until VPS env changes, jobs still enqueue two symbols.

With cap still 2, four symbols help when a slot is free (e.g. ETH flat, SOL sets up). They do **not** mean four concurrent positions unless you later raise the cap on purpose.

Parent report: [`2026-10-05-autotrade-stack-gap.md`](2026-10-05-autotrade-stack-gap.md).
