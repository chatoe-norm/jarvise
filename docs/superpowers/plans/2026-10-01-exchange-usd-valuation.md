# Exchange USD valuation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Per-asset + total ~USD on `/analytics` exchange panel via Binance public USDT prices.

**Architecture:** `jarvise_exchange.value` + public ticker GET; web panel consumes; soft-fail unchanged.

**Tech Stack:** Python, httpx, pytest, FastAPI HTML panel

## File map

| Path | Responsibility |
|------|----------------|
| `src/jarvise_exchange/value.py` | Stables, price fetch, `value_spot_balances` |
| `src/jarvise_exchange/models.py` | `ValuedBalance` / result dataclass |
| `src/jarvise_web/app.py` | Exchange card ~USD columns + total |
| `tests/test_exchange_value.py` | Unit tests with mocked HTTP |

## Tasks

1. Models + `value_spot_balances` (TDD)
2. Public ticker helper (mock HTTP)
3. Wire `_exchange_panel_html`
4. Docs / PROJECT_CONTEXT note for §5.8a
