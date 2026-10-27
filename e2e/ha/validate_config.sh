#!/usr/bin/env bash
# Validate send_to_x3 with the real Home Assistant Supervisor schemas.
#
# Clones home-assistant/supervisor at a pinned release, installs its requirements
# into a venv (both cached in WORK_DIR), then runs validate_config.py.
#
#   e2e/ha/validate_config.sh                        # cache in e2e/work/ha (gitignored)
#   WORK_DIR=/tmp/sendtox3-ha e2e/ha/validate_config.sh
#   PYTHON=python3.14 SUPERVISOR_TAG=2026.10.4 e2e/ha/validate_config.sh
#
# Needs git, network access for the first run, and Python >= 3.14 (the
# Supervisor's minimum).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
tag="${SUPERVISOR_TAG:-2026.10.4}"
work="${WORK_DIR:-${repo}/e2e/work/ha}"
python="${PYTHON:-python3}"
src="${work}/supervisor-${tag}"
venv="${work}/venv-supervisor-${tag}"

if ! "${python}" -c 'import sys; sys.exit(sys.version_info < (3, 14))'; then
    echo "validate_config.sh: ${python} is older than Python 3.14; set PYTHON=" >&2
    exit 2
fi

mkdir -p "${work}"
if [[ ! -f "${src}/supervisor/apps/validate.py" ]]; then
    rm -rf "${src}"
    git clone --quiet --depth 1 --branch "${tag}" \
        https://github.com/home-assistant/supervisor.git "${src}"
fi
if [[ ! -x "${venv}/bin/python" ]] || [[ ! -f "${venv}/.installed" ]]; then
    "${python}" -m venv "${venv}"
    "${venv}/bin/pip" install --quiet --disable-pip-version-check -r "${src}/requirements.txt"
    touch "${venv}/.installed"
fi

exec "${venv}/bin/python" -I "${here}/validate_config.py" --supervisor "${src}" "$@"
