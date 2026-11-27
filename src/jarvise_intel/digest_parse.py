"""Stdlib-only digest Markdown item parser for the Hermes adapter image (R1).

No third-party imports. Downstream ``parse_digest_text`` in jarvise_context
adds theme scores on top of this shared split.
"""

from __future__ import annotations

import re
from typing import Any

_ITEM_SPLIT = re.compile(r"\n(?=\d+\.\s+\*\*)")
# Group 1 = bold title; group 2 = rest of title line (often " — YYYY-MM-DD"); group 3 = body.
_ITEM_HEAD = re.compile(r"^\d+\.\s+\*\*(.*?)\*\*([^\n]*)\n(.*)", re.DOTALL)
_DAY_ON_TITLE = re.compile(r"\s*[—–-]\s*(\d{4}-\d{2}-\d{2})\s*.*$")
_FOOTER_PREFIX = "Not a trade signal"


def parse_digest_items(content: str) -> list[dict[str, Any]]:
    """Parse numbered digest items into title/url/comment (and day when present)."""
    if not content or not content.strip():
        return []
    blocks = _ITEM_SPLIT.split(content)
    items: list[dict[str, Any]] = []
    for block in blocks:
        match = _ITEM_HEAD.match(block.strip())
        if not match:
            continue
        raw_title = match.group(1).strip()
        title_suffix = match.group(2) or ""
        day = ""
        day_match = _DAY_ON_TITLE.search(raw_title) or _DAY_ON_TITLE.search(title_suffix)
        if day_match:
            day = day_match.group(1)
        title = _DAY_ON_TITLE.sub("", raw_title).strip()
        body = match.group(3).strip()
        url = ""
        comment_lines: list[str] = []
        for line in body.split("\n"):
            line = line.strip()
            if not line or line.startswith(_FOOTER_PREFIX):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                url = line
            else:
                comment_lines.append(line)
        item: dict[str, Any] = {
            "title": title,
            "url": url,
            "comment": " ".join(comment_lines),
        }
        if day:
            item["day"] = day
        items.append(item)
    return items
