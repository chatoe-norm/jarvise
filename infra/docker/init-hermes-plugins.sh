#!/bin/sh
# Idempotent: copy vendored mcp-serve into HERMES_HOME and enable it when the PIN changes.
# Copies only the vendored tree (never clones GitHub). First enable may resolve
# plugin Python deps via HTTPS_PROXY (hermes-egress); later starts are a no-op when PIN matches.
set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"
VENDOR="${HERMES_PLUGIN_VENDOR:-/opt/jarvise/hermes-plugins/mcp-serve}"
TARGET="${HERMES_HOME}/plugins/mcp-serve"
PIN_VENDOR="${VENDOR}/PIN"
PIN_TARGET="${TARGET}/PIN"

if [ ! -f "${PIN_VENDOR}" ]; then
  echo "[hermes-init] missing vendored PIN at ${PIN_VENDOR}" >&2
  exit 1
fi

need_copy=0
if [ ! -f "${PIN_TARGET}" ]; then
  need_copy=1
elif ! cmp -s "${PIN_VENDOR}" "${PIN_TARGET}"; then
  need_copy=1
fi

if [ "${need_copy}" = 1 ]; then
  echo "[hermes-init] installing mcp-serve from local vendor pin"
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
      'plugins:' \
      '  enabled:' \
      '    - mcp-serve' \
      >"${HERMES_HOME}/config.yaml"
  fi
fi

mkdir -p "${HERMES_HOME}/installs"

# Fast path: command already registered for this home.
if HERMES_HOME="${HERMES_HOME}" hermes mcp-serve --help >/dev/null 2>&1; then
  echo "[hermes-init] mcp-serve already enabled"
  # Ensure hermes can read what root may have written.
  if [ "$(id -u)" = 0 ] && id hermes >/dev/null 2>&1; then
    chown -R hermes:hermes "${HERMES_HOME}/plugins" "${HERMES_HOME}/installs" 2>/dev/null || true
  fi
  exit 0
fi

echo "[hermes-init] enabling mcp-serve (no tool override)"
HERMES_HOME="${HERMES_HOME}" hermes plugins enable mcp-serve --no-allow-tool-override </dev/null
if ! HERMES_HOME="${HERMES_HOME}" hermes mcp-serve --help >/dev/null 2>&1; then
  echo "[hermes-init] mcp-serve enable failed" >&2
  exit 1
fi
if [ "$(id -u)" = 0 ] && id hermes >/dev/null 2>&1; then
  chown -R hermes:hermes "${HERMES_HOME}/plugins" "${HERMES_HOME}/installs" "${HERMES_HOME}/config.yaml" 2>/dev/null || true
fi
echo "[hermes-init] mcp-serve enabled"
