#pragma once
// Host stub: no image decoding in the harness (covers only; articles carry no images).
#include <HalStorage.h>
class Print;
class GifToBmpConverter {
 public:
  static bool gifFileToBmpStream(HalFile&, Print&, bool = true, bool = false) { return false; }
  static bool gifFileTo1BitBmpStreamWithSize(HalFile&, Print&, int, int) { return false; }
};
