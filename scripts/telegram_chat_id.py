#!/usr/bin/env python3
"""Resolve Telegram chat id from bot token (getUpdates).

Usage:
  # Read TELEGRAM_BOT_TOKEN from .env / env, print chat ids
  python scripts/telegram_chat_id.py

  # Wait until you Start the bot / send a message (up to N seconds)
  python scripts/telegram_chat_id.py --wait 60

  # Write first private chat id into .env as TELEGRAM_CHAT_ID
  python scripts/telegram_chat_id.py --wait 60 --write

  # Explicit token
  python scripts/telegram_chat_id.py --token '123:ABC'
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = REPO_ROOT / ".env"
API = "https://api.telegram.org"


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, val = raw.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        out[key] = val
    return out


def _api(token: str, method: str) -> dict:
    url = f"{API}/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            import json

            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Telegram HTTP {exc.code}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Telegram request failed: {exc}") from exc


def _chats_from_updates(payload: dict) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for update in payload.get("result") or []:
        for key in ("message", "edited_message", "my_chat_member", "channel_post"):
            block = update.get(key)
            if not isinstance(block, dict):
                continue
            chat = block.get("chat") or {}
            cid = chat.get("id")
            if cid is None or cid in seen:
                continue
            seen.add(int(cid))
            label = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
            found.append((int(cid), str(chat.get("type") or ""), str(label)))
    return found


def _write_env_chat_id(path: Path, chat_id: int) -> None:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    line = f"TELEGRAM_CHAT_ID={chat_id}"
    if re.search(r"^TELEGRAM_CHAT_ID=.*$", text, flags=re.M):
        text = re.sub(r"^TELEGRAM_CHAT_ID=.*$", line, text, count=1, flags=re.M)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", help="Bot token (default: TELEGRAM_BOT_TOKEN)")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help="Path to .env")
    parser.add_argument(
        "--wait",
        type=int,
        default=0,
        metavar="SEC",
        help="Poll getUpdates until a chat appears (seconds)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write first private chat id into .env as TELEGRAM_CHAT_ID",
    )
    args = parser.parse_args()

    dotenv = _load_dotenv(args.env)
    token = (
        args.token or os.environ.get("TELEGRAM_BOT_TOKEN") or dotenv.get("TELEGRAM_BOT_TOKEN") or ""
    ).strip()
    if not token:
        print("Missing TELEGRAM_BOT_TOKEN (.env / env / --token)", file=sys.stderr)
        return 2

    me = _api(token, "getMe")
    if not me.get("ok"):
        print(f"getMe failed: {me.get('description')}", file=sys.stderr)
        return 1
    username = (me.get("result") or {}).get("username") or "?"
    print(f"bot ok: @{username}")
    print(f"1) Open Telegram → @{username} → Start / send any message")
    print("2) Waiting for getUpdates…" if args.wait else "2) Fetching getUpdates…")

    deadline = time.time() + max(0, args.wait)
    chats: list[tuple[int, str, str]] = []
    while True:
        updates = _api(token, "getUpdates")
        if not updates.get("ok"):
            print(f"getUpdates failed: {updates.get('description')}", file=sys.stderr)
            return 1
        chats = _chats_from_updates(updates)
        if chats or time.time() >= deadline:
            break
        time.sleep(2)

    if not chats:
        print("No chats yet. Start the bot in Telegram, then re-run (or use --wait 60).")
        return 1

    print("chats:")
    for cid, ctype, label in chats:
        print(f"  {cid}\ttype={ctype}\t{label}")

    private = next((c for c in chats if c[1] == "private"), chats[0])
    chosen = private[0]
    print(f"suggested TELEGRAM_CHAT_ID={chosen}")

    if args.write:
        _write_env_chat_id(args.env, chosen)
        print(f"wrote TELEGRAM_CHAT_ID to {args.env}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
