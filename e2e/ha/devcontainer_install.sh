#!/usr/bin/env bash
# Install and start Send to X3 as a *local app* in a real Home Assistant Supervisor.
#
# Runs Home Assistant's official apps devcontainer image (privileged, Docker in
# Docker, systemd) the way the apps-example .devcontainer.json does, starts the
# Supervisor on the stable channel, and lets that Supervisor build, install and
# start the app from a copy of this repository mounted as local apps. Checks:
#
#   1. the store lists local_send_to_x3; the Supervisor builds and installs it;
#   2. it starts; /data/options.json is what the Supervisor wrote;
#   3. GET /healthz on the published host port 8765 (the X3's path);
#   4. the admin UI asks for a token when reached directly by another container;
#   5. real ingress, browser -> Core -> Supervisor -> app, with an ingress session
#      created the way the frontend does: the web UI loads without an app token
#      and with ingress-relative links, the paste form POST creates an item, and a
#      20 MB upload gets through (ingress_stream; the Supervisor buffers only 16 MB);
#   6. a mobile_app.share event fired through the Supervisor's Core API proxy
#      becomes an item (the Companion app share-sheet path).
#
# Usage:
#   e2e/ha/devcontainer_install.sh          # run everything, then tear down
#   KEEP=1 e2e/ha/devcontainer_install.sh   # keep it (Home Assistant: http://127.0.0.1:7123)
#   e2e/ha/devcontainer_install.sh --down   # remove the container and its volumes
#
# Environment: WORK_DIR (scratch, default e2e/work/ha), SUPERVISOR_CHANNEL
# (default stable). Needs Docker that allows
# privileged containers (Docker Desktop works), about 6 GB of disk, and network
# access. The first run takes 5 to 15 minutes, mostly pulling Home Assistant.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
name="${DEVCONTAINER_NAME:-sendtox3-ha-devcontainer}"
image="${DEVCONTAINER_IMAGE:-ghcr.io/home-assistant/devcontainer:5-apps}"
channel="${SUPERVISOR_CHANNEL:-stable}"
work="${WORK_DIR:-${repo}/e2e/work/ha}"
workspace="${work}/devcontainer-workspace/xteink"
slug="local_send_to_x3"
supervisor_api="http://172.30.32.2"

log() { printf '\n==> %s\n' "$*"; }
fail() {
    printf '\nFAIL: %s\n' "$*" >&2
    exit 1
}
dc() { docker exec "${name}" "$@"; }
ha_cli() { dc docker exec hassio_cli ha "$@"; }
app_info() { ha_cli apps info "${slug}" --raw-json; }
healthy() { dc curl -fsS http://127.0.0.1:8765/healthz | jq -e '.ok == true' >/dev/null; }
# Number of stored items, read from the app's own database inside its container
# (/healthz deliberately reports no counts).
items() {
    dc docker exec "${app_container}" python3 -c \
        'from sendtox3.store import Store; print(Store("/data").count())'
}

down() {
    docker rm -f "${name}" >/dev/null 2>&1 || true
    local v
    for v in docker containerd supervisor; do
        docker volume rm -f "${name}-${v}" >/dev/null 2>&1 || true
    done
}

# wait_for <seconds> <command...>: retry every 5 s; returns 1 on timeout.
wait_for() {
    local deadline=$((SECONDS + $1))
    shift
    until "$@" >/dev/null 2>&1; do
        ((SECONDS < deadline)) || return 1
        sleep 5
    done
}

if [[ "${1:-}" == "--down" ]]; then
    down
    exit 0
fi

log "Copying the repository to ${workspace}"
mkdir -p "${workspace}"
rsync -a --delete \
    --exclude .git --exclude .pytest_cache --exclude __pycache__ --exclude 'e2e/work' \
    "${repo}/" "${workspace}/"

log "Starting ${image} as ${name}"
down
docker run -d --name "${name}" --privileged \
    -v "${name}-docker:/var/lib/docker" \
    -v "${name}-containerd:/var/lib/containerd" \
    -v "${name}-supervisor:/mnt/supervisor" \
    --tmpfs /tmp \
    --mount "type=bind,source=${workspace},target=/mnt/supervisor/apps/local/xteink" \
    -e WORKSPACE_DIRECTORY=/mnt/supervisor/apps/local/xteink \
    -e SUPERVISOR_CHANNEL="${channel}" \
    -p 127.0.0.1:7123:8123 \
    -p 127.0.0.1:7357:4357 \
    "${image}" >/dev/null
if [[ -z "${KEEP:-}" ]]; then
    trap 'rc=$?; log "Tearing down (KEEP=1 keeps it)"; down; exit "${rc}"' EXIT
fi

wait_for 180 dc docker info || fail "dockerd did not start inside the devcontainer"

log "Starting the Supervisor (channel ${channel})"
dc bash -c 'nohup bash -lc supervisor_run > /var/log/supervisor_run.log 2>&1 &'
if ! wait_for 900 ha_cli supervisor info; then
    dc tail -n 60 /var/log/supervisor_run.log >&2 || true
    fail "the Supervisor API did not come up"
fi
ha_cli supervisor info --raw-json | jq -r '.data | "Supervisor \(.version) (\(.arch), channel \(.channel))"'

log "1. Store, build and install"
store_has_app() {
    ha_cli store reload >/dev/null 2>&1 || true
    app_info
}
wait_for 300 store_has_app || fail "the Supervisor store does not list ${slug}"
app_info | jq -r '.data | "store: \(.name) \(.version_latest), rating \(.rating), ingress \(.ingress), local build \(.build)"'
ha_cli apps install "${slug}" || fail "install (image build) failed"

log "2. Start"
ha_cli apps start "${slug}" || true
is_started() { app_info | jq -e '.data.state == "started"'; }
if ! wait_for 120 is_started; then
    ha_cli apps logs "${slug}" | tail -n 60 >&2 || true
    fail "${slug} did not reach state started"
fi
app_info | jq -r '.data | "installed \(.version), state \(.state), ip \(.ip_address), ports \(.network | tostring)"'
# The container is app_<slug> since the "apps" rename (addon_<slug> before).
app_container="$(dc docker ps --format '{{.Names}}' | grep -E "^(app|addon)_${slug}\$" | head -n 1)"
[[ -n "${app_container}" ]] || fail "no running container for ${slug}"
echo "container ${app_container}; options.json (secrets hidden):"
dc docker exec "${app_container}" cat /data/options.json |
    jq -c 'with_entries(select(.key | test("token|password") | not))'

log "3. /healthz on the published host port"
wait_for 60 healthy || fail "/healthz did not answer on host port 8765"
dc curl -fsS http://127.0.0.1:8765/healthz
echo

log "4. Admin UI from another container on the hassio network"
app_ip="$(app_info | jq -r '.data.ip_address')"
code="$(dc docker exec hassio_cli curl -s -o /dev/null -w '%{http_code}' "http://${app_ip}:8765/admin")"
echo "GET /admin without a token: HTTP ${code}"
[[ "${code}" == "401" ]] || fail "the admin UI must need a token off the ingress path"

log "Waiting for Home Assistant Core"
# Core may answer on 8123 or redirect it to port 80; use whichever asks for auth.
core_url=""
find_core() {
    local url
    for url in http://127.0.0.1:8123 http://127.0.0.1; do
        if [[ "$(dc curl -s -o /dev/null -w '%{http_code}' "${url}/api/")" == "401" ]]; then
            core_url="${url}"
            return 0
        fi
    done
    return 1
}
supervisor_user() {
    dc jq -e -r '.data.users[] | select(.system_generated and .name == "Supervisor") | .id' \
        /mnt/supervisor/homeassistant/.storage/auth
}
wait_for 1200 find_core || fail "Home Assistant Core did not come up"
wait_for 300 supervisor_user || fail "Core has no Supervisor user yet"
echo "Core at ${core_url}"

log "5. Ingress: browser -> Core -> Supervisor -> app"
core_token="$(dc jq -r '.access_token // empty' /mnt/supervisor/homeassistant.json)"
[[ -n "${core_token}" ]] || fail "no Core access token in /mnt/supervisor/homeassistant.json"
# What Core does for a logged-in frontend user (POST /ingress/session is Core-only).
session="$(dc curl -fsS -X POST -H "Authorization: Bearer ${core_token}" -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$(supervisor_user)\"}" "${supervisor_api}/ingress/session" | jq -r '.data.session')"
[[ -n "${session}" && "${session}" != "null" ]] || fail "could not create an ingress session"
entry="$(app_info | jq -r '.data.ingress_entry')"
ingress="${core_url}${entry}"
page=""
load_ui() { page="$(dc curl -fsS --cookie "ingress_session=${session}" "${ingress}/")"; }
# Core answers /api/ before its hassio integration (and so ingress) is ready.
if ! wait_for 180 load_ui; then
    dc curl -sS -i --cookie "ingress_session=${session}" "${ingress}/" | head -n 20 >&2 || true
    fail "the web UI did not load through ingress"
