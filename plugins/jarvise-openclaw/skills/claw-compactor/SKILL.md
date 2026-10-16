---
name: claw-compactor
description: OpenClaw Session History Compactor — Automatically summarizes and compacts conversation sessions exceeding 300KB to reduce token costs and prevent context overflow.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# OpenClaw Session Compactor

Optimizes context usage and prevents context overflow for autonomous multi-tier agents.

## Triggers & Limits
- **Compaction Threshold:** Automatically triggers when session state / conversation history exceeds `300KB`.
- **Ephemerality:** System prompts referencing doctrine files (`jarvise-doctrine.txt`) use `cache_control: { "type": "ephemeral" }` to leverage prompt caching.
- **Summary Retention:** Retains key decisions, indicators, trade outcomes, and hard constraints (`1% risk`, `FLAT` exits) while compacting verbose intermediate turns.
