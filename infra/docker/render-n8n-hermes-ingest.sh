#!/usr/bin/env bash
# Render __JARVISE_HERMES_INGEST_TOKEN__ into a unique temp copy of jarvise-hermes-ingest.json.
# Does not print the token. Does not edit /opt/jarvise/.env. Does not publish/activate.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/infra/n8n/workflows/jarvise-hermes-ingest.json"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/jarvise-n8n-hermes.XXXXXX")"
chmod 700 "$OUT"
if [[ -z "${JARVISE_HERMES_INGEST_TOKEN:-}" ]]; then
  echo "JARVISE_HERMES_INGEST_TOKEN is unset; refusing to render" >&2
  exit 1
fi
if [[ "$JARVISE_HERMES_INGEST_TOKEN" == "__JARVISE_HERMES_INGEST_TOKEN__" ]]; then
  echo "JARVISE_HERMES_INGEST_TOKEN is still the placeholder; refusing to render" >&2
  exit 1
fi
if [[ ${#JARVISE_HERMES_INGEST_TOKEN} -lt 16 ]]; then
  echo "JARVISE_HERMES_INGEST_TOKEN is too short; refusing to render" >&2
  exit 1
fi
[[ -f "$SRC" ]] || { echo "missing $SRC" >&2; exit 1; }
python3 - "$SRC" "$OUT" <<'PY'
import json
import os
import sys
from pathlib import Path

src = Path(sys.argv[1])
out = Path(sys.argv[2])
token = os.environ.get("JARVISE_HERMES_INGEST_TOKEN", "")
if not token or token == "__JARVISE_HERMES_INGEST_TOKEN__":
    raise SystemExit("refusing to render without a real token")
raw = src.read_text(encoding="utf-8")
if "__JARVISE_HERMES_INGEST_TOKEN__" not in raw:
    raise SystemExit("placeholder missing in source workflow")
# JSON-escape the token so quotes/control chars cannot break the workflow body.
escaped = json.dumps(token)[1:-1]
rendered = raw.replace("__JARVISE_HERMES_INGEST_TOKEN__", escaped)
if "__JARVISE_HERMES_INGEST_TOKEN__" in rendered:
    raise SystemExit("render failed")
if "openclaw" in rendered.lower() and "hooks/agent" in rendered:
    raise SystemExit("refusing to render an OpenClaw workflow with this script")
dest = out / src.name
dest.write_text(rendered, encoding="utf-8")
os.chmod(dest, 0o600)
print(f"rendered {dest.name} under {out} (mode 0600)")
PY
echo "Keep the workflow inactive until Stage C cutover."
echo "Import-only commands (token not printed; do not publish/activate here):"
echo "  docker cp $OUT/jarvise-hermes-ingest.json jarvise-n8n-1:/tmp/jarvise-hermes-ingest.json"
echo "  docker exec jarvise-n8n-1 n8n import:workflow --input=/tmp/jarvise-hermes-ingest.json"
echo "  docker exec jarvise-n8n-1 rm -f /tmp/jarvise-hermes-ingest.json"
echo "Then: rm -rf $OUT"
echo "Publish/activation is Stage C only. Do not activate while jrvsNewsIngest0719 is canonical."
