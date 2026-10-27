#!/usr/bin/env bash
# Validate every .epub in a directory with epubcheck 5.4.0 (--failonwarnings), offline.
#   e2e/epubcheck/run.sh <dir-with-epubs>
# Builds the image (e2e/epubcheck/Dockerfile) on first use. Prints PASS/FAIL per file with
# epubcheck's messages for failures. Exit 0 only if every file has 0 fatals/errors/warnings.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${EPUBCHECK_IMAGE:-sendtox3-epubcheck:5.4.0}"
DIR="$(cd "${1:?usage: run.sh <dir-with-epubs>}" && pwd)"
ls "$DIR"/*.epub >/dev/null 2>&1 || { echo "no .epub files in $DIR" >&2; exit 2; }
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -q -t "$IMAGE" "$HERE" >/dev/null
fi
# One container, one JVM per book (epubcheck takes a single publication per run).
docker run --rm --network none -v "$DIR:/data:ro" --entrypoint /bin/sh "$IMAGE" -c '
  status=0
  for f in /data/*.epub; do
    name=$(basename "$f")
    if out=$(java -jar /opt/epubcheck/epubcheck.jar --failonwarnings "$f" 2>&1); then
      echo "PASS $name"
    else
      echo "FAIL $name"
      printf "%s\n" "$out" | sed "s/^/    /"
      status=1
    fi
  done
  exit $status
'
