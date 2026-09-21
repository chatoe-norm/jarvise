#!/usr/bin/env python3
"""Index Jarvise doctrine into Qdrant (compat wrapper).

Prefer: jarvise rag index
Paper/RAG only — no order placement.
"""

from __future__ import annotations

import argparse
import json
import sys

from jarvise import rag


def main() -> int:
    parser = argparse.ArgumentParser(description="Index Jarvise doctrine into Qdrant")
    parser.add_argument("--qdrant-url", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--skip-index", action="store_true")
    args = parser.parse_args()
    result = rag.index_sources(
        qdrant_url=args.qdrant_url,
        query=args.query,
        skip_index=args.skip_index,
    )
    if not result.get("ok"):
        print(result.get("error") or result, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.query else f"indexed {result.get('chunks')} chunks into {result.get('collection')}")
    if args.query:
        for hit in result.get("hits") or []:
            print(f"score={hit['score']:.3f} source={hit.get('source')}")
            print(hit.get("text"))
            print("---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
