#pragma once
// Host GfxRenderer stand-in for CrossPoint 2.x that measures text with the REAL built-in
// reader font metrics (lib/EpdFont/builtinFonts/*.h + lib/EpdFont/EpdFont.cpp), mirroring
// the built-in-font (non-SD) path of lib/GfxRenderer/GfxRenderer.cpp at 3f387a9:
// getTextAdvanceX (ligatures, kerning, differential rounding, sup/sub and small caps),
// getSpaceAdvance, getKerning, getLineHeight, getFontAscenderSize, getCodepointMetrics.
// The API surface follows test/chapter_html_slim_parser/stubs/GfxRenderer.h (MIT,
// CrossPoint Reader organization). No drawing, no bidi reordering, no fallback fonts.
// 2.x ships Noto Sans only; select the size with CP_PT=12|14|16|18 (default 14).
#include <BidiUtils.h>
#include <EpdFontFamily.h>
#include <GfxRenderer/FontVariantProvider.h>
#include <Utf8.h>
#include <builtinFonts/all.h>

#include <cstdlib>
#include <cstring>
#include <map>
#include <string>

namespace BidiUtils {
enum class BidiBaseDir : signed char { AUTO = -1, LTR = 0, RTL = 1 };
}

enum Color : uint8_t { Clear = 0x00, White = 0x01, LightGray = 0x05, DarkGray = 0x0A, Black = 0x10 };

class GfxRenderer {
 public:
  enum class TextMeasureMode { Layout, Rendered };
  class FrameBufferLoan {  // no framebuffer to lend on the host
   public:
    explicit FrameBufferLoan(GfxRenderer&) {}
    void end() {}
  };

  static int trackingBetween(uint32_t l, uint32_t r, int8_t t) {
    auto sp = [](uint32_t c) { return c == ' ' || c == 0xA0 || c == 0x3000; };
    return l == 0 || sp(l) || sp(r) ? 0 : t;
  }
  static int32_t scaleSmallCapAdvance(int32_t advanceFP) { return (advanceFP * 3 + 2) / 4; }

