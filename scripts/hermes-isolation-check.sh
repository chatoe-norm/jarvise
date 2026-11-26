#!/bin/sh
# Local Docker Desktop isolation proof for Hermes Stage A (R3/R4/R1).
# Uses a throwaway project name and volume paths under the worktree. Never the VPS.
# Requires Docker. Synthetic credentials only.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT="jarvise-hermes-iso-$$"
GATEWAY_DATA="${ROOT}/tmp-hermes-iso/gateway"
ADAPTER_DATA="${ROOT}/tmp-hermes-iso/adapter"
PILOT_DATA="${ROOT}/tmp-hermes-iso/pilot"
ENV_FILE="${ROOT}/tmp-hermes-iso/.env"

cleanup() {
  docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" --profile hermes down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

rm -rf "${ROOT}/tmp-hermes-iso"
mkdir -p "${GATEWAY_DATA}" "${ADAPTER_DATA}" "${PILOT_DATA}"

# Synthetic secrets — never production values.
cat >"${ENV_FILE}" <<'EOF'
JARVISE_HERMES_API_KEY=iso-api-key-0123456789abcdef
JARVISE_HERMES_INGEST_TOKEN=iso-ingest-token-0123456789
HERMES_PROVIDER=openrouter
HERMES_MODEL=anthropic/claude-sonnet-5.5
OPENROUTER_API_KEY=
JARVISE_UID=1000
JARVISE_GID=1000
EOF

# Point compose data dirs at throwaway paths via bind overrides.
cat >"${ROOT}/tmp-hermes-iso/compose.override.yml" <<EOF
services:
  hermes:
    volumes:
      - ${GATEWAY_DATA}:/opt/data
      - ${ROOT}/config/hermes/plugins:/opt/jarvise/hermes-plugins:ro
      - ${ROOT}/config/hermes/config.yaml.template:/opt/jarvise/hermes-config.yaml.template:ro
      - ${ROOT}/infra/docker/init-hermes-plugins.sh:/opt/jarvise/init-hermes-plugins.sh:ro
      - ${ROOT}/infra/docker/hermes-entrypoint.sh:/opt/jarvise/hermes-entrypoint.sh:ro
  hermes-ingest:
    volumes:
      - ${ADAPTER_DATA}:/data/hermes/adapter
      - ${PILOT_DATA}:/data/hermes/pilot-digests
      - ${ROOT}/config/hermes:/config/hermes:ro
EOF

cp "${ROOT}/config/hermes/config.yaml.template" "${GATEWAY_DATA}/config.yaml"

echo "[iso] building egress + ingest images"
docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml --profile hermes build hermes-egress hermes-ingest

echo "[iso] cold start"
docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml --profile hermes up -d hermes-egress hermes

echo "[iso] wait health inside container"
ok=0
i=0
while [ "$i" -lt 60 ]; do
  i=$((i + 1))
  if docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml exec -T hermes \
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8642/health', timeout=5)" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 5
done
if [ "$ok" != 1 ]; then
  echo "[iso] FAIL: gateway health never became ready" >&2
  docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml logs --tail=80 hermes >&2 || true
  exit 1
fi
echo "[iso] health OK (cold)"

echo "[iso] UID check"
uid_line="$(docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml exec -T hermes id hermes)"
echo "${uid_line}" | grep -q 'uid=10000' || {
  echo "[iso] FAIL: expected uid=10000, got ${uid_line}" >&2
  exit 1
}
echo "[iso] hermes UID 10000 OK"

echo "[iso] restart"
docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml restart hermes
ok=0
i=0
while [ "$i" -lt 40 ]; do
  i=$((i + 1))
  if docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml exec -T hermes \
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8642/health', timeout=5)" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 5
done
if [ "$ok" != 1 ]; then
  echo "[iso] FAIL: health after restart" >&2
  exit 1
fi
echo "[iso] health OK (restart)"

echo "[iso] mcp-serve tools/list"
# MCP initialize + tools/list over stdio
tools_json="$(
  docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
    exec -T --user hermes hermes hermes mcp-serve --tools-only <<'MCP'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"jarvise-iso","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
MCP
)"
echo "${tools_json}" | tee "${ROOT}/tmp-hermes-iso/tools-list.json" >/dev/null
echo "${tools_json}" | grep -qi '"tools"' || {
  echo "[iso] FAIL: tools/list missing tools key" >&2
  echo "${tools_json}" >&2
  exit 1
}
# Fail if forbidden tool families appear in the name list.
forbidden='message|approval|conversation|shell|terminal|file_|write_file|exec|delegat|order|image_gen|vision|tts|audio|permission'
if echo "${tools_json}" | grep -Eio "\"name\"[[:space:]]*:[[:space:]]*\"[^\"]*(${forbidden})[^\"]*\"" >/dev/null; then
  echo "[iso] FAIL: forbidden tool names present" >&2
  echo "${tools_json}" >&2
  exit 1
fi
echo "[iso] tools/list OK (no forbidden families)"

echo "[iso] network: hermes must not have hermes_egress"
nets="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$(docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml ps -q hermes)")"
echo "hermes nets: ${nets}"
echo "${nets}" | grep -q hermes_egress && {
  echo "[iso] FAIL: hermes attached to hermes_egress" >&2
  exit 1
}
echo "${nets}" | grep -q hermes_net || {
  echo "[iso] FAIL: hermes missing hermes_net" >&2
  exit 1
}
echo "${nets}" | grep -q hermes_api || {
  echo "[iso] FAIL: hermes missing hermes_api" >&2
  exit 1
}
echo "[iso] PASS"
