#pragma once
// Arduino Client interface (same shape as freeink-sdk SecureNet/test/host/stubs/Client.h).
#include <Arduino.h>
#ifndef WIRE_HARNESS_IPADDRESS
#define WIRE_HARNESS_IPADDRESS
class IPAddress {};  // only the type name is needed; connect(IPAddress, port) is unused
#endif
class Client {
 public:
  virtual ~Client() = default;
  virtual int connect(IPAddress ip, uint16_t port) = 0;
  virtual int connect(const char* host, uint16_t port) = 0;
  virtual size_t write(uint8_t b) = 0;
  virtual size_t write(const uint8_t* buf, size_t size) = 0;
  virtual int available() = 0;
  virtual int read() = 0;
  virtual int read(uint8_t* buf, size_t size) = 0;
  virtual int peek() = 0;
  virtual void flush() = 0;
  virtual void stop() = 0;
  virtual uint8_t connected() = 0;
  virtual operator bool() = 0;
  void setTimeout(unsigned long) {}
};
