#pragma once
// Minimal stand-in for src/CrossPointSettings.h: only what TxtReaderActivity.cpp reads.
// Defaults mirror src/CrossPointSettings.h:265,294,302-305,235 (env overrides for experiments).
#include <cstdint>
#include <cstdlib>
class CrossPointSettings {
 public:
  enum PARAGRAPH_ALIGNMENT { JUSTIFIED = 0, LEFT_ALIGN = 1, CENTER_ALIGN = 2, RIGHT_ALIGN = 3, BOOK_STYLE = 4 };
  struct StatusBarSpec { bool title = true; bool showsTitle() const { return title; } };
  uint8_t screenMargin = (uint8_t)(std::getenv("CP_MARGIN") ? std::atoi(std::getenv("CP_MARGIN")) : 5);
  uint8_t paragraphAlignment = (uint8_t)(std::getenv("CP_ALIGN") ? std::atoi(std::getenv("CP_ALIGN")) : JUSTIFIED);
  uint8_t textAntiAliasing = 0;
  int getReaderFontId() const { return 1; }
  StatusBarSpec statusBarSpec() const { return {}; }
};
inline CrossPointSettings SETTINGS;
