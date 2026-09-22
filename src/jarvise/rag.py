"""RAG ingest helpers — NotebookLM, OpenClaw, fetch, Firecrawl → local sources → Qdrant.

Paper/doctrine only. No order placement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

COLLECTION = "jarvise_doctrine"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_SOURCES_CONFIG = Path("config/rag-sources.json")
OPENCLAW_EXPORT_REL = Path("data/openclaw/exports")
OPENCLAW_SOURCES_REL = Path("data/analytics/sources/openclaw")
SOURCE_KINDS = ("notebook", "fetch", "firecrawl", "openclaw")


def repo_root() -> Path:
    env = os.environ.get("JARVISE_ROOT")
    if env:
        return Path(env).resolve()
    # src/jarvise/rag.py → parents[2] = repo root
    return Path(__file__).resolve().parents[2]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sources_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or (repo_root() / DEFAULT_SOURCES_CONFIG)
    if not cfg_path.is_file():
        return {"version": 1, "fetch": [], "firecrawl": [], "notebook": {}}
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    return value.strip("-")[:80] or "source"


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


def _write_markdown(path: Path, body: str, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "\n".join(f"<!-- {k}: {v} -->" for k, v in meta.items())
    path.write_text(f"{header}\n\n{body.strip()}\n", encoding="utf-8")


def sync_notebook(
    *,
    profile: str | None = None,
    alias: str | None = None,
    export_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Export NotebookLM sources via nlm CLI into data/analytics/sources/notebook/."""
    root = repo_root()
    cfg = load_sources_config()
    nb = cfg.get("notebook") or {}
    profile = profile or os.environ.get("NLM_PROFILE") or nb.get("nlm_profile") or "chatoe"
    alias = alias or nb.get("alias") or "jarvise"
    out = export_dir or root / (nb.get("export_dir") or "data/analytics/sources/notebook")
    out = out if out.is_absolute() else root / out

    notebook_json = root / "config" / "notebook.json"
    notebook_id = None
    if notebook_json.is_file():
        notebook_id = json.loads(notebook_json.read_text(encoding="utf-8")).get("notebook_id")

    result: dict[str, Any] = {
        "ok": True,
        "paper_only": True,
        "profile": profile,
        "alias": alias,
        "export_dir": str(out),
        "method": None,
        "files": [],
    }

    if dry_run:
        result["dry_run"] = True
        result["would"] = "nlm notebook export / source list → markdown under export_dir"
        return result

    out.mkdir(parents=True, exist_ok=True)
    # Try several nlm invocation shapes; record best-effort status.
    attempts = [
        ["nlm", "notebook", "export", alias, "--profile", profile, "--out", str(out)],
        ["nlm", "source", "list", alias, "--profile", profile],
    ]
    if notebook_id:
        attempts.insert(
            0,
            ["nlm", "notebook", "export", notebook_id, "--profile", profile, "--out", str(out)],
        )

    last_err = None
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        except FileNotFoundError:
            last_err = "nlm CLI not found on PATH"
            break
        except subprocess.TimeoutExpired:
            last_err = f"timeout: {' '.join(cmd)}"
            continue
        if proc.returncode == 0:
            result["method"] = " ".join(cmd)
            result["stdout_tail"] = (proc.stdout or "")[-2000:]
            # If list-only, write a snapshot file
            if "source" in cmd and "list" in cmd and proc.stdout:
                snap = out / f"notebook-sources-{slugify(alias)}.md"
                _write_markdown(
                    snap,
                    proc.stdout,
                    {
                        "kind": "notebook",
                        "alias": alias,
                        "ingested_at": utc_now_iso(),
                        "paper_only": True,
                    },
                )
                result["files"].append(str(snap))
            else:
                result["files"] = [str(p) for p in sorted(out.rglob("*")) if p.is_file()]
            return result
        last_err = (proc.stderr or proc.stdout or f"exit {proc.returncode}")[-500:]

    # Fallback stub so pipeline remains operable without nlm on CI/dev
    stub = out / "README.md"
    if not stub.exists():
        _write_markdown(
            stub,
            (
                "# NotebookLM export placeholder\n\n"
                "Run `nlm login --profile chatoe` on the VPS, then "
                "`jarvise rag sync-notebook`.\n"
            ),
            {"kind": "notebook", "ingested_at": utc_now_iso(), "paper_only": True},
        )
    result["ok"] = False
    result["error"] = last_err or "nlm export failed"
    result["files"] = [str(stub)] if stub.exists() else []
    return result


