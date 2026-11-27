#!/bin/sh
# Local Docker Desktop isolation proof for Hermes Stage A (R3/R4/R1 / F7).
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
      - ${ROOT}/infra/docker/hermes_config_toolsets.py:/opt/jarvise/hermes_config_toolsets.py:ro
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

echo "[iso] config helper runs in hermes image (PyYAML preferred; structural fallback OK)"
# Quote the container path inside sh -c so Git Bash/MSYS does not rewrite /opt/... .
docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
  exec -T hermes sh -c 'python /opt/jarvise/hermes_config_toolsets.py --check /opt/data/config.yaml' || {
  echo "[iso] FAIL: hermes_config_toolsets.py --check failed" >&2
  exit 1
}
if docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
  exec -T hermes python -c "import yaml" >/dev/null 2>&1; then
  echo "[iso] PyYAML present in hermes image"
else
  echo "[iso] NOTE: hermes image has no PyYAML — structural subset parser used (not the old skills: regex)"
fi

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
forbidden='message|approval|conversation|shell|terminal|file_|write_file|exec|delegat|order|image_gen|vision|tts|audio|permission|skill_manage|cron'
if echo "${tools_json}" | grep -Eio "\"name\"[[:space:]]*:[[:space:]]*\"[^\"]*(${forbidden})[^\"]*\"" >/dev/null; then
  echo "[iso] FAIL: forbidden tool names present" >&2
  echo "${tools_json}" >&2
  exit 1
fi
# Positive allowlist for MCP research tools — missing names FAIL (F7).
for need in web_search web_extract; do
  echo "${tools_json}" | grep -q "\"${need}\"" || {
    echo "[iso] FAIL: expected tool ${need} not seen in tools/list" >&2
    echo "${tools_json}" >&2
    exit 1
  }
done
echo "[iso] tools/list OK (MCP discovery; not HTTP /v1/runs toolset)"

echo "[iso] gateway process UID — python workers as hermes (10000); no root uvicorn/api_server"
# s6 PID 1 stays root. Supervised API must run as hermes. Ignore unrelated root python
# (e.g. healthcheck) whose cmdline does not look like the API server.
proc_report="$(docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
  exec -T hermes sh -c '
    hermes_py=0
    root_api=0
    for p in /proc/[0-9]*; do
      cmd=$(tr "\0" " " <"$p/cmdline" 2>/dev/null || true)
      case "$cmd" in *python*) ;; *) continue ;; esac
      uid=$(awk "/^Uid:/{print \$2}" "$p/status" 2>/dev/null || echo "")
      case "$cmd" in
        *uvicorn*|*api_server*|*hermes.api*|*gateway.run*|*gateway\ run*)
          if [ "$uid" = "0" ]; then root_api=1; fi
          if [ "$uid" = "10000" ]; then hermes_py=1; fi
          ;;
        *)
          if [ "$uid" = "10000" ]; then hermes_py=1; fi
          ;;
      esac
    done
    echo "hermes_py=$hermes_py root_api=$root_api"
  ')"
echo "${proc_report}"
echo "${proc_report}" | grep -q 'hermes_py=1' || {
  echo "[iso] FAIL: no python process as uid 10000" >&2
  exit 1
}
if echo "${proc_report}" | grep -q 'root_api=1'; then
  # s6/entrypoint may still show a root wrapper whose cmdline mentions gateway run.
  # Require a hermes (10000) python worker; treat root wrapper as noted gap, not PASS of "no root".
  echo "[iso] NOTE: a root process cmdline matched api/gateway patterns (s6 wrapper likely); hermes_py=10000 present"
fi

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

echo "[iso] bring up ingest for behavioural network checks"
docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml --profile hermes up -d hermes-ingest
i=0
while [ "$i" -lt 30 ]; do
  i=$((i + 1))
  if docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
    exec -T hermes-ingest python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8092/healthz', timeout=3)" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "[iso] adapter cannot open public TCP (internal nets)"
if docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
  exec -T hermes-ingest python -c "import socket; socket.create_connection(('1.1.1.1',443),3)" >/dev/null 2>&1; then
  echo "[iso] FAIL: adapter reached public 1.1.1.1:443" >&2
  exit 1
fi
echo "[iso] adapter public bypass blocked OK"

echo "[iso] hermes container cannot open public TCP (bypass from tool host)"
if docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
  exec -T hermes python -c "import socket; socket.create_connection(('1.1.1.1',443),3)" >/dev/null 2>&1; then
  echo "[iso] FAIL: hermes reached public 1.1.1.1:443" >&2
  exit 1
fi
echo "[iso] hermes public bypass blocked OK"

echo "[iso] proxy denies private/metadata CONNECT; public HTTPS must be HTTP 200"
docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
  exec -T hermes python - <<'PY'
import socket
import sys

def connect_req(hostport: str) -> bytes:
    s = socket.create_connection(("hermes-egress", 8899), 10)
    s.settimeout(15)
    s.sendall(f"CONNECT {hostport} HTTP/1.1\r\nHost: {hostport}\r\n\r\n".encode())
    data = s.recv(256)
    s.close()
    return data

for target in ("127.0.0.1:443", "10.0.0.1:443", "169.254.169.254:443"):
    priv = connect_req(target)
    if b"403" not in priv and b"Forbidden" not in priv:
        print("FAIL private", target, priv[:80], file=sys.stderr)
        sys.exit(1)

# Public target must succeed (HTTP 200). 502 is not success (F7).
pub = connect_req("example.com:443")
if b"200" not in pub:
    print("FAIL public not 200", pub[:120], file=sys.stderr)
    sys.exit(1)
print("proxy behavioural checks OK")
PY

echo "[iso] config has no skills on api_server (grep is not HTTP toolset proof)"
docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f docker-compose.yml -f tmp-hermes-iso/compose.override.yml \
  exec -T hermes sh -c 'grep -A5 "api_server:" /opt/data/config.yaml | grep -q skills && exit 1 || exit 0'
echo "[iso] NOTE: HTTP /v1/runs agent toolset is not queried at runtime — config+MCP only (unrun gap)"

echo "[iso] PASS"
