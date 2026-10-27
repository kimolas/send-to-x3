#pragma once
// Minimal stand-in for src/activities/reader/ReaderActivity.h (no Activity framework).
#include <GfxRenderer.h>
#include <string>
#include "util/ScreenshotInfo.h"
struct MappedInputManager {
  struct Labels { const char *btn1 = "", *btn2 = "", *btn3 = "", *btn4 = ""; };
  Labels mapLabels(const char*, const char*, const char*, const char*) const { return {}; }
};
class ReaderActivity {
 protected:
  GfxRenderer& renderer;
  MappedInputManager& mappedInput;
  std::string bookPath;
  int pagesUntilFullRefresh = 0;
  void markPageRendered() {}
  ReaderActivity(const char*, GfxRenderer& r, MappedInputManager& m, std::string p, bool)
      : renderer(r), mappedInput(m), bookPath(std::move(p)) {}
  virtual bool loadBook() = 0;
  virtual std::string getBookTitle() const = 0;
  virtual void renderBook() = 0;
 public:
  virtual ~ReaderActivity() = default;
  virtual bool pageTurn(bool) = 0;
  virtual bool skipPages(int) { return false; }
  virtual bool isAtEndOfBook() const = 0;
  virtual void onReturnFromEndOfBook() {}
  virtual ScreenshotInfo getScreenshotInfo() const { return {}; }
};
