#pragma once
#include <chrono>
#include <cstdint>
struct EspHostStub { uint32_t getFreeHeap() const { return 300u * 1024u; } uint32_t getMaxAllocHeap() const { return 200u * 1024u; } };
inline EspHostStub ESP;
inline unsigned long millis() {
  using namespace std::chrono;
  return (unsigned long)duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}
inline void delay(uint32_t) {}
inline void vTaskDelay(uint32_t) {}
