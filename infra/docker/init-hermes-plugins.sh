#!/bin/sh
# Idempotent: restore vendored mcp-serve into HERMES_HOME and enable it.
# Never clones GitHub. Tree hash (not PIN text alone) decides whether to re-copy.
# MCP probes run as uid hermes. Existing config.yaml is only stripped of the
# managed skills toolset entry — model/provider/credentials are never overwritten.
set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"
VENDOR="${HERMES_PLUGIN_VENDOR:-/opt/jarvise/hermes-plugins/mcp-serve}"
TARGET="${HERMES_HOME}/plugins/mcp-serve"
PIN_VENDOR="${VENDOR}/PIN"
HASH_STAMP="${HERMES_HOME}/plugins/.mcp-serve.treehash"

if [ ! -f "${PIN_VENDOR}" ]; then
  echo "[hermes-init] missing vendored PIN at ${PIN_VENDOR}" >&2
  exit 1
fi

tree_hash() {
  # Stable content fingerprint of the plugin tree (paths + file digests).
  (cd "$1" && find . -type f ! -name '*.pyc' | LC_ALL=C sort | while IFS= read -r f; do
    sha256sum "$f" 2>/dev/null || shasum -a 256 "$f"
  done | sha256sum | awk '{print $1}')
}

VENDOR_HASH="$(tree_hash "${VENDOR}")"
NEED_COPY=1
if [ -f "${HASH_STAMP}" ] && [ -d "${TARGET}" ]; then
  CURRENT="$(cat "${HASH_STAMP}")"
  if [ "${CURRENT}" = "${VENDOR_HASH}" ]; then
    NEED_COPY=0
  fi
fi

if [ "${NEED_COPY}" = 1 ]; then
  echo "[hermes-init] restoring mcp-serve from read-only vendor (hash ${VENDOR_HASH})"
  mkdir -p "${HERMES_HOME}/plugins" "${HERMES_HOME}/installs"
  rm -rf "${TARGET}"
  cp -a "${VENDOR}" "${TARGET}"
  printf '%s\n' "${VENDOR_HASH}" >"${HASH_STAMP}"
fi

strip_skills_toolset() {
  # Managed non-secret edit only: remove list items that are exactly 'skills'
  # under platform_toolsets. Does not touch model/provider/credential keys.
  python3 - <<'PY'
import os
import re
import sys
from pathlib import Path

path = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "config.yaml"
if not path.is_file():
    sys.exit(0)
text = path.read_text(encoding="utf-8")
if "platform_toolsets" not in text:
    sys.exit(0)
# Refuse opaque / unreadable shapes that still mention skills.
if re.search(r"skills\s*:", text) and "platform_toolsets" in text:
    # mapping form skills: ... is unexpected here; fail closed.
    print("[hermes-init] unexpected skills mapping in config.yaml; refusing", file=sys.stderr)
    sys.exit(1)
new_lines: list[str] = []
changed = False
in_platform = False
for line in text.splitlines(keepends=True):
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    if re.match(r"^platform_toolsets\s*:", line):
        in_platform = True
        new_lines.append(line)
        continue
    if in_platform and indent == 0 and stripped and not stripped.startswith("#"):
        in_platform = False
    if in_platform and re.match(r"^-\s*skills\s*(#.*)?$", stripped):
        changed = True
        continue
    new_lines.append(line)
out = "".join(new_lines)
if changed:
    path.write_text(out, encoding="utf-8")
    print("[hermes-init] stripped skills from platform_toolsets")
elif re.search(r"(?m)^\s*-\s*skills\s*$", text):
    print("[hermes-init] skills list entry present but not under platform_toolsets; refusing", file=sys.stderr)
    sys.exit(1)
PY
}

if [ ! -f "${HERMES_HOME}/config.yaml" ]; then
  if [ -f /opt/jarvise/hermes-config.yaml.template ]; then
    cp /opt/jarvise/hermes-config.yaml.template "${HERMES_HOME}/config.yaml"
  else
    printf '%s\n' \
      'model:' \
      '  default: ""' \
      '  provider: ""' \
      'fallback_providers: []' \
      'platform_toolsets:' \
      '  api_server: [web]' \
      '  cli: [web]' \
      'plugins:' \
      '  enabled:' \
      '    - mcp-serve' \
      >"${HERMES_HOME}/config.yaml"
  fi
else
  export HERMES_HOME
  strip_skills_toolset
fi

mkdir -p "${HERMES_HOME}/installs"

run_as_hermes() {
  if [ "$(id -u)" = 0 ] && id hermes >/dev/null 2>&1; then
    if command -v s6-setuidgid >/dev/null 2>&1; then
      s6-setuidgid hermes env HERMES_HOME="${HERMES_HOME}" "$@"
    else
      su -s /bin/sh hermes -c "HERMES_HOME='${HERMES_HOME}' $(printf '%q ' "$@")"
    fi
  else
    HERMES_HOME="${HERMES_HOME}" "$@"
  fi
}

# Ensure hermes owns the plugin tree before probes (writable-home enable path).
if [ "$(id -u)" = 0 ] && id hermes >/dev/null 2>&1; then
  chown -R hermes:hermes "${HERMES_HOME}/plugins" "${HERMES_HOME}/installs" 2>/dev/null || true
  chown hermes:hermes "${HERMES_HOME}/config.yaml" 2>/dev/null || true
fi

if run_as_hermes hermes mcp-serve --help >/dev/null 2>&1; then
  echo "[hermes-init] mcp-serve already enabled (probe as hermes)"
  exit 0
fi

echo "[hermes-init] enabling mcp-serve (no tool override)"
# Enable may need to write installs/; run as root then re-chown, or as hermes if writable.
if [ "$(id -u)" = 0 ]; then
  HERMES_HOME="${HERMES_HOME}" hermes plugins enable mcp-serve --no-allow-tool-override </dev/null
  chown -R hermes:hermes "${HERMES_HOME}/plugins" "${HERMES_HOME}/installs" "${HERMES_HOME}/config.yaml" 2>/dev/null || true
else
  run_as_hermes hermes plugins enable mcp-serve --no-allow-tool-override </dev/null
fi
if ! run_as_hermes hermes mcp-serve --help >/dev/null 2>&1; then
  echo "[hermes-init] mcp-serve enable failed" >&2
  exit 1
fi
echo "[hermes-init] mcp-serve enabled"
