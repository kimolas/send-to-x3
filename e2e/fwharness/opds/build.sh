#!/bin/sh
# Builds a host (macOS/Linux) binary that runs CrossPoint 1.6.5's OpdsParser + UrlUtils +
# OpdsFilename exactly as compiled for the device (vendored expat, -DXML_GE=0
# -DXML_CONTEXT_BYTES=1024).
#
# Usage: CP_REPO=/path/to/crosspoint-reader@1.6.5 [FWH_WORK=/tmp/x] e2e/fwharness/opds/build.sh
#        then  "$FWH_WORK/opds/opds_check" feed.xml [serverUrl] [currentPathOrFeedUrl] [chunk]
# The binary path is printed on the last line. Nothing is written into the repo.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
: "${CP_REPO:?set CP_REPO to a CrossPoint 1.6.5 checkout}"
CP=$CP_REPO
OUT=${FWH_WORK:-${TMPDIR:-/tmp}/sendtox3-fwharness}/opds
SYS=""
SYSXX=""
if [ "$(uname)" = "Darwin" ]; then
  SDK=$(xcrun --show-sdk-path)
  # Some Command Line Tools installs hide libc++ headers; point at the SDK's copy.
  SYS="-isysroot $SDK"; SYSXX="-isysroot $SDK -I$SDK/usr/include/c++/v1"
fi
mkdir -p "$OUT"
for f in xmlparse xmlrole xmltok; do
  cc -c -O1 $SYS -DXML_GE=0 -DXML_CONTEXT_BYTES=1024 -I"$CP/lib/expat" "$CP/lib/expat/$f.c" -o "$OUT/$f.o"
done
c++ -std=gnu++20 -O1 -Wall -Wextra $SYSXX -I"$HERE/stubs" -I"$CP/lib/OpdsParser" -I"$CP/lib/XmlParserUtils" \
  -I"$CP/lib/expat" -I"$CP/src/util" -I"$CP/lib/Utf8" \
  "$HERE/main.cpp" "$CP/lib/OpdsParser/OpdsParser.cpp" "$CP/lib/OpdsParser/OpdsStream.cpp" \
  "$CP/src/util/UrlUtils.cpp" "$CP/src/util/OpdsFilename.cpp" "$CP/src/util/StringUtils.cpp" "$CP/lib/Utf8/Utf8.cpp" \
  "$OUT/xmlparse.o" "$OUT/xmlrole.o" "$OUT/xmltok.o" -o "$OUT/opds_check"
echo "$OUT/opds_check"
