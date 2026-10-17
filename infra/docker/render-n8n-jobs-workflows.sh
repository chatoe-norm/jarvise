#!/usr/bin/env bash
# Render __JARVISE_JOBS_TOKEN__ from the environment into a temp directory.
# Does not print the token. Does not render jarvise-live-reconcile.json.
# Does not edit /opt/jarvise/.env or data/openclaw/openclaw.json.
# The VPS OpenClaw config still has mcp.servers.jarvise-redis until the owner
# removes that entry after recreating the container with docker-compose.prod.yml.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/infra/n8n/workflows"
OUT="${TMPDIR:-/tmp}/jarvise-n8n-jobs-rendered"
if [[ -z "${JARVISE_JOBS_TOKEN:-}" ]]; then
  echo "JARVISE_JOBS_TOKEN is unset; refusing to render" >&2
  exit 1
fi
if [[ "$JARVISE_JOBS_TOKEN" == "__JARVISE_JOBS_TOKEN__" ]]; then
  echo "JARVISE_JOBS_TOKEN is still the placeholder; refusing to render" >&2
  exit 1
fi
if [[ ${#JARVISE_JOBS_TOKEN} -lt 16 ]]; then
  echo "JARVISE_JOBS_TOKEN is too short; refusing to render" >&2
  exit 1
fi
rm -rf "$OUT"
mkdir -p "$OUT"
chmod 700 "$OUT"
python3 - "$SRC" "$OUT" <<'PY'
import json
import os
import sys
from pathlib import Path

src, out = (Path(p) for p in sys.argv[1:])
token = os.environ.get("JARVISE_JOBS_TOKEN", "")
if not token or token == "__JARVISE_JOBS_TOKEN__":
    raise SystemExit("refusing to render without a real token")
manifest = []
for path in sorted(src.glob("*.json")):
    if path.name == "jarvise-live-reconcile.json":
        continue
    raw = path.read_text(encoding="utf-8")
    if "jobs:8090" not in raw or "__JARVISE_JOBS_TOKEN__" not in raw:
        continue
    rendered = raw.replace("__JARVISE_JOBS_TOKEN__", token)
    if "__JARVISE_JOBS_TOKEN__" in rendered or token not in rendered:
        raise SystemExit(f"render failed for {path.name}")
    dest = out / path.name
    dest.write_text(rendered, encoding="utf-8")
    os.chmod(dest, 0o600)
    workflow_id = json.loads(rendered).get("id") or ""
    manifest.append(f"{path.name}\t{workflow_id}")
live = out / "jarvise-live-reconcile.json"
if live.exists():
    raise SystemExit("refusing to leave a rendered live-reconcile workflow")
(out / "manifest.tsv").write_text("\n".join(manifest) + "\n", encoding="utf-8")
print(f"rendered {len(manifest)} workflows")
PY
echo "Skipped jarvise-live-reconcile.json. Do not import or publish it while live trading is off."
echo "Import and publish the rendered files, then delete them:"
while IFS=$'\t' read -r name wid; do
  [[ -z "$name" ]] && continue
  echo "  docker cp $OUT/$name jarvise-n8n-1:/tmp/$name"
  echo "  docker exec jarvise-n8n-1 n8n import:workflow --input=/tmp/$name"
  if [[ -n "$wid" ]]; then
    echo "  docker exec jarvise-n8n-1 n8n publish:workflow --id=$wid"
  fi
  echo "  docker exec jarvise-n8n-1 rm -f /tmp/$name"
done < "$OUT/manifest.tsv"
echo "Then: rm -rf $OUT && docker restart jarvise-n8n-1"
echo "The VPS file data/openclaw/openclaw.json still has mcp.servers.jarvise-redis until the owner removes it."
