"""Export Jarvise NotebookLM sources into data/analytics/sources/notebook/.

Local only. Output is gitignored. Caps each source so the VPS index stays usable.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "analytics" / "sources" / "notebook"
NOTEBOOK = "jarvise"
PROFILE = "chatoe"
MAX_CHARS = 24_000


def slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return text[:80] or "source"


def run_nlm(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["nlm", *args, "--profile", PROFILE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    listed = run_nlm(["source", "list", NOTEBOOK, "--json"])
    if listed.returncode != 0 or not listed.stdout:
        print(listed.stderr or listed.stdout or "source list failed")
        return 1
    sources = json.loads(listed.stdout)
    (OUT / "sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    catalog = ["# Jarvise notebook sources", "", f"Count: {len(sources)}", ""]
    ok = 0
    failed = 0
    for source in sources:
        title = str(source.get("title") or source["id"])
        url = source.get("url") or ""
        kind = source.get("type") or ""
        catalog.append(f"- {title} ({kind}) {url}".rstrip())
        dest = OUT / f"{slug(title)}-{source['id'][:8]}.md"
        if dest.is_file() and dest.stat().st_size > 80:
            ok += 1
            continue
        fetched = run_nlm(["source", "content", source["id"], "--json"])
        if fetched.returncode != 0 or not fetched.stdout:
            failed += 1
            print(f"FAIL {title}: {(fetched.stderr or fetched.stdout or '')[-200:]}")
            continue
        payload = json.loads(fetched.stdout)
        body = str(payload.get("content") or "")
        truncated = len(body) > MAX_CHARS
        if truncated:
            body = body[:MAX_CHARS] + "\n\n[truncated for RAG index]\n"
        text = (
            f"<!-- kind: notebook -->\n"
            f"<!-- title: {title} -->\n"
            f"<!-- url: {url} -->\n"
            f"<!-- source_id: {source['id']} -->\n\n"
            f"# {title}\n\n"
            f"{body}\n"
        )
        dest.write_text(text, encoding="utf-8")
        ok += 1
        print(f"OK {dest.name} chars={len(body)} truncated={truncated}")
    (OUT / "catalog.md").write_text("\n".join(catalog) + "\n", encoding="utf-8")
    print(f"done ok={ok} failed={failed} total={len(sources)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
