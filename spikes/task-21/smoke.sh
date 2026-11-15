#!/usr/bin/env bash
# Smoke test of the hagent app image, without HA OS and without a Supervisor.
#
# Steps:
#  1. Package the app folder (scripts/package-app.sh).
#  2. Build the image for linux/amd64, as the Supervisor builds it.
#  3. Start FakeHA in a container from the same image.
#  4. Start the app with s6 (the normal entry point) in the network namespace
#     of FakeHA. /data is a Docker volume with an options.json in the shape
#     that the Supervisor writes.
#  5. Check the migrations, the UI, the API and the link.
#  6. Simulate an HA restart and check that the app connects again.
#  7. Restart the app container and check that the data stays.
#  8. Stop the app and check the clean exit.
#
# Usage: spikes/task-21/smoke.sh   (needs Docker; no live HA, no API keys)
# Set KEEP=1 to keep the containers and the volume for inspection.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image="${IMAGE:-local/hagent:smoke}"
ha="hagent-smoke-ha"
app="hagent-smoke-app"
volume="hagent-smoke-data"
work="$(mktemp -d)"

cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    docker rm -f "${app}" "${ha}" >/dev/null 2>&1 || true
    docker volume rm "${volume}" >/dev/null 2>&1 || true
  fi
  rm -rf "${work}"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL $*"; docker logs "${app}" 2>&1 | tail -40; exit 1; }

# Wait until the app log holds the line $1 at least $2 times.
wait_log() {
  local pattern="$1" count="${2:-1}"
  for _ in $(seq 1 90); do
    if [[ "$(docker logs "${app}" 2>&1 | grep -c -- "${pattern}")" -ge "${count}" ]]; then
      return 0
    fi
    sleep 1
  done
  fail "no log line '${pattern}' (${count} times)"
}

step "package and build (linux/amd64)"
"${root}/scripts/package-app.sh" "${work}" >/dev/null
docker build --platform linux/amd64 -q -t "${image}" "${work}/hagent"

docker rm -f "${app}" "${ha}" >/dev/null 2>&1 || true
docker volume rm "${volume}" >/dev/null 2>&1 || true

step "start FakeHA"
docker run -d --name "${ha}" --platform linux/amd64 \
  -e PYTHONPATH=/smoke \
  -v "${root}/app/tests/helpers:/smoke/helpers:ro" \
  -v "${root}/spikes/task-21:/smoke/bin:ro" \
  --entrypoint python "${image}" /smoke/bin/fake_ha_server.py >/dev/null

step "write options.json"
# The Supervisor writes /data/options.json from config.yaml. "ingress" is not
# in the app schema: the smoke test adds it, because its requests come from
# 127.0.0.1 and not from the Supervisor (172.30.32.2).
docker volume create "${volume}" >/dev/null
docker run --rm -i -v "${volume}:/data" --entrypoint sh "${image}" \
  -c 'cat > /data/options.json' <<'JSON'
{"log_level": "info",
 "providers": {"genai_prices_auto_update": false,
               "refresh_openrouter_prices": false},
 "link": {"discovery": true},
 "ingress": {"allowed_addresses": ["127.0.0.1"]}}
JSON

step "start the app (s6)"
# SUPERVISOR_TOKEN is the token variable that the Supervisor sets. FakeHA
# accepts "test-token". HAGENT_HA_URL replaces ws://supervisor/core/websocket.
docker run -d --name "${app}" --platform linux/amd64 \
  --network "container:${ha}" \
  -e SUPERVISOR_TOKEN=test-token \
  -e HAGENT_HA_URL=ws://127.0.0.1:8123/api/websocket \
  -e TZ=Europe/London \
  -v "${volume}:/data" "${image}" >/dev/null
wait_log "connected to HA"
wait_log "started"

step "migrations"
docker exec "${app}" python -c '
import sqlite3
db = sqlite3.connect("/data/hagent.db")
n = db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
mode = db.execute("PRAGMA journal_mode").fetchone()[0]
print(f"PASS migrations applied={n} journal_mode={mode}")
assert n > 0
' || fail "migrations"
docker exec "${app}" ls /data

step "UI, API and link"
docker exec -e PYTHONPATH=/smoke "${ha}" python /smoke/bin/probe.py || fail "probe"

step "HA restart"
docker kill -s USR1 "${ha}" >/dev/null
wait_log "connected to HA" 2
echo "PASS the app connected to HA again"

step "app restart keeps the data"
id1="$(docker logs "${app}" 2>&1 | grep -o 'hagent installation [^ ]*' | head -1)"
docker restart "${app}" >/dev/null
wait_log "hagent installation" 2
id2="$(docker logs "${app}" 2>&1 | grep -o 'hagent installation [^ ]*' | tail -1)"
[[ -n "${id1}" && "${id1}" == "${id2}" ]] || fail "installation ID changed"
echo "PASS same ${id2}"
docker exec -e PYTHONPATH=/smoke "${ha}" python /smoke/bin/probe.py || fail "probe"

step "clean stop"
docker stop "${app}" >/dev/null
code="$(docker inspect -f '{{.State.ExitCode}}' "${app}")"
docker logs "${app}" 2>&1 | tail -5
[[ "${code}" == "0" ]] || fail "exit code ${code}"
echo "PASS exit code 0"

echo
echo "smoke test passed"
