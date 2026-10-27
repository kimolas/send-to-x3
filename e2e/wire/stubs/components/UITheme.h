#pragma once
// Harness stand-in: the event drain only calls GUI.drawPopup (toasts), and only with a
// renderer; the harness passes none, so toasts are reported here if ever drawn.
#include <cstdio>
class GfxRenderer;
struct WireHarnessGui {
  void drawPopup(GfxRenderer&, const char* text) { fprintf(stderr, "[TOAST] %s\n", text); }
};
inline WireHarnessGui GUI;
