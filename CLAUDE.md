# Claude Code in Jarvise

`AGENTS.md` and `PROJECT_CONTEXT.md` are the source of truth for rules and status. This file only sets how Claude works here. Do not repeat their content in replies or new files.

## Role

Claude reviews, audits, and synthesizes. Cursor (Grok) implements and ships. The owner pays per Claude context, so spend it on judgment, not on writing large diffs or waiting for CI.

Claude does itself:

- Read-only audits: ledger and replay analysis on a DB copy, code reading, root cause.
- Review of a diff or PR, and decisions the owner asks for.
- A fix of a few lines when finding the bug was the hard part, with its regression test.
- Research notes in `docs/research/` and Thai summaries for the owner.

Claude hands to Cursor:

- Features, refactors, multi-file edits, `web/` UI work.
- `fdpy` / `wdpy`: commit, PR, CI and Deploy waits, healthz check.
- Re-running the full check suite once the change is settled.

## Handoff prompt

When work goes to Cursor, end the reply with one paste-ready prompt in a `text` code block, in English:

1. **Read first:** exact files and sections (for example `AGENTS.md`, the research note §2.1), not "read the repo".
2. **Context:** what is already done and verified, with file paths and the numbers that matter. Cursor has no memory of this session.
3. **Task:** numbered steps. For each change, name the file, the behaviour, and what must stay unchanged.
4. **Checks:** `.venv\Scripts\ruff.exe check src tests`, `ruff format --check src tests`, `.venv\Scripts\python.exe -m mypy src`, `.venv\Scripts\python.exe -m pytest`; add `cd web; npm run lint; npm test; npm run build` only if `web/` changed. State the expected result.
5. **Ship:** `fdpy` steps, or "open a PR, do not merge" when the owner still has to decide.
6. **Hard rules:** live off, VPS `.env` untouched, floors 0.55/0.70, analyzer formula, 1.5× ATR stop, `flat_exit`, `same_side_hold`, universe, plus anything specific to the task.
7. **Report:** what Cursor sends back (PR URL, Deploy result, numbers).

A task that touches a locked area goes in a separate prompt marked "owner approval required", so the owner approves by pasting it. Never put secrets, `.env` values, or tokens in a prompt.

## Replay and strategy research

- Write the decision rule into the research note before running: window, one decision cell, pass criteria. Other cells are sensitivity only.
- Look for features in-sample only. Run out-of-sample only for a frozen rule that already passed in-sample. If nothing passes, say so and stop searching.
- Work on a copy of `data/analytics/jarvise-vps-ledger-ro.db`. Never write the original or the VPS ledger. Scratch scripts go in the session scratchpad, not the repo.
- Check the measuring tool before trusting a result. PR #179 found replay stops reading bars from after the fill.

## Context economy

- Grep, then read the needed range. Do not read whole large files.
- After a small fix, run the touched test files. The full suite goes in the Cursor prompt.
- Replies to the owner are in Thai and short: findings, numbers, the decision needed, then the prompt.
