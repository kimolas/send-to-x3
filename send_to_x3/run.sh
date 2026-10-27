#!/command/with-contenv bashio
# shellcheck shell=bash
# ==============================================================================
# Send to X3: start the server.
#
# This is the image CMD, run by s6-overlay's /init (config.yaml has init: false).
# `with-contenv` hands this script the container environment (SUPERVISOR_TOKEN,
# TZ, ...), and `exec` makes Python the process s6 watches, so when it exits the
# container stops and the Supervisor watchdog can restart it.
#
# Options are not read here: Python reads /data/options.json, which the
# Supervisor writes before every start. That also works with plain
# `docker run -v <dir>:/data` and no Supervisor at all.
# ==============================================================================

data_dir="${SENDTOX3_DATA_DIR:-/data}"
mkdir -p "${data_dir}"

if [[ -n "${SUPERVISOR_TOKEN:-}" ]]; then
    bashio::log.info "Starting Send to X3 (Home Assistant app)"
else
    bashio::log.info "Starting Send to X3 (standalone, no Supervisor)"
fi

if [[ ! -f "${data_dir}/options.json" ]]; then
    bashio::log.info "No ${data_dir}/options.json; using defaults and SENDTOX3_* variables"
fi

exec python3 -u -m sendtox3
