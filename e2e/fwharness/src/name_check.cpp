// Host harness: run CrossPoint's real file-name code over a list of names.
//
// stdin: one name per line, hex-encoded UTF-8 (so any byte, including '\n', can be tested).
// stdout, one tab-separated line per input:
//   sanitizeFilename(name)                                  hex   (1.x and 2.x)
//   opdsBookFilename("", name, TitleOnly)                    hex   (OPDS download name)
//   sanitizeFilenamePreservingExtension(name + ".epub")       hex   ("-" on 1.x)
//   sanitizeFilenamePreservingExtension(name + ".txt")        hex   ("-" on 1.x)
//   2.x catalog download accepts "<.epub name>" into /Inbox  0/1   ("-" on 1.x)
//   2.x catalog download accepts "<.txt name>" into /Inbox   0/1   ("-" on 1.x)
// The acceptance rule mirrors PluginCatalogActivity.cpp (2.x): non-empty, no "..", no '/',
// and protectedpaths::isPluginPath("/Inbox/<name>").
#include <cstdio>
#include <iostream>
#include <string>

#include "OpdsFilename.h"
#include "StringUtils.h"
#ifdef CP_HAS_PROTECTED_PATHS
#include "ProtectedPaths.h"
#endif

namespace {

std::string hex(const std::string& s) {
  static const char* digits = "0123456789abcdef";
  std::string out;
  out.reserve(s.size() * 2);
  for (unsigned char c : s) {
    out += digits[c >> 4];
    out += digits[c & 15];
  }
  return out;
}

bool unhex(const std::string& in, std::string& out) {
  auto val = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  if (in.size() % 2) return false;
  out.clear();
  for (size_t i = 0; i < in.size(); i += 2) {
    const int hi = val(in[i]), lo = val(in[i + 1]);
    if (hi < 0 || lo < 0) return false;
    out += static_cast<char>(hi * 16 + lo);
  }
  return true;
}

#if defined(CP_HAS_PRESERVING_EXTENSION) && defined(CP_HAS_PROTECTED_PATHS)
bool catalogAccepts(const std::string& filename) {
  if (filename.empty() || filename.find("..") != std::string::npos || filename.find('/') != std::string::npos) {
    return false;
  }
  return protectedpaths::isPluginPath("/Inbox/" + filename);
}
#endif

}  // namespace

int main() {
  std::string line, name;
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (!unhex(line, name)) {
      std::printf("ERR\n");
      continue;
    }
    std::printf("%s\t%s", hex(StringUtils::sanitizeFilename(name)).c_str(),
                hex(opdsBookFilename("", name, OpdsFilenameFormat::TitleOnly)).c_str());
#if defined(CP_HAS_PRESERVING_EXTENSION) && defined(CP_HAS_PROTECTED_PATHS)
    const std::string e = StringUtils::sanitizeFilenamePreservingExtension(name + ".epub");
    const std::string t = StringUtils::sanitizeFilenamePreservingExtension(name + ".txt");
    std::printf("\t%s\t%s\t%d\t%d", hex(e).c_str(), hex(t).c_str(), catalogAccepts(e) ? 1 : 0,
                catalogAccepts(t) ? 1 : 0);
#else
    std::printf("\t-\t-\t-\t-");
#endif
    std::printf("\n");
  }
  return 0;
}
