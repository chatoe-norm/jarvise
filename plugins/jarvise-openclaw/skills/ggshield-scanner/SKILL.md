---
name: ggshield-scanner
description: Git Secret Leak Prevention — Pre-commit and pre-push automated scan against hardcoded tokens, API keys, and sensitive environment variables.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# GGShield Secret Scanner

Guards repository integrity by scanning all modified files before staging and committing:
- Flags OpenAI, Anthropic, OpenRouter, and Binance API key patterns.
- Ensures `.env`, `secrets.json`, and local credential stores remain excluded via `.gitignore`.
- Fails closed on detection to prevent accidental pushes to public remotes.