fi
csrf="$(grep -o 'name="csrf" value="[^"]*"' <<<"${page}" | awk -F'"' 'NR == 1 { print $4 }')"
[[ -n "${csrf}" ]] || fail "ingress showed the login page instead of the web UI"
grep -q "action=\"${entry}/admin/send\"" <<<"${page}" || fail "links are not under the ingress path"
echo "web UI loaded through ${entry}/ without an app token; links use the ingress path"
before="$(items)"
code="$(dc curl -s -o /dev/null -w '%{http_code}' --cookie "ingress_session=${session}" \
    --data-urlencode "csrf=${csrf}" \
    --data-urlencode "content=Sent through Home Assistant ingress by the devcontainer test." \
    "${ingress}/admin/send")"
after="$(items)"
echo "paste form POST: HTTP ${code}, items ${before} -> ${after}"
((after > before)) || fail "the paste form did not create an item through ingress"
dc sh -c 'head -c 20000000 /dev/zero > /tmp/sendtox3-20mb.bin'
code="$(dc curl -s -o /dev/null -w '%{http_code}' --cookie "ingress_session=${session}" \
    -F "csrf=${csrf}" -F "file=@/tmp/sendtox3-20mb.bin;type=application/octet-stream" \
    "${ingress}/admin/upload")"
echo "20 MB upload: HTTP ${code} (422 = reached the app, which rejects the file type)"
[[ "${code}" == "422" ]] || fail "a 20 MB upload did not reach the app through ingress"

log "6. Companion app share through Home Assistant Core"
subscribed() { ha_cli apps logs "${slug}" | grep "HA share: subscribed" >/dev/null; }
wait_for 180 subscribed || fail "the ha_share worker never subscribed to mobile_app.share"
before="$(items)"
# Single quotes on purpose: ${SUPERVISOR_TOKEN} expands inside the app container.
# shellcheck disable=SC2016
dc docker exec "${app_container}" sh -c \
    'curl -fsS -X POST -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" \
        -d "{\"text\": \"Shared from the Companion app in the devcontainer test.\"}" \
        http://supervisor/core/api/events/mobile_app.share'
echo
grew() { (($(items) > before)); }
wait_for 60 grew || fail "the mobile_app.share event did not become an item"
echo "items ${before} -> $(items)"

log "App log (last 30 lines)"
ha_cli apps logs "${slug}" | tail -n 30

log "PASS: the Supervisor built, installed and started ${slug}; ingress and share work"
