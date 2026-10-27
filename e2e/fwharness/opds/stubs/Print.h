#pragma once
#include <cstddef>
#include <cstdint>
// Host stub of Arduino Print. OpdsParser overrides write(uint8_t),
// write(const uint8_t*, size_t) and flush(), so all three must be virtual.
class Print {
 public:
  virtual ~Print() = default;
  virtual size_t write(uint8_t) = 0;
  virtual size_t write(const uint8_t* data, size_t len) {
    size_t n = 0;
    while (n < len && write(data[n]) == 1) ++n;
    return n;
  }
  virtual void flush() {}
};
