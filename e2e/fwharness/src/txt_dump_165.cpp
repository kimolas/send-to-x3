// Host harness: CrossPoint 1.x's real TXT pagination. build.sh copies
// src/activities/reader/TxtReaderActivity.cpp verbatim next to the stubs in stubs/txt165 and
// links it with lib/Txt/Txt.cpp and the real built-in font metrics (stubs/realfont).
// Usage: CP_SDROOT=<sd dir> txt_dump /path/on/sd/file.txt [insetT insetR insetB insetL]
// Prints the title the reader shows (the file name), viewport, page count and index time,
// then every page; exits 0 when the file loads.
#include <chrono>
#include <cstdio>
#include <Txt.h>
#include <memory>
#include <string>
#include <vector>
#include "CrossPointSettings.h"
#include "ReaderActivity.h"
// Same trick as test/chapter_html_slim_parser/ChapterHtmlSlimParserTest.cpp:11-15 (headers above are guarded).
#define class struct
#define private public
#include "TxtReaderActivity.h"
#undef private
#undef class

int main(int argc, char** argv) {
  if (argc < 2) { std::fprintf(stderr, "usage: %s /file.txt\n", argv[0]); return 2; }
  GfxRenderer renderer;
  if (argc >= 6) { renderer.insetT = std::atoi(argv[2]); renderer.insetR = std::atoi(argv[3]);
                   renderer.insetB = std::atoi(argv[4]); renderer.insetL = std::atoi(argv[5]); }
  MappedInputManager input;
  Storage.mkdir("/.crosspoint");
  TxtReaderActivity act(renderer, input, argv[1], false);
  if (!act.loadBook()) { std::printf("LOAD FAILED\n"); return 1; }
  act.txt->clearCache();
  act.txt->setupCacheDir();
  std::printf("TITLE (status bar / library / recent) = [%s]\n", act.getBookTitle().c_str());
  const auto t0 = std::chrono::steady_clock::now();
  act.initializeReader(renderer);
  const auto t1 = std::chrono::steady_clock::now();
  std::printf("viewportWidth=%d linesPerPage=%d lineHeight=%d totalPages=%d indexMs=%lld measureCalls=%llu\n",
              act.viewportWidth, act.linesPerPage, renderer.getLineHeight(0), act.totalPages,
              (long long)std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count(), renderer.measureCalls);
  for (int p = 0; p < act.totalPages; p++) {
    std::vector<std::string> lines; size_t next = 0;
    act.loadPageAtOffset(renderer, act.pageOffsets[p], lines, next);
    std::printf("-- page %d (offset %zu)\n", p, act.pageOffsets[p]);
    for (const auto& l : lines) {
      std::string vis;
      for (unsigned char c : l) { if (c == '\t') vis += "\\t"; else if (c == '\r') vis += "\\r"; else vis += (char)c; }
      const char* note = "";
      const unsigned char* q = reinterpret_cast<const unsigned char*>(l.c_str());
      uint32_t cp; while ((cp = utf8NextCodepoint(&q))) { if (cp >= 0x80 && !renderer.hasGlyph(cp)) { note = "   <-- contains codepoint with no glyph (renders U+FFFD)"; break; }
                                                         if (cp < 0x20 && cp != 0x0D) { note = "   <-- control char (renders U+FFFD)"; break; } }
      std::printf("  [%3d px] |%s|%s\n", renderer.getTextAdvanceX(0, l.c_str(), EpdFontFamily::REGULAR), vis.c_str(), note);
    }
  }
  return 0;
}
