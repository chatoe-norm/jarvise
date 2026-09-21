#!/usr/bin/env python3
"""Index Jarvise doctrine into Qdrant collection jarvise_doctrine.

Default embeddings: sentence-transformers/all-MiniLM-L6-v2 (local).
Paper/RAG only — no order placement.

Usage:
  .venv/bin/pip install -e '.[rag]'
  .venv/bin/python scripts/rag_index_doctrine.py
  .venv/bin/python scripts/rag_index_doctrine.py --query "expectancy FLAT"
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = [
    ROOT / "data" / "analytics" / "sources" / "jarvise-doctrine.txt",
    ROOT / "data" / "analytics" / "sources" / "jarvise-analyzer-stack.txt",
    ROOT / "data" / "analytics" / "stack.md",
]
COLLECTION = "jarvise_doctrine"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def chunk_text(text: str, size: int = 500, overlap: int = 80) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + size])
        i += max(size - overlap, 1)
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description="Index Jarvise doctrine into Qdrant")
    parser.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", "http://localhost:6333"))
    parser.add_argument("--query", default=None, help="Smoke-search after indexing")
    parser.add_argument("--skip-index", action="store_true")
    args = parser.parse_args()

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print(
            "Error: install RAG deps first.\n  .venv/bin/pip install -e '.[rag]'",
            file=sys.stderr,
        )
        return 2

    client = QdrantClient(url=args.qdrant_url, check_compatibility=False)
    model = SentenceTransformer(MODEL_NAME)
    dim = int(model.get_embedding_dimension())

    if not args.skip_index:
        points: list[qm.PointStruct] = []
        for path in DEFAULT_SOURCES:
            if not path.exists():
                print(f"skip missing: {path}")
                continue
            raw = path.read_text(encoding="utf-8")
            for idx, chunk in enumerate(chunk_text(raw)):
                pid = int(hashlib.sha256(f"{path.name}:{idx}:{chunk[:40]}".encode()).hexdigest()[:16], 16)
                vec = model.encode(chunk).tolist()
                points.append(
                    qm.PointStruct(
                        id=pid,
                        vector=vec,
                        payload={
                            "text": chunk,
                            "source": path.name,
                            "paper_only": True,
                            "note": "no order placement",
                        },
                    )
                )
        if client.collection_exists(COLLECTION):
            client.delete_collection(COLLECTION)
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        )
        if points:
            client.upsert(collection_name=COLLECTION, points=points)
        print(f"indexed {len(points)} chunks into {COLLECTION} @ {args.qdrant_url}")

    if args.query:
        qvec = model.encode(args.query).tolist()
        result = client.query_points(collection_name=COLLECTION, query=qvec, limit=3)
        for hit in result.points:
            text = (hit.payload or {}).get("text", "")
            print(f"score={hit.score:.3f} source={(hit.payload or {}).get('source')}")
            print(text[:240])
            print("---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
