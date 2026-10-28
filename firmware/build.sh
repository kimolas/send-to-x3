#!/usr/bin/env bash
# Build the Send to X3 CrossPoint firmware for the X3 (and X4) in Docker:
# upstream CrossPoint at the commit in firmware/UPSTREAM plus firmware/patches/.
#
#   firmware/build.sh                  # clone/update upstream, apply the patches, test, build
#   firmware/build.sh --src DIR        # build an existing CrossPoint checkout as it is
#   firmware/build.sh --no-tests       # skip the plugin host tests
#   firmware/build.sh --tests-only     # only the host tests
#
# Output: firmware/dist/crosspoint-2.0.0-sx3+<hash>-x3-x4.bin (gitignored) and a
# .size.txt with the RAM/flash summary. Needs Docker and git. The source tree
# lives in $SENDTOX3_FW_WORK (default ~/.cache/sendtox3-firmware); the builder is
# the image sendtox3-firmware-builder:1 (made from python:3.13-slim by
# container-setup.sh); PlatformIO's packages and the build output live in the
# Docker volumes sendtox3-fw-pio and sendtox3-fw-build (about 5 GB). To free the
# space: `docker volume rm` the volumes and `docker image rm` the two images.
# The first build downloads the toolchain and rebuilds the Arduino core: expect
# 20-40 minutes. Later builds take a few minutes.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="$(sed -n 's/^URL=//p' "${here}/UPSTREAM")"
COMMIT="$(sed -n 's/^COMMIT=//p' "${here}/UPSTREAM")"
work="${SENDTOX3_FW_WORK:-${HOME}/.cache/sendtox3-firmware}"
image="sendtox3-firmware-builder:1"

src=""
run_args=()
while (( $# )); do
  case "$1" in
    --src) src="$(cd "$2" && pwd)"; shift 2 ;;
    --no-tests) run_args+=(--no-tests); shift ;;
    --tests-only) run_args+=(--no-build); shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "build.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

if [[ -z "${src}" ]]; then
  src="${work}/crosspoint"
  if [[ ! -d "${src}/.git" ]]; then
    mkdir -p "${work}"
    git clone --quiet "${URL}" "${src}"
  fi
  if ! git -C "${src}" cat-file -e "${COMMIT}^{commit}" 2>/dev/null; then
    git -C "${src}" fetch --quiet origin
  fi
  # Drop a git am that failed in an earlier run: checkout and clean leave its
  # .git/rebase-apply behind, and every later git am would refuse to start.
  git -C "${src}" am --quit >/dev/null 2>&1 || true
  git -C "${src}" checkout --quiet --force --detach "${COMMIT}"
  git -C "${src}" clean -fdq
  git -C "${src}" submodule update --init --recursive --quiet
  # Fixed committer and dates: the same patches always give the same commit, so
  # the version (2.0.0-sx3+<hash>) names exactly what was built.
  if ! git -C "${src}" -c user.name=sendtox3 -c user.email=sendtox3@localhost -c commit.gpgsign=false \
      am --quiet --committer-date-is-author-date "${here}"/patches/*.patch; then
    git -C "${src}" am --quit >/dev/null 2>&1 || true
    echo "build.sh: firmware/patches do not apply to ${COMMIT} (firmware/UPSTREAM);" \
      "see 'Moving to a newer CrossPoint' in firmware/README.md" >&2
    exit 1
  fi
fi
case "${src}" in
  *" "*) echo "build.sh: the source path must not contain spaces: ${src}" >&2; exit 2 ;;
esac

hash="$(git -C "${src}" rev-parse --short=7 HEAD)"
out="${work}/out"
mkdir -p "${out}"
# Docker mounts these from the work folder: the project may sit in a synced
# folder (with spaces in its path) that Docker cannot share.
cp "${here}/container-setup.sh" "${here}/container-build.sh" "${work}/"

if ! docker image inspect "${image}" >/dev/null 2>&1; then
  setup="sendtox3-firmware-setup-$$"
  docker run --name "${setup}" --volume "${work}/container-setup.sh:/setup.sh:ro" \
    python:3.13-slim sh /setup.sh
  docker commit "${setup}" "${image}" >/dev/null
  docker rm "${setup}" >/dev/null
fi
docker run --rm \
  --volume "${work}/container-build.sh:/usr/local/bin/sendtox3-firmware-build:ro" \
  --volume "${src}:/src" \
  --volume "${out}:/out" \
  --volume sendtox3-fw-pio:/root/.platformio \
  --volume sendtox3-fw-build:/build \
  --env SENDTOX3_FW_HASH="${hash}" \
  --env PLATFORMIO_BUILD_DIR=/build/pio \
  --env PLATFORMIO_BUILD_CACHE_DIR=/build/cache \
  "${image}" sendtox3-firmware-build "${run_args[@]+"${run_args[@]}"}"

mkdir -p "${here}/dist"
shopt -s nullglob
for f in "${out}"/crosspoint-*-sx3+"${hash}"-x3-x4.*; do
  cp "${f}" "${here}/dist/"
  echo "firmware/dist/$(basename "${f}")"
done
