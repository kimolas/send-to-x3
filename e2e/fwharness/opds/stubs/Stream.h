#pragma once
#include "Print.h"
// Host stub of Arduino Stream (only what OpdsStream.h needs).
class Stream : public Print {
 public:
  virtual int available() = 0;
  virtual int read() = 0;
  virtual int peek() = 0;
};
