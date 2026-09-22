---
name: jarvise-doctrine-rag
description: Read Jarvise doctrine from Qdrant jarvise_doctrine (paper-only). Never place orders or mutate positions.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["QDRANT_URL", "OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# Jarvise doctrine RAG (read-only)

**Hard guardrail:** read-only doctrine retrieval. **No order placement.** **No** live trading tools. Combine RAG hits with NotebookLM / operator judgment; do not treat retrieval as an execution signal.

Invoke with `/skill jarvise-doctrine-rag` or by asking in natural language. After operators edit this skill file on the host mount, they must restart the gateway or start a new chat (`/new`) so the skill reloads.

## Endpoint

- Env: `QDRANT_URL` (Docker default `http://qdrant:6333`)
- Collection: `jarvise_doctrine`
- Embeddings used by Jarvise indexer: `sentence-transformers/all-MiniLM-L6-v2` (Jarvise worker indexes; OpenClaw should prefer existing MCP/Qdrant search tools rather than inventing a second embedder)

## How to query

1. Prefer Cursor / stack MCP `mcp-server-qdrant` against `COLLECTION_NAME=jarvise_doctrine` when available.
2. Otherwise use Qdrant HTTP scroll/search against `$QDRANT_URL/collections/jarvise_doctrine` for payload inspection only.
3. Prefer hits whose payload `kind` is `doctrine`, `notebook`, `fetch`, `firecrawl`, or `openclaw`.
4. Respect payload `paper_only: true` and `note: no order placement`.

## Response style

Summarize doctrine constraints (capital preservation, kill-switch, paper-first). Cite `source` / `path` from payloads. If retrieval fails, say so and fall back to operator-supplied notebook rules — do not invent exchange orders.