  GfxRenderer() {
    const char* p = std::getenv("CP_PT");
    const int pt = p ? std::atoi(p) : 14;
#define CP_PICK(n)                                                                                  \
  if (pt == n) {                                                                                    \
    r_ = new EpdFont(&notosans_##n##_regular);                                                      \
    b_ = new EpdFont(&notosans_##n##_bold);                                                         \
    i_ = new EpdFont(&notosans_##n##_italic);                                                       \
    bi_ = new EpdFont(&notosans_##n##_bolditalic);                                                  \
  }
    CP_PICK(12) CP_PICK(14) CP_PICK(16) CP_PICK(18)
#undef CP_PICK
    if (!r_) {
      r_ = new EpdFont(&notosans_14_regular);
      b_ = new EpdFont(&notosans_14_bold);
      i_ = new EpdFont(&notosans_14_italic);
      bi_ = new EpdFont(&notosans_14_bolditalic);
    }
    fontMap.emplace(0, EpdFontFamily(r_, b_, i_, bi_));
  }

  // X3 portrait panel 528x792; bezel insets unknown (freeink-sdk BoardConfig).
  int getScreenWidth() const { return 528; }
  int getScreenHeight() const { return 792; }
  bool isFontCacheScanning() const { return false; }
  void drawLine(int, int, int, int, int, bool) const {}
  void drawLine(int, int, int, int, bool = true) const {}
  void fillRect(int, int, int, int, bool = true) const {}
  void fillRectDither(int, int, int, int, Color) const {}
  void drawText(int, int, int, const char*, bool = true, EpdFontFamily::Style = EpdFontFamily::REGULAR,
                BidiUtils::BidiBaseDir = BidiUtils::BidiBaseDir::AUTO, int8_t = 0) const {}
  int drawScaledCodepoint(int fontId, uint32_t cp, EpdFontFamily::Style style, int, int, int scale256) const {
    const EpdGlyph* g = fam(fontId).getGlyph(cp, style);
    return g ? fp4::toPixel(static_cast<int32_t>(g->advanceX) * scale256 / 256) : 0;
  }
  bool getCodepointMetrics(int fontId, uint32_t cp, EpdFontFamily::Style style, int32_t& advanceFP, int& top) const {
    const EpdGlyph* g = fam(fontId).getGlyph(cp, style);
    if (!g) return false;
    advanceFP = g->advanceX;
    top = g->top;
    return true;
  }

  const std::map<int, EpdFontFamily>& getFontMap() const { return fontMap; }
  void setFontVariantProvider(const FontVariantProvider& provider) { provider_ = provider; }
  int resolveFontVariant(int fontId, float scale) const {
    const int id = provider_.resolveFont(fontId, scale);
    return fontMap.count(id) ? id : fontId;
  }
  bool ensureFontLoaded(int fontId) const { return fontMap.count(fontId) != 0; }
  bool isSdCardFont(int) const { return false; }
  void ensureSdCardFontReady(int, const char*, uint8_t = 0x0F, bool = false) const {}
  void ensureSdCardFontReady(int, const char* const*, const size_t*, size_t, bool, bool, uint8_t,
                             bool = false) const {}

  int getLineHeight(int fontId) const { return fam(fontId).getData(EpdFontFamily::REGULAR)->advanceY; }
  int getLineHeight(int fontId, float c) const { return static_cast<int>(getLineHeight(fontId) * c + 0.5f); }
  int getFontAscenderSize(int fontId) const { return fam(fontId).getData(EpdFontFamily::REGULAR)->ascender; }
  int getTextHeight(int fontId) const { return getFontAscenderSize(fontId); }
  int getSpaceWidth(int fontId, EpdFontFamily::Style s = EpdFontFamily::REGULAR) const {
    const EpdGlyph* g = fam(fontId).getGlyph(' ', s);
    return g ? fp4::toPixel(g->advanceX) : 0;
  }
  int getSpaceAdvance(int fontId, uint32_t l, uint32_t r, EpdFontFamily::Style s) const {
    const auto& f = fam(fontId);
    const EpdGlyph* g = f.getGlyph(' ', s);
    const int32_t adv = g ? static_cast<int32_t>(g->advanceX) : 0;
    return fp4::toPixel(adv + static_cast<int32_t>(f.getKerning(l, ' ', s)) +
                        static_cast<int32_t>(f.getKerning(' ', r, s)));
  }
  int getKerning(int fontId, uint32_t l, uint32_t r, EpdFontFamily::Style s, int8_t t = 0) const {
    return fp4::toPixel(fam(fontId).getKerning(l, r, s)) + trackingBetween(l, r, t);
  }
  int getTextWidth(int id, const char* text, EpdFontFamily::Style s,
                   BidiUtils::BidiBaseDir = BidiUtils::BidiBaseDir::AUTO) const {
    return getTextAdvanceX(id, text, s);
  }
  int getTextAdvanceX(int fontId, const char* text, EpdFontFamily::Style style, int8_t tracking = 0,
                      BidiUtils::BidiBaseDir = BidiUtils::BidiBaseDir::AUTO,
                      TextMeasureMode = TextMeasureMode::Layout) const {
    ++measureCalls;
    const auto& font = fam(fontId);
    uint32_t cp, prevCp = 0;
    int widthPx = 0;
    int32_t prevAdvanceFP = 0;
    while ((cp = utf8NextCodepoint(reinterpret_cast<const uint8_t**>(&text)))) {
      if (BidiUtils::isTransparentMark(cp) || utf8IsCombiningMark(cp)) continue;
      const uint32_t smallCapCp = (style & EpdFontFamily::SMALL_CAPS) ? utf8SmallCapsUpper(cp) : cp;
      const bool isSmallCap = smallCapCp != cp;
      if (style & EpdFontFamily::SMALL_CAPS) {
        cp = smallCapCp;
      } else {
        cp = font.applyLigatures(cp, text, style);
      }
      if (prevCp != 0) {
        widthPx += fp4::toPixel(prevAdvanceFP + font.getKerning(prevCp, cp, style)) +
                   trackingBetween(prevCp, cp, tracking);
      }
      EpdGlyph solidFallback;
      const EpdGlyph* glyph = font.getGlyphMetrics(cp, solidFallback, style);
      prevAdvanceFP = glyph ? glyph->advanceX : 0;
      if ((style & (EpdFontFamily::SUP | EpdFontFamily::SUB)) != 0) {
        prevAdvanceFP = (prevAdvanceFP + 1) / 2;
      } else if (isSmallCap) {
        prevAdvanceFP = scaleSmallCapAdvance(prevAdvanceFP);
      }
      prevCp = cp;
    }
    return widthPx + fp4::toPixel(prevAdvanceFP);
  }
  bool hasGlyph(uint32_t cp) const { return fam(0).hasCodepoint(cp, EpdFontFamily::REGULAR); }
  mutable unsigned long long measureCalls = 0;

 private:
  const EpdFontFamily& fam(int fontId) const {
    const auto it = fontMap.find(fontId);
    return it != fontMap.end() ? it->second : fontMap.begin()->second;
  }
  EpdFont *r_ = nullptr, *b_ = nullptr, *i_ = nullptr, *bi_ = nullptr;
  std::map<int, EpdFontFamily> fontMap;
  FontVariantProvider provider_;
};
