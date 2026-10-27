// Host harness for CrossPoint 2.x .txt files. 2.x has no TxtReaderActivity: the Epub class
// opens .txt/.md itself (Txt::buildTxtCache: one spine item, TOC label = file name, language
// "en"), streams the text through TxtToHtml and lays it out with the EPUB chapter parser.
// This runs exactly that path with real font metrics.
//
// Usage: CP_SDROOT=<sd dir> txt_dump /path/on/sd/file.txt [viewportW=518] [viewportH=768]
// Prints "TITLE (status bar / library / recent) = [..]", the pages, and
// "totalPages=N indexMs=M"; exit 0 only if the file loads and parses.
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
    std::fprintf(stderr, "usage: %s /file.txt [w] [h]\n", argv[0]);
    return 2;
  }
  const std::string path = argv[1];
  const uint16_t w = argc > 2 ? std::atoi(argv[2]) : 518;
  const uint16_t h = argc > 3 ? std::atoi(argv[3]) : 768;
  Storage.mkdir("/.crosspoint");
  auto book = std::make_shared<Epub>(path, "/.crosspoint");
  book->clearCache();
  const auto t0 = std::chrono::steady_clock::now();
  if (!book->load(true, false)) {
    std::printf("LOAD FAILED\nEXIT 1\n");
    return 1;
  }
  std::string tocTitle = book->getTocItemsCount() > 0 ? book->getTocItem(0).title : "";
  std::printf("TITLE (status bar / library / recent) = [%s]\n", book->getTitle().c_str());
  std::printf("TOC title=[%s] lang=[%s] spine=%d\n", tocTitle.c_str(), book->getLanguage().c_str(),
              book->getSpineItemsCount());
  GfxRenderer renderer;
  Hyphenator::setPreferredLanguage(book->getLanguage());
  int rc = book->getSpineItemsCount() == 1 ? 0 : 1;
  int pages = 0;
  for (int i = 0; i < book->getSpineItemsCount(); i++) {
    const auto s = book->getSpineItem(i);
    const std::string tmp = "/.crosspoint/harness_txt_" + std::to_string(i) + ".html";
    if (!book->extractItemToFile(s.href, tmp)) {
      std::printf("CHAPTER %d extract FAILED\n", i);
      rc = 1;
      continue;
    }
    auto onPage = [&](std::unique_ptr<Page> page, uint16_t, uint16_t, uint32_t) {
      std::printf("-- page %d\n", pages++);
      if (!page) return;
      for (const auto& el : page->elements) {
        if (el->getTag() != TAG_PageLine) continue;
        const auto* b = static_cast<const PageLine&>(*el).getBlock();
        std::printf("  |");
        for (uint16_t k = 0; k < b->wordCount(); ++k) std::printf(" %s", b->wordText(k));
        std::printf("\n");
      }
    };
    CssParser* css = book->getCssParser();
    ChapterHtmlSlimParser parser(book, tmp, renderer, 0, 1.0f, true, 0, w, h, false, false, onPage, true, "", "",
                                 0, {}, nullptr, css);
    if (!parser.parseAndBuildPages()) rc = 1;
    std::remove(HalStorage::map(tmp).c_str());
  }
  const auto ms =
      std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count();
  std::printf("totalPages=%d indexMs=%lld\n", pages, (long long)ms);
  std::printf("EXIT %d\n", rc);
  return rc;
}
