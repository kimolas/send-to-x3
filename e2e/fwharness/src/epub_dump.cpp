// Host harness: run CrossPoint's real EPUB pipeline (ZipFile + container.xml + OPF + nav/NCX
// + BookMetadataCache + CssParser + hyphenation + ChapterHtmlSlimParser/ParsedText layout)
// against an .epub and dump library metadata, spine, TOC and the laid-out pages. Text is
// measured with the real built-in reader fonts (stubs/realfont/GfxRenderer.h).
//
// Usage: CP_SDROOT=<sd dir> epub_dump /path/on/sd/book.epub [viewportW=518] [viewportH=768]
//   The path is resolved under $CP_SDROOT when absolute. Env: CP_FONT=serif|sans,
//   CP_PT=12|14|16|18, CP_LOG=1 (firmware log lines on stderr), CP_HYPHENATE=1.
// Exit code 0 only if the library metadata loads, the spine is non-empty and every chapter
// parses. Machine-readable lines start with LIBRARY / META / SPINE / TOC / CHAPTER / EXIT.
#include <Epub.h>
#include <Epub/Page.h>
#include <GfxRenderer.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <memory>

#include "Epub/hyphenation/Hyphenator.h"
#include "Epub/parsers/ChapterHtmlSlimParser.h"

int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr, "usage: %s /book.epub [w] [h]\n", argv[0]);
    return 2;
  }
  const std::string path = argv[1];
  const uint16_t w = argc > 2 ? std::atoi(argv[2]) : 518;
  const uint16_t h = argc > 3 ? std::atoi(argv[3]) : 768;
  const bool hyphenate = std::getenv("CP_HYPHENATE") != nullptr;
  Storage.mkdir("/.crosspoint");
  int rc = 0;
  {  // Library-index path: metadata-only OPF parse, no caches (LibraryBuilder)
    Epub probe(path, "/.crosspoint");
    std::string t, a;
    const bool ok = probe.loadMetadata(t, a);
    std::printf("LIBRARY loadMetadata ok=%d title=[%s] author=[%s]\n", ok, t.c_str(), a.c_str());
    if (!ok) rc = 1;
  }
  auto epub = std::make_shared<Epub>(path, "/.crosspoint");
  epub->clearCache();
  if (!epub->load(true, false)) {
    std::printf("LOAD FAILED\nEXIT 1\n");
    return 1;
  }
  std::printf("META title=[%s] author=[%s] lang=[%s]\n", epub->getTitle().c_str(), epub->getAuthor().c_str(),
              epub->getLanguage().c_str());
  std::printf("SPINE count=%d bookSize=%zu\n", epub->getSpineItemsCount(), epub->getBookSize());
  for (int i = 0; i < epub->getSpineItemsCount(); i++) {
    const auto s = epub->getSpineItem(i);
    std::printf("  spine[%d] href=[%s] cum=%u toc=%d\n", i, s.href.c_str(), s.cumulativeSize, s.tocIndex);
  }
  std::printf("TOC count=%d\n", epub->getTocItemsCount());
  for (int i = 0; i < epub->getTocItemsCount(); i++) {
    const auto t = epub->getTocItem(i);
    std::printf("  toc[%d] level=%u title=[%s] href=[%s] anchor=[%s] spine=%d\n", i, t.level, t.title.c_str(),
                t.href.c_str(), t.anchor.c_str(), t.spineIndex);
  }
  if (epub->getSpineItemsCount() == 0) rc = 1;
  GfxRenderer renderer;
  Hyphenator::setPreferredLanguage(epub->getLanguage());  // as Section::startBuild does
  for (int i = 0; i < epub->getSpineItemsCount(); i++) {
    const auto s = epub->getSpineItem(i);
    const std::string tmp = "/.crosspoint/harness_" + std::to_string(i) + ".html";
    if (!epub->extractItemToFile(s.href, tmp)) {
      std::printf("CHAPTER %d extract FAILED\n", i);
      rc = 1;
      continue;
    }
    const std::string hostTmp = HalStorage::map(tmp);
    const auto slash = s.href.find_last_of('/');
    const std::string contentBase = slash == std::string::npos ? "" : s.href.substr(0, slash + 1);
    const std::string imgBase = epub->getCachePath() + "/img_" + std::to_string(i) + "_";
    CssParser* css = epub->getCssParser();
    if (css) css->loadFromCache();
    int pages = 0;
    auto onPage = [&](std::unique_ptr<Page> page, uint16_t, uint16_t, uint32_t) {
      std::printf("  -- page %d\n", pages++);
      if (!page) return;
      for (const auto& el : page->elements) {
        if (el->getTag() == TAG_PageLine) {
          const auto* b = static_cast<const PageLine&>(*el).getBlock();
          std::printf("    y=%4d x=%3d a=%d |", el->yPos, el->xPos, (int)b->getBlockStyle().alignment);
          for (uint16_t k = 0; k < b->wordCount(); ++k) {
            const int st = (int)b->wordStyle(k);
            std::printf(" %s%s%s", (st & EpdFontFamily::BOLD) ? "*" : "", b->wordText(k),
                        (st & EpdFontFamily::ITALIC) ? "/" : "");
          }
          std::printf("\n");
        } else if (el->getTag() == TAG_PageHorizontalRule) {
          std::printf("    y=%4d <hr>\n", el->yPos);
        } else {
          std::printf("    y=%4d <image>\n", el->yPos);
        }
      }
    };
    // Device defaults: extraParagraphSpacing=1, paragraphAlignment=JUSTIFIED(0), embeddedStyle=1,
    // hyphenation off, imageRendering=display (CrossPointSettings.h).
    ChapterHtmlSlimParser parser(epub, tmp, renderer, 0, 1.0f, true, 0, w, h, hyphenate, false, onPage, true,
                                 contentBase, imgBase, 0, {}, nullptr, css);
    std::printf("CHAPTER %d [%s]\n", i, s.href.c_str());
    const auto t0 = std::chrono::steady_clock::now();
    const bool ok = parser.parseAndBuildPages();
    const auto ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count();
    std::printf("CHAPTER %d result ok=%d pages=%d ms=%lld\n", i, ok ? 1 : 0, pages, (long long)ms);
    if (!ok) rc = 1;
    if (css) css->clear();
    std::remove(hostTmp.c_str());
  }
  std::printf("EXIT %d\n", rc);
  return rc;
}
