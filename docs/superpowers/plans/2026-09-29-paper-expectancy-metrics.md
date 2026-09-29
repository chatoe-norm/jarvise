# Paper expectancy metrics — Implementation Plan

> **For agentic workers:** TDD; commit when authorized.

**Goal:** Compute paper expectancy from round-trips; expose via CLI + `/analytics` equally.

**Spec:** [docs/superpowers/specs/2026-09-29-paper-expectancy-metrics-design.md](../specs/2026-09-29-paper-expectancy-metrics-design.md)

## File map

| Path | Responsibility |
|------|----------------|
| `src/jarvise_paper/metrics.py` | `compute_paper_metrics`, `persist_metrics_snapshot` |
| `src/jarvise_ingest/db.py` | `list_paper_orders_asc` (all fills chronological) |
| `tests/test_paper_metrics.py` | Round-trip / EV / Sharpe gate tests |
| `src/jarvise_paper/cli.py` | `paper metrics [--json] [--persist]` |
| `src/jarvise_web/app.py` | card + GET/POST metrics API |
| `tests/test_web.py` / `tests/test_paper_cli.py` | Surface tests |
| docs + `PROJECT_CONTEXT.md` | handoff |

## Tasks

### Task 1: metrics module + tests
### Task 2: CLI `paper metrics`
### Task 3: web card + API
### Task 4: docs / PROJECT_CONTEXT §5.3