def ingest_fetch(entries: list[dict[str, Any]] | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    root = repo_root()
    cfg = load_sources_config()
    entries = entries if entries is not None else list(cfg.get("fetch") or [])
    out_dir = root / "data" / "analytics" / "sources" / "fetch"
    written: list[str] = []
    errors: list[str] = []

    if dry_run:
        return {"ok": True, "dry_run": True, "count": len(entries), "urls": [e.get("url") for e in entries]}

    for entry in entries:
        url = entry.get("url")
        if not url:
            continue
        sid = slugify(str(entry.get("id") or urlparse(url).netloc))
        dest = out_dir / f"{sid}.md"
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": "jarvise-rag/0.1 (+paper-only)"})
                resp.raise_for_status()
                body = resp.text
            _write_markdown(
                dest,
                body[:500_000],
                {
                    "kind": "fetch",
                    "url": url,
                    "source": sid,
                    "ingested_at": utc_now_iso(),
                    "paper_only": True,
                },
            )
            written.append(str(dest))
        except Exception as exc:  # noqa: BLE001 — collect per-URL errors
            errors.append(f"{url}: {exc}")

    return {
        "ok": not errors,
        "paper_only": True,
        "written": written,
        "errors": errors,
    }


def sync_openclaw(
    *,
    export_dir: Path | None = None,
    dest_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy OpenClaw export notes into data/analytics/sources/openclaw/ for RAG."""
    root = repo_root()
    src = export_dir or root / OPENCLAW_EXPORT_REL
    src = src if src.is_absolute() else root / src
    dest = dest_dir or root / OPENCLAW_SOURCES_REL
    dest = dest if dest.is_absolute() else root / dest

    candidates: list[Path] = []
    if src.is_dir():
        for path in sorted(src.iterdir()):
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".markdown"}:
                candidates.append(path)

    result: dict[str, Any] = {
        "ok": True,
        "paper_only": True,
        "export_dir": str(src),
        "dest_dir": str(dest),
        "candidates": len(candidates),
        "written": [],
        "skipped": [],
    }

    if dry_run:
        result["dry_run"] = True
        result["would_copy"] = [p.name for p in candidates]
        return result

    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    skipped: list[str] = []
    for path in candidates:
        body = path.read_text(encoding="utf-8", errors="replace").strip()
        if not body:
            skipped.append(path.name)
            continue
        sid = slugify(path.stem)
        suffix = ".md" if path.suffix.lower() in {".md", ".markdown"} else ".txt"
        out = dest / f"{sid}{suffix}"
        _write_markdown(
            out,
            body,
            {
                "kind": "openclaw",
                "source": sid,
                "origin": path.name,
                "ingested_at": utc_now_iso(),
                "paper_only": True,
                "jarvise": "kind=openclaw paper_only=true",
            },
        )
        written.append(str(out))

    result["written"] = written
    result["skipped"] = skipped
    return result


def ingest_firecrawl(
    entries: list[dict[str, Any]] | None = None,
    *,
    api_key: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = repo_root()
    cfg = load_sources_config()
    entries = entries if entries is not None else list(cfg.get("firecrawl") or [])
    key = api_key or os.environ.get("FIRECRAWL_API_KEY") or ""
    out_dir = root / "data" / "analytics" / "sources" / "firecrawl"
    written: list[str] = []
    errors: list[str] = []

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "count": len(entries),
            "has_api_key": bool(key),
            "urls": [e.get("url") for e in entries],
        }

    if not key:
        return {
            "ok": False,
            "paper_only": True,
            "written": [],
            "errors": ["FIRECRAWL_API_KEY not set; skipped firecrawl ingest"],
        }

    endpoint = os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v1/scrape")
    for entry in entries:
        url = entry.get("url")
        if not url:
            continue
        sid = slugify(str(entry.get("id") or urlparse(url).netloc))
        dest = out_dir / f"{sid}.md"
        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"url": url, "formats": ["markdown"]},
                )
                resp.raise_for_status()
                payload = resp.json()
            data = payload.get("data") or payload
            md = data.get("markdown") or data.get("content") or json.dumps(payload)[:200_000]
            _write_markdown(
                dest,
                md,
                {
                    "kind": "firecrawl",
                    "url": url,
                    "source": sid,
                    "ingested_at": utc_now_iso(),
                    "paper_only": True,
                },
            )
            written.append(str(dest))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")

    return {"ok": not errors, "paper_only": True, "written": written, "errors": errors}


def collect_source_files(root: Path | None = None) -> list[Path]:
    root = root or repo_root()
    bases = [
        root / "data" / "analytics" / "sources",
        root / "data" / "analytics",
    ]
    files: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".txt", ".md", ".markdown"}:
                files.append(path)
    # Prefer unique paths
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in files:
        rp = path.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        unique.append(path)
    return unique


def index_sources(
    *,
    qdrant_url: str | None = None,
    collection: str = COLLECTION,
    query: str | None = None,
    skip_index: bool = False,
) -> dict[str, Any]:
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        return {
            "ok": False,
            "error": f"RAG deps missing ({exc}). Install with: pip install -e '.[rag]'",
        }

    url = qdrant_url or os.environ.get("QDRANT_URL", "http://localhost:6333")
    client = QdrantClient(url=url, timeout=120, check_compatibility=False)
    model = SentenceTransformer(MODEL_NAME)
    dim = int(model.get_embedding_dimension())
    root = repo_root()
    result: dict[str, Any] = {
        "ok": True,
        "paper_only": True,
        "qdrant_url": url,
        "collection": collection,
        "chunks": 0,
    }

    if not skip_index:
        pending: list[dict[str, Any]] = []
        for path in collect_source_files(root):
            kind = "doctrine"
            for part in SOURCE_KINDS:
                if part in path.parts:
                    kind = part
                    break
            raw = path.read_text(encoding="utf-8", errors="replace")
            body = re.sub(r"<!--.*?-->\n?", "", raw, flags=re.DOTALL).strip()
            url_meta = None
            m = re.search(r"<!--\s*url:\s*(.*?)\s*-->", raw)
            if m:
                url_meta = m.group(1)
            rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            for idx, chunk in enumerate(chunk_text(body)):
                pending.append(
                    {
                        "id": int(
                            hashlib.sha256(f"{path.as_posix()}:{idx}:{chunk[:40]}".encode()).hexdigest()[:15],
                            16,
                        ),
                        "text": chunk,
                        "source": path.name,
                        "path": rel,
                        "kind": kind,
                        "url": url_meta,
                    }
                )
        vectors = (
            model.encode([item["text"] for item in pending], batch_size=32, show_progress_bar=False)
            if pending
            else []
        )
        ingested_at = utc_now_iso()
        points = [
            qm.PointStruct(
                id=item["id"],
                vector=vec.tolist(),
                payload={
                    "text": item["text"],
                    "source": item["source"],
                    "path": item["path"],
                    "kind": item["kind"],
                    "url": item["url"],
                    "ingested_at": ingested_at,
                    "paper_only": True,
                    "note": "no order placement",
                },
            )
            for item, vec in zip(pending, vectors, strict=True)
        ]
        if client.collection_exists(collection):
            client.delete_collection(collection)
        client.create_collection(
            collection_name=collection,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        )
        batch = 64
        for start in range(0, len(points), batch):
            client.upsert(collection_name=collection, points=points[start : start + batch])
        result["chunks"] = len(points)

    if query:
        qvec = model.encode(query).tolist()
        hits = client.query_points(collection_name=collection, query=qvec, limit=3)
        result["hits"] = [
            {
                "score": hit.score,
                "source": (hit.payload or {}).get("source"),
                "text": ((hit.payload or {}).get("text") or "")[:240],
            }
            for hit in hits.points
        ]
    return result


def publish_redis_status(key: str, payload: dict[str, Any]) -> None:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return
    try:
        import redis
    except ImportError:
        return
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.set(key, json.dumps(payload))
