#!/usr/bin/env bash
# Build host harnesses that run CrossPoint's REAL EPUB, TXT and file-name code on this
# machine (no CMake, no googletest, no device). The CrossPoint checkout is only read.
#
#   CP_REPO=/path/to/crosspoint-reader [CP_SDK=/path/to/freeink-sdk] \
#   [FWH_WORK=/some/dir/outside/the/repo] e2e/fwharness/build.sh
#
# Outputs (in $FWH_WORK/<git describe of CP_REPO>/bin):
#   epub_dump    full EPUB pipeline (ZipFile, container, OPF, nav/NCX, BookMetadataCache,
#                CssParser, hyphenation, ChapterHtmlSlimParser + ParsedText layout) measured
#                with the real built-in Noto fonts. Exit 0 = metadata + every chapter OK.
#   txt_dump     1.x: verbatim TxtReaderActivity.cpp + lib/Txt pagination;
#                2.x: lib/Txt + TxtToHtml conversion + the chapter parser (how 2.x reads .txt).
#   name_check   StringUtils::sanitizeFilename* / opdsBookFilename / ProtectedPaths rules.
# The last line printed is "FWH_BIN=<bin dir>".
#
# Works with CrossPoint 1.6.5 and 2.0.0rc/develop (3f387a9). 2.x needs the freeink-sdk
# submodule (git submodule update --init freeink-sdk, or point CP_SDK at an export of the
# pinned commit). macOS: uses the SDK's libc++ headers when the Command Line Tools copy is
# broken (the CPLUS_INCLUDE_PATH quirk), and the SDK's libexpat.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
: "${CP_REPO:?set CP_REPO to a CrossPoint checkout}"
R="$(cd "$CP_REPO" && pwd)"
SDKDIR="${CP_SDK:-$R/freeink-sdk}"
LABEL="$(git -C "$R" describe --tags --always 2>/dev/null || basename "$R")"
LABEL="$(printf '%s' "$LABEL" | tr -c 'A-Za-z0-9._-' '_')"
WORK="${FWH_WORK:-${TMPDIR:-/tmp}/sendtox3-fwharness}"
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
case "$WORK/" in
  "$REPO_ROOT"/*) echo "FWH_WORK must be outside the repository ($REPO_ROOT)" >&2; exit 2 ;;
esac
OUT="$WORK/$LABEL"
BIN="$OUT/bin"
OBJ="$OUT/obj"
JOBS="${FWH_JOBS:-$( (sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4) | head -1)}"

if [ -f "$R/src/activities/reader/TxtReaderActivity.cpp" ]; then
  FAMILY=1x
elif [ -f "$R/lib/Txt/TxtToHtml.cpp" ]; then
  FAMILY=2x
else
  echo "unrecognised CrossPoint layout in $R" >&2
  exit 2
fi

# ---------------------------------------------------------------- toolchain
CC="${CC:-clang}"
CXX="${CXX:-clang++}"
CSYS=()
CXXSYS=()
if [ "$(uname -s)" = Darwin ]; then
  SDK="$(xcrun --show-sdk-path)"
  CSYS=(-isysroot "$SDK")
  CXXSYS=(-isysroot "$SDK")
  # Some Command Line Tools installs ship a stale usr/include/c++/v1 that hides the SDK's
  # libc++ headers ("'cassert' file not found"); fall back to the SDK copy explicitly.
  if ! echo '#include <cassert>' | "$CXX" -std=c++20 -x c++ -fsyntax-only ${CXXSYS[@]+"${CXXSYS[@]}"} - 2>/dev/null; then
    CXXSYS+=(-nostdinc++ -isystem "$SDK/usr/include/c++/v1")
  fi
fi
CXXFLAGS=(-std=c++20 -O1 -w -DCROSSPOINT_HOST_HARNESS=1)

# ---------------------------------------------------------------- up-to-date check
stamp_inputs() {
  git -C "$R" rev-parse HEAD 2>/dev/null || echo "no-git"
  git -C "$R" status --porcelain 2>/dev/null | head -50
  echo "$SDKDIR"
  "$CXX" --version 2>/dev/null | head -1
  (cd "$HERE" && find . -type f \( -name '*.cpp' -o -name '*.h' -o -name '*.sh' \) -print | LC_ALL=C sort | xargs cat)
}
STAMP="$(stamp_inputs | (shasum -a 256 2>/dev/null || sha256sum) | cut -c1-64)"
if [ -f "$OUT/.stamp" ] && [ "$(cat "$OUT/.stamp")" = "$STAMP" ] &&
   [ -x "$BIN/epub_dump" ] && [ -x "$BIN/txt_dump" ] && [ -x "$BIN/name_check" ]; then
  echo "up to date: $BIN ($FAMILY)"
  echo "FWH_BIN=$BIN"
  exit 0
fi
rm -rf "$OBJ" "$BIN"
mkdir -p "$OBJ" "$BIN"

# ---------------------------------------------------------------- helpers
# compile_many <objdir> <flags-array-name> <sources...>: parallel compile, prints objects
compile_many() {
  local objdir="$1" flagsvar="$2"
  shift 2
  mkdir -p "$objdir"
  local src obj pids="" status=0
  for src in "$@"; do
    obj="$objdir/$(printf '%s' "$src" | sed "s#^$R/##; s#^$OBJ/##; s#^$HERE/##; s#/#__#g").o"
    printf '%s\n' "$obj"
    (
      eval "flags=(\"\${${flagsvar}[@]}\")"
      case "$src" in
        *.c) "$CC" -c -O1 ${CSYS[@]+"${CSYS[@]}"} ${flags[@]+"${flags[@]}"} "$src" -o "$obj" ;;
        *) "$CXX" -c "${CXXFLAGS[@]}" ${CXXSYS[@]+"${CXXSYS[@]}"} ${flags[@]+"${flags[@]}"} "$src" -o "$obj" ;;
      esac
    ) >"$obj.log" 2>&1 &
    pids="$pids $!"
    while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$JOBS" ]; do sleep 0.1; done
  done
  for pid in $pids; do wait "$pid" || status=1; done
  if [ "$status" -ne 0 ]; then
    for log in "$objdir"/*.log; do [ -s "$log" ] && { echo "== $log" >&2; head -40 "$log" >&2; }; done
    return 1
  fi
}

link() {  # link <output> <objects...>
  local out="$1"
  shift
  "$CXX" ${CXXSYS[@]+"${CXXSYS[@]}"} "$@" -lexpat -o "$out"
  echo "built $out"
}

# ---------------------------------------------------------------- name_check
NAME_FLAGS=(-I"$R/src/util" -I"$R/src/network" -I"$R/lib/Utf8")
if grep -q sanitizeFilenamePreservingExtension "$R/src/util/StringUtils.h"; then
  NAME_FLAGS+=(-DCP_HAS_PRESERVING_EXTENSION=1)
fi
if [ -f "$R/src/network/ProtectedPaths.h" ]; then
  NAME_FLAGS+=(-DCP_HAS_PROTECTED_PATHS=1)
fi
NAME_SRC=("$HERE/src/name_check.cpp" "$R/src/util/StringUtils.cpp" "$R/src/util/OpdsFilename.cpp" "$R/lib/Utf8/Utf8.cpp")
NAME_OBJS="$(compile_many "$OBJ/name" NAME_FLAGS "${NAME_SRC[@]}")"
# shellcheck disable=SC2086
link "$BIN/name_check" $NAME_OBJS

# ---------------------------------------------------------------- EPUB pipeline
if [ "$FAMILY" = 2x ]; then
  EPUB_INC=(-I"$HERE/stubs/realfont2x" -I"$HERE/stubs/common" -I"$HERE/stubs/common2x")
else
  EPUB_INC=(-I"$HERE/stubs/realfont" -I"$HERE/stubs/common")
fi
EPUB_INC+=(-I"$R" -I"$R/lib" -I"$R/lib/Epub" -I"$R/lib/Epub/Epub" -I"$R/lib/Epub/Epub/parsers"
  -I"$R/lib/Epub/Epub/css" -I"$R/lib/EpdFont" -I"$R/lib/Utf8" -I"$R/lib/Memory" -I"$R/lib/Serialization"
  -I"$R/lib/MiniBidi" -I"$R/lib/FsHelpers" -I"$R/lib/XmlParserUtils" -I"$R/lib/ZipFile" -I"$R/lib/miniz/src"
  -I"$R/lib/hal" -I"$R/lib/Txt")
[ -d "$R/lib/AsciiText" ] && EPUB_INC+=(-I"$R/lib/AsciiText")
[ -d "$R/lib/GifSupport" ] && EPUB_INC+=(-I"$R/lib/GifSupport")
if [ "$FAMILY" = 2x ]; then
  CPI="$SDKDIR/libs/book/ContentProtection/include"
  if [ ! -f "$CPI/ContentProtection.h" ]; then
    echo "2.x needs the freeink-sdk submodule: git -C '$R' submodule update --init freeink-sdk, or set CP_SDK" >&2
    exit 2
  fi
  # Only the ContentDecryptor interface header; openProtectedBook is provided by the link stubs.
  EPUB_INC+=(-I"$CPI")
fi
C_INC=(-I"$R/lib/MiniBidi" -I"$R/lib/miniz/src")

E="$R/lib/Epub/Epub"
EPUB_SRC=(
  "$R/lib/Epub/Epub.cpp" "$E/BookMetadataCache.cpp" "$E/css/CssParser.cpp"
  "$E/parsers/ContainerParser.cpp" "$E/parsers/ContentOpfParser.cpp" "$E/parsers/TocNavParser.cpp"
  "$E/parsers/TocNcxParser.cpp" "$E/parsers/ChapterHtmlSlimParser.cpp" "$E/ParsedText.cpp" "$E/WordStore.cpp"
  "$E/Page.cpp" "$E/blocks/TextBlock.cpp" "$E/converters/ImageDimsProbe.cpp" "$E/htmlEntities.cpp"
  "$E/hyphenation/Hyphenator.cpp" "$E/hyphenation/LanguageRegistry.cpp" "$E/hyphenation/LiangHyphenation.cpp"
  "$E/hyphenation/HyphenationCommon.cpp"
  "$R/lib/ZipFile/ZipFile.cpp" "$R/lib/miniz/src/InflateStream.cpp" "$R/lib/Memory/BuildScratch.cpp"
  "$R/lib/hal/HalMemory.cpp" "$R/lib/FsHelpers/FsHelpers.cpp" "$R/lib/MiniBidi/BidiUtils.cpp"
  "$R/lib/Utf8/Utf8.cpp" "$R/lib/EpdFont/EpdFont.cpp" "$R/lib/EpdFont/EpdFontFamily.cpp"
)
if [ "$FAMILY" = 2x ]; then
  # 2.x: Epub.cpp opens .txt/.md itself, so the TXT library is part of the EPUB pipeline.
  EPUB_SRC+=("$R/lib/Txt/Txt.cpp" "$R/lib/Txt/TxtToHtml.cpp" "$HERE/src/epub_link_stubs_2x.cpp")
  # Added on develop after 2.0.0rc (CJK line breaking); compile it where it exists.
  [ -f "$E/CjkBreaks.cpp" ] && EPUB_SRC+=("$E/CjkBreaks.cpp")
else
  EPUB_SRC+=("$HERE/src/epub_link_stubs_165.cpp")
fi
C_SRC=("$R/lib/MiniBidi/minibidi.c" "$R/lib/miniz/src/miniz_impl.c")

C_OBJS="$(compile_many "$OBJ/c" C_INC "${C_SRC[@]}")"
EPUB_OBJS="$(compile_many "$OBJ/epub" EPUB_INC "${EPUB_SRC[@]}" "$HERE/src/epub_dump.cpp")"
# shellcheck disable=SC2086
link "$BIN/epub_dump" $EPUB_OBJS $C_OBJS

# ---------------------------------------------------------------- TXT
if [ "$FAMILY" = 1x ]; then
  # Verbatim copies: quoted #includes resolve next to the .cpp, so the stubs sit beside it.
  TXTSRC="$OBJ/txtsrc"
  mkdir -p "$TXTSRC"
  cp "$R/src/activities/reader/TxtReaderActivity.cpp" "$R/src/activities/reader/TxtReaderActivity.h" \
     "$R/src/activities/reader/ProgressFile.h" "$R/src/fontIds.h" "$TXTSRC/"
  cp -R "$HERE/stubs/txt165/." "$TXTSRC/"
  TXT_INC=(-I"$TXTSRC" -I"$HERE/stubs/realfont" -I"$HERE/stubs/common" -I"$R/lib" -I"$R/lib/Txt" -I"$R/lib/Utf8"
    -I"$R/lib/MiniBidi" -I"$R/lib/Serialization" -I"$R/lib/Memory" -I"$R/lib/FsHelpers" -I"$R/lib/EpdFont")
  TXT_SRC=("$HERE/src/txt_dump_165.cpp" "$TXTSRC/TxtReaderActivity.cpp" "$R/lib/Txt/Txt.cpp"
    "$R/lib/FsHelpers/FsHelpers.cpp" "$R/lib/Utf8/Utf8.cpp" "$R/lib/MiniBidi/BidiUtils.cpp"
    "$R/lib/EpdFont/EpdFont.cpp" "$R/lib/EpdFont/EpdFontFamily.cpp")
  TXT_OBJS="$(compile_many "$OBJ/txt" TXT_INC "${TXT_SRC[@]}")"
  # shellcheck disable=SC2086
  link "$BIN/txt_dump" $TXT_OBJS "$OBJ/c/lib__MiniBidi__minibidi.c.o"
else
  TXT_OBJS="$(compile_many "$OBJ/txt" EPUB_INC "$HERE/src/txt_dump_2x.cpp")"
  # shellcheck disable=SC2086
  link "$BIN/txt_dump" $TXT_OBJS $(printf '%s\n' $EPUB_OBJS | grep -v '__epub_dump.cpp.o$') $C_OBJS
fi

printf '%s\n' "$STAMP" >"$OUT/.stamp"
echo "family=$FAMILY repo=$R"
echo "FWH_BIN=$BIN"
