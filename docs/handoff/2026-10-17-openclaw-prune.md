# OpenClaw news digest fix and plugin prune — 2026-10-17

Done by Claude Code on the VPS with owner approval. Read this before changing OpenClaw.

## What was wrong

The scheduled `Jarvise news ingest` (n8n `jrvsNewsIngest0719`) had not produced a digest since `2026-10-14-13.md`. Three faults stacked:

1. **n8n timeout 30 s.** The HTTP node gave up after 30 s (`ECONNABORTED`). Fixed in PR #187 (`timeout: 660000`, guarded by `tests/test_n8n_workflows.py`).
2. **Hook token placeholder.** Importing `infra/n8n/workflows/jarvise-news-ingest.json` directly loads the literal `Bearer __OPENCLAW_HOOK_TOKEN__`, so OpenClaw answers 401. This file must be rendered first: `infra/docker/seed-openclaw-ingest-agent.sh` (or the same replace step) substitutes the token from `data/openclaw/hook-token`. Re-imported rendered on 2026-10-17; temp files deleted.
3. **Agent admission timeout.** Every agent start stalled the gateway event loop for 35–70 s while about 50 plugins loaded, so `/hooks/agent` returned `503 hook agent run did not start before admission timeout` (~45 s). A restart did not help.

## What changed on the VPS (`/opt/jarvise/data/openclaw`)

- **Kept enabled (8):** `openrouter` (primary model), `google` (Gemini fallback), `openai` (openai-compatible embeddings for memory), `memory-lancedb` (memory slot), `firecrawl`, `perplexity`, `web-readability`, `document-extract`.
- **Disabled (37 bundled, `plugins.entries.<id>.enabled = false`):** alibaba, anthropic, azure-speech, browser, canvas, clawrouter, copilot-proxy, cua-computer, deepgram, device-pair, diagnostics-otel, elevenlabs, fal, file-transfer, geolocation, github, github-copilot, huggingface, kie, linux-node, litellm, lmstudio, memory-wiki, microsoft, microsoft-foundry, minimax, nvidia, ollama, opencode-go, runway, senseaudio, sglang, talk-voice, together, tts-local-cli, vllm, xai.
- **Uninstalled (5 global, ~760 MB):** acpx, diffs, diffs-language-pack, lobster, tokenjuice. Their config entries were removed.
- **Removed skill:** `workspace/skills/trade-bot` (third-party, `license: Proprietary`, crypto trading bot; it was being skipped for invalid frontmatter). Not fixed on purpose.
- `openclaw plugins registry --refresh` run after the uninstall.
- Agents, models, hooks, `identity` objects, compose limits, and the image pin were not changed.

Backups (root-only): `/opt/jarvise/backups/openclaw-prune-2026-10-17/` holds `openclaw.json` (before), `extensions-global.tgz`, `skill-trade-bot.tgz`, and `skills-removed/trade-bot`. `data/openclaw/openclaw.json.bak-pre-plugin-prune` is the same pre-change config. To roll back: stop openclaw, restore `openclaw.json` and untar the extensions into `data/openclaw/`, start openclaw.

## Result

- Hook test after the prune: `200 {"ok":true}` in 11.8 s (was 503 after 42–46 s).
- New digest `2026-10-17-15.md` written at 15:20 UTC, 7 sourced items, ends with "Not a trade signal".
- No `trade-bot` warnings after restart.

## Still open

- **MCP `jarvise-qdrant` fails to start** (`/home/node/.openclaw/bin/qdrant-mcp`, `MCP error -32000: Connection closed`), 6 times in 72 h, before this change too. Check whether the tactics/doctrine retrieval path depends on it.
- Gateway still logs a 33–35 s liveness delay at startup and memory-pressure warnings near the 1.59 GiB RSS threshold. Agent admission now fits inside the limit.
- AGENTS.md should say that `jarvise-news-ingest.json` is imported only in rendered form.
