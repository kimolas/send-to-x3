#pragma once
#include <GfxRenderer.h>
#include <cstdlib>
#include <string>
// Status bar reservation: 19 px with the default spec (BaseTheme.h:195/LyraTheme.h:52 statusBarVerticalMargin,
// UITheme.cpp:148-156). Override with CP_STATUSBAR.
class UITheme {
 public:
  static UITheme& getInstance() { static UITheme t; return t; }
  int getStatusBarHeight() const { return std::getenv("CP_STATUSBAR") ? std::atoi(std::getenv("CP_STATUSBAR")) : 19; }
};
struct GuiStub {
  void drawPopup(GfxRenderer&, const char*) {}
  void drawButtonHints(GfxRenderer&, const char*, const char*, const char*, const char*) {}
  void drawStatusBar(GfxRenderer&, float, int, int, const std::string&) {}
};
inline GuiStub GUI;
