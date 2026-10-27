#pragma once
// Firmware log macros -> stderr, in the device's "[LEVEL] [TAG] message" shape.
#include <cstdio>
#define LOG_ERR(tag, fmt, ...) fprintf(stderr, "[ERR] [%s] " fmt "\n", tag, ##__VA_ARGS__)
#define LOG_INF(tag, fmt, ...) fprintf(stderr, "[INF] [%s] " fmt "\n", tag, ##__VA_ARGS__)
#define LOG_DBG(tag, fmt, ...) fprintf(stderr, "[DBG] [%s] " fmt "\n", tag, ##__VA_ARGS__)
