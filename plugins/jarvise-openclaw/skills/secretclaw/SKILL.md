---
name: secretclaw
description: Secure credential and secret boundary injection — Ensures API keys are injected via protected environment mounts and never revealed in chats or logs.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# SecretClaw Security Boundary

Enforces least-privilege credential security across Jarvise & OpenClaw:
1. **Never Type Keys in Chat:** API keys and private secrets must never be passed through conversation prompts or chat sessions.
2. **Read-Only Privilege Default:** Exchange keys loaded into `.env` must be configured strictly with `Read` permission. `Withdraw` and `Internal Transfer` must remain strictly disabled.
3. **Protected OS Keystore / Environment Injection:** Keys are injected via secure system environment variables (`.env` bind mounts on VPS) with restrictive file permissions (`chmod 600`).
