#pragma once
// Host GfxRenderer stand-in that measures text with CrossPoint's REAL built-in reader font
// metrics (lib/EpdFont/builtinFonts/*.h + lib/EpdFont/EpdFont.cpp), mirroring the non-SD path of
// GfxRenderer::getTextAdvanceX / getSpaceAdvance / getKerning / getLineHeight
// (lib/GfxRenderer/GfxRenderer.cpp:2060-2210). No drawing, no bidi reordering (LTR only).
// Select the face with env CP_FONT=serif|sans and CP_PT=12|14|16|18. Defaults are the device
// defaults: Noto Serif 14 on 1.x (CrossPointSettings.h:116,288); 2.x ships only Noto Sans.
#include <EpdFontFamily.h>
#include <Utf8.h>
#include <builtinFonts/all.h>
#include <cstdlib>
#include <cstring>
#include <string>

namespace BidiUtils { enum class BidiBaseDir : signed char { AUTO = -1, LTR = 0, RTL = 1 }; }

class FontCacheManager {
 public:
  struct Scope { void endScanAndPrewarm() {} };
  Scope createPrewarmScope() { return {}; }
};

class GfxRenderer {
 public:
  enum class TextMeasureMode { Layout, Rendered };
  static int trackingBetween(uint32_t l, uint32_t r, int8_t t) {
    auto sp = [](uint32_t c) { return c == ' ' || c == 0xA0 || c == 0x3000; };
    return l == 0 || sp(l) || sp(r) ? 0 : t;
  }
  GfxRenderer() {
    const char* f = std::getenv("CP_FONT");
    const char* p = std::getenv("CP_PT");
    const int pt = p ? std::atoi(p) : 14;
#define CP_PICK(fam, n) \
    if (pt == n) { r_ = new EpdFont(&fam##_##n##_regular); b_ = new EpdFont(&fam##_##n##_bold); \
                   i_ = new EpdFont(&fam##_##n##_italic); bi_ = new EpdFont(&fam##_##n##_bolditalic); }
#if __has_include(<builtinFonts/notoserif_14_regular.h>)
    const bool sans = f && std::strcmp(f, "sans") == 0;
    if (sans) { CP_PICK(notosans, 12) CP_PICK(notosans, 14) CP_PICK(notosans, 16) CP_PICK(notosans, 18) }
    else { CP_PICK(notoserif, 12) CP_PICK(notoserif, 14) CP_PICK(notoserif, 16) CP_PICK(notoserif, 18) }
    if (!r_) { r_ = new EpdFont(&notoserif_14_regular); b_ = new EpdFont(&notoserif_14_bold);
               i_ = new EpdFont(&notoserif_14_italic); bi_ = new EpdFont(&notoserif_14_bolditalic); }
#else
    (void)f;
    CP_PICK(notosans, 12) CP_PICK(notosans, 14) CP_PICK(notosans, 16) CP_PICK(notosans, 18)
    if (!r_) { r_ = new EpdFont(&notosans_14_regular); b_ = new EpdFont(&notosans_14_bold);
               i_ = new EpdFont(&notosans_14_italic); bi_ = new EpdFont(&notosans_14_bolditalic); }
#endif
#undef CP_PICK
    fam_ = new EpdFontFamily(r_, b_, i_, bi_);
  }
  // Screen size is only used by TXT; X3 portrait panel is 528x792 (USER_GUIDE.md:568).
  int screenW = 528, screenH = 792;
  int insetT = 0, insetR = 0, insetB = 0, insetL = 0;  // board bezel insets: unknown here (freeink-sdk BoardConfig)
  int getScreenWidth() const { return screenW; }
  int getScreenHeight() const { return screenH; }
  void getOrientedViewableTRBL(int* t, int* r, int* b, int* l) const { *t = insetT; *r = insetR; *b = insetB; *l = insetL; }
  bool isFontCacheScanning() const { return false; }
  FontCacheManager* getFontCacheManager() { return &fcm_; }
  void drawLine(int, int, int, int, int, bool) const {}
  void drawLine(int, int, int, int, bool = true) const {}
  void drawText(int, int, int, const char*, bool = true, EpdFontFamily::Style = EpdFontFamily::REGULAR,
                BidiUtils::BidiBaseDir = BidiUtils::BidiBaseDir::AUTO, int8_t = 0) const {}
  void drawCenteredText(int, int, const char*, bool = true, EpdFontFamily::Style = EpdFontFamily::REGULAR) const {}
  void clearScreen() const {}
  void displayBuffer() const {}
  int getLineHeight(int) const { return fam_->getData()->advanceY; }
  int getLineHeight(int id, float c) const { return static_cast<int>(getLineHeight(id) * c + 0.5f); }
  int getFontAscenderSize(int) const { return fam_->getData()->ascender; }
  int getSpaceWidth(int, EpdFontFamily::Style s = EpdFontFamily::REGULAR) const {
    const EpdGlyph* g = fam_->getGlyph(' ', s); return g ? fp4::toPixel(g->advanceX) : 0;
  }
  int getSpaceAdvance(int, uint32_t l, uint32_t r, EpdFontFamily::Style s) const {
    const EpdGlyph* g = fam_->getGlyph(' ', s);
    const int32_t adv = g ? g->advanceX : 0;
    return fp4::toPixel(adv + fam_->getKerning(l, ' ', s) + fam_->getKerning(' ', r, s));
  }
  int getKerning(int, uint32_t l, uint32_t r, EpdFontFamily::Style s, int8_t t = 0) const {
    return fp4::toPixel(fam_->getKerning(l, r, s)) + trackingBetween(l, r, t);
  }
  int getTextWidth(int id, const char* text, EpdFontFamily::Style s,
                   BidiUtils::BidiBaseDir = BidiUtils::BidiBaseDir::AUTO) const { return getTextAdvanceX(id, text, s); }
  int getTextAdvanceX(int, const char* text, EpdFontFamily::Style style, int8_t tracking = 0,
                      BidiUtils::BidiBaseDir = BidiUtils::BidiBaseDir::AUTO,
                      TextMeasureMode = TextMeasureMode::Layout) const {
    ++measureCalls;
    uint32_t cp, prevCp = 0; int widthPx = 0; int32_t prevAdv = 0;
    while ((cp = utf8NextCodepoint(reinterpret_cast<const uint8_t**>(&text)))) {
      if (utf8IsCombiningMark(cp)) continue;
      cp = fam_->applyLigatures(cp, text, style);
      if (prevCp != 0) widthPx += fp4::toPixel(prevAdv + fam_->getKerning(prevCp, cp, style)) + trackingBetween(prevCp, cp, tracking);
      const EpdGlyph* g = fam_->getGlyph(cp, style);
      prevAdv = g ? g->advanceX : 0;
      if ((style & (EpdFontFamily::SUP | EpdFontFamily::SUB)) != 0) prevAdv = (prevAdv + 1) / 2;
      prevCp = cp;
    }
    return widthPx + fp4::toPixel(prevAdv);
  }
  bool hasGlyph(uint32_t cp) const { return fam_->hasCodepoint(cp, EpdFontFamily::REGULAR); }
  bool isSdCardFont(int) const { return false; }
  void ensureSdCardFontReady(int, const char*, uint8_t = 0x0F) const {}
  void ensureSdCardFontReady(int, const char* const*, const size_t*, size_t, bool, bool, uint8_t) const {}
  mutable unsigned long long measureCalls = 0;
 private:
  EpdFont *r_ = nullptr, *b_ = nullptr, *i_ = nullptr, *bi_ = nullptr;
  EpdFontFamily* fam_ = nullptr;
  FontCacheManager fcm_;
};
