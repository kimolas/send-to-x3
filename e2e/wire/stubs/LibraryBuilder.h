#pragma once
// Harness stand-in for lib/LibraryIndex: counts "library is stale" marks instead of
// writing /.crosspoint/library.dirty. isLibraryBookFile mirrors LibraryBuilder.cpp
// (book extensions, case-insensitive, no hidden path segment).
#include <cctype>
#include <string_view>
namespace library {
inline int& dirtyMarks() {
  static int n = 0;
  return n;
}
inline bool markLibraryIndexDirty() {
  dirtyMarks()++;
  return true;
}
inline bool isLibraryBookFile(std::string_view p) {
  if (p.find("/.") != std::string_view::npos) return false;
  for (const char* ext : {".epub", ".txt", ".md", ".xtc", ".xtch"}) {
    const std::string_view e(ext);
    if (p.size() < e.size()) continue;
    bool same = true;
    for (size_t i = 0; i < e.size(); i++) {
      if (std::tolower(static_cast<unsigned char>(p[p.size() - e.size() + i])) != e[i]) same = false;
    }
    if (same) return true;
  }
  return false;
}
}  // namespace library
