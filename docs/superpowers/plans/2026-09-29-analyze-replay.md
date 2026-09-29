# Analyze replay — Implementation Plan

**Goal:** Walk stored closed candles; upsert analysis; optional isolated paper apply + metrics.

**Spec:** [docs/superpowers/specs/2026-09-29-analyze-replay-design.md](../specs/2026-09-29-analyze-replay-design.md)

## File map

| Path | Responsibility |
|------|----------------|
| `src/jarvise_ingest/db.py` | `load_candles_in_range`, `reset_paper_ledger` |
| `src/jarvise_analyze/replay.py` | `replay_range` |
| `src/jarvise_analyze/cli.py` | `--replay` flags + wiring |
| `tests/test_analyze_replay.py` | Core tests |
| docs + PROJECT_CONTEXT | handoff |

## Tasks

1. DB helpers + tests  
2. `replay_range` + tests  
3. CLI flags  
4. Docs / §5.4 checkbox  
