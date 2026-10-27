// Host harness: feeds an OPDS feed through CrossPoint 1.6.5's OpdsParser exactly
// like OpdsBookBrowserActivity::fetchFeed does, then prints what the device would
// list, which URL it would request for each row, and the on-disk filename.
#include <OpdsParser.h>
#include <OpdsStream.h>

#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "OpdsFilename.h"
#include "UrlUtils.h"

int main(int argc, char** argv) {
  if (argc < 2) { std::fprintf(stderr, "usage: %s feed.xml [serverUrl] [currentPath] [chunk]\n", argv[0]); return 2; }
  std::ifstream f(argv[1], std::ios::binary);
  std::stringstream ss; ss << f.rdbuf();
  const std::string xml = ss.str();
  const std::string serverUrl = argc > 2 ? argv[2] : "http://homeassistant.local:8099/opds";
  const std::string currentPath = argc > 3 ? argv[3] : "";
  const size_t chunk = argc > 4 ? std::stoul(argv[4]) : 2048;  // SecureHttpClient READ_CHUNK

  OpdsParser parser;
  {
    OpdsParserStream stream{parser};
    for (size_t off = 0; off < xml.size(); off += chunk) {
      const size_t n = std::min(chunk, xml.size() - off);
      stream.write(reinterpret_cast<const uint8_t*>(xml.data() + off), n);
    }
  }  // ~OpdsParserStream -> parser.flush()
  if (!parser) { std::printf("RESULT: PARSE_FAILED (\"Failed to parse feed\")\n"); return 1; }
  const std::string feedUrl = UrlUtils::buildUrl(serverUrl, currentPath);
  std::printf("feedUrl=%s\nsearchTemplate=%s\nnext=%s\nprev=%s\ntruncated=%d\n", feedUrl.c_str(),
              parser.getSearchTemplate().c_str(), parser.getNextPageUrl().c_str(), parser.getPrevPageUrl().c_str(),
              parser.truncated());
  if (!parser.getPrevPageUrl().empty())
    std::printf("[NAV ] \"« Previous Page\" -> %s\n", UrlUtils::buildUrl(feedUrl, parser.getPrevPageUrl()).c_str());
  for (const auto& e : parser.getEntries()) {
    if (e.type == OpdsEntryType::BOOK) {
      std::printf("[BOOK] title=\"%s\" author=\"%s\" id=\"%s\"\n       GET %s\n       file /%s\n", e.title.c_str(),
                  e.author.c_str(), e.id.c_str(), UrlUtils::buildUrl(feedUrl, e.href).c_str(),
                  opdsBookFilename(e.author, e.title, OpdsFilenameFormat::AuthorTitle).c_str());
    } else {
      std::printf("[NAV ] title=\"%s\" -> %s\n", e.title.c_str(), UrlUtils::buildUrl(feedUrl, e.href).c_str());
    }
  }
  if (!parser.getNextPageUrl().empty())
    std::printf("[NAV ] \"Next Page »\" -> %s\n", UrlUtils::buildUrl(feedUrl, parser.getNextPageUrl()).c_str());
  if (parser.getEntries().empty() && parser.getNextPageUrl().empty() && parser.getPrevPageUrl().empty())
    std::printf("RESULT: NO_ENTRIES (\"No entries found\" error screen)\n");
  else
    std::printf("RESULT: OK (%zu rows)\n", parser.getEntries().size() + !parser.getNextPageUrl().empty() + !parser.getPrevPageUrl().empty());
  return 0;
}
