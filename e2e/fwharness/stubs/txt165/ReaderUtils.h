#pragma once
#include <GfxRenderer.h>
namespace ReaderUtils {
inline void displayWithRefreshCycle(const GfxRenderer&, int&, bool = false) {}
inline void displayBaseWithRefreshCycle(const GfxRenderer&, int&) {}
template <typename F> void renderAntiAliased(GfxRenderer&, F&&) {}
}
