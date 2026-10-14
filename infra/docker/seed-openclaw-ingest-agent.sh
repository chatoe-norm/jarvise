#!/usr/bin/env bash
# Copy the ingest-agent workspace templates and enable the n8n → OpenClaw hook.
# Does not write data/openclaw/exports/ and does not add unknown keys to openclaw.json.
# The hook token stays in the gitignored state dir. The committed workflow keeps a placeholder.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/config/openclaw/ingest"
DEST="$ROOT/data/openclaw/workspace-ingest"
CFG="$ROOT/data/openclaw/openclaw.json"
TOKEN_FILE="$ROOT/data/openclaw/hook-token"
WORKFLOW_SRC="$ROOT/infra/n8n/workflows/jarvise-news-ingest.json"
RENDERED="${TMPDIR:-/tmp}/jarvise-news-ingest.rendered.json"

mkdir -p "$DEST/digests"
cp "$SRC/AGENTS.md" "$DEST/AGENTS.md"
cp "$SRC/WATCHLIST.md" "$DEST/WATCHLIST.md"
# The gateway user is uid 1000. A root-owned workspace makes agents add reject IDENTITY.md.
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$(stat -c '%u:%g' "$ROOT/data/openclaw")" "$DEST"
fi
chmod 700 "$DEST" "$DEST/digests"
echo "workspace templates -> $DEST"
if [[ -e "$ROOT/data/openclaw/exports" ]]; then
  echo "left exports/ untouched"
fi

if [[ ! -f "$CFG" ]]; then
  echo "missing $CFG — run infra/docker/seed-openclaw-config.sh first" >&2
  exit 1
fi

if [[ ! -s "$TOKEN_FILE" ]]; then
  umask 077
  openssl rand -hex 32 > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "wrote hook token file (not printed)"
else
  chmod 600 "$TOKEN_FILE"
  echo "hook token file already present"
fi

python3 - "$CFG" "$TOKEN_FILE" <<'PY'
import json
import sys
from pathlib import Path

cfg_path, token_path = sys.argv[1:]
token = Path(token_path).read_text(encoding="utf-8").strip()
if len(token) < 32:
    raise SystemExit("hook token is too short")
cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
if "_jarvise" in cfg:
    raise SystemExit("refusing to keep undocumented _jarvise key")
hooks = cfg.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
allowed = hooks.get("allowedAgentIds")
if not isinstance(allowed, list):
    allowed = []
if "ingest" not in allowed:
    allowed.append("ingest")
hooks.update(
    {
        "enabled": True,
        "token": token,
        "path": "/hooks",
        "allowedAgentIds": allowed,
        "allowRequestSessionKey": False,
    }
)
cfg["hooks"] = hooks
Path(cfg_path).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
print("merged hooks.allowedAgentIds without printing the token")
PY

python3 - "$WORKFLOW_SRC" "$TOKEN_FILE" "$RENDERED" <<'PY'
import json
import sys
from pathlib import Path

src, token_path, dest = sys.argv[1:]
token = Path(token_path).read_text(encoding="utf-8").strip()
raw = Path(src).read_text(encoding="utf-8")
if "__OPENCLAW_HOOK_TOKEN__" not in raw:
    raise SystemExit("workflow template is missing the token placeholder")
rendered = raw.replace("__OPENCLAW_HOOK_TOKEN__", token)
workflow = json.loads(rendered)
blob = json.dumps(workflow)
if "__OPENCLAW_HOOK_TOKEN__" in blob:
    raise SystemExit("placeholder survived rendering")
if token not in blob:
    raise SystemExit("rendered workflow is missing the token")
Path(dest).write_text(rendered, encoding="utf-8")
print(f"rendered workflow -> {dest}")
PY
chmod 600 "$RENDERED"
if [[ "$(id -u)" -eq 0 ]]; then
  owner="$(stat -c '%u:%g' "$ROOT/data/openclaw")"
  chown "$owner" "$CFG" "$TOKEN_FILE"
  chown -R "$owner" "$DEST"
fi
echo "Import on the VPS with:"
echo "  docker cp $RENDERED jarvise-n8n-1:/tmp/jarvise-news-ingest.rendered.json"
echo "  docker exec jarvise-n8n-1 n8n import:workflow --input=/tmp/jarvise-news-ingest.rendered.json"
echo "  docker exec jarvise-n8n-1 n8n publish:workflow --id=jrvsNewsIngest0719"
echo "Then delete the rendered file. Restart OpenClaw only if /hooks/agent is not loaded yet."
