#pragma once
// Harness stand-in: PluginLocations only reads SETTINGS.pluginsEnabled.
#include <cstdint>
struct WireHarnessSettings {
  uint8_t pluginsEnabled = 1;
};
inline WireHarnessSettings SETTINGS;
