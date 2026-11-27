#!/bin/sh
# Idempotent: restore vendored mcp-serve into HERMES_HOME and enable it.
# Never clones GitHub. Installed tree hash vs read-only vendor decides re-copy (F5).
# MCP probes and plugins enable run as uid hermes. Existing config.yaml is validated
# structurally via hermes_config_toolsets.py — model/provider/credentials are never
# overwritten when already valid.
set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"
VENDOR="${HERMES_PLUGIN_VENDOR:-/opt/jarvise/hermes-plugins/mcp-serve}"
TARGET="${HERMES_HOME}/plugins/mcp-serve"
PIN_VENDOR="${VENDOR}/PIN"
CONFIG_HELPER="${HERMES_CONFIG_HELPER:-/opt/jarvise/hermes_config_toolsets.py}"

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
if [ -d "${TARGET}" ]; then
  # Compare installed tree to vendor — not a writable stamp (F5).
  CURRENT="$(tree_hash "${TARGET}")"
  if [ "${CURRENT}" = "${VENDOR_HASH}" ]; then
    NEED_COPY=0
  fi
fi

if [ "${NEED_COPY}" = 1 ]; then
  echo "[hermes-init] restoring mcp-serve from read-only vendor (hash ${VENDOR_HASH})"
  mkdir -p "${HERMES_HOME}/plugins" "${HERMES_HOME}/installs"
  rm -rf "${TARGET}"
  cp -a "${VENDOR}" "${TARGET}"
fi

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
  if [ ! -f "${CONFIG_HELPER}" ]; then
    echo "[hermes-init] missing config helper at ${CONFIG_HELPER}" >&2
    exit 1
  fi
  export HERMES_HOME
  python3 "${CONFIG_HELPER}" "${HERMES_HOME}/config.yaml"
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
# Prefer enable as hermes after chown (F5).
run_as_hermes hermes plugins enable mcp-serve --no-allow-tool-override </dev/null
if [ "$(id -u)" = 0 ] && id hermes >/dev/null 2>&1; then
  chown -R hermes:hermes "${HERMES_HOME}/plugins" "${HERMES_HOME}/installs" "${HERMES_HOME}/config.yaml" 2>/dev/null || true
fi
if ! run_as_hermes hermes mcp-serve --help >/dev/null 2>&1; then
  echo "[hermes-init] mcp-serve enable failed" >&2
  exit 1
fi
echo "[hermes-init] mcp-serve enabled"
