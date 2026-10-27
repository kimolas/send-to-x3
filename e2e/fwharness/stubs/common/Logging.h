#pragma once
#include <cstdio>
#include <cstdlib>
// Set CP_LOG=1 to see firmware log lines on stderr.
inline bool cpLogOn() { static int on = std::getenv("CP_LOG") ? 1 : 0; return on; }
#define CP_LOG_(lvl, tag, ...) do { if (cpLogOn()) { std::fprintf(stderr, "[%s][%s] ", lvl, tag); std::fprintf(stderr, __VA_ARGS__); std::fprintf(stderr, "\n"); } } while (0)
#define LOG_DBG(tag, ...) CP_LOG_("DBG", tag, __VA_ARGS__)
#define LOG_INF(tag, ...) CP_LOG_("INF", tag, __VA_ARGS__)
#define LOG_ERR(tag, ...) CP_LOG_("ERR", tag, __VA_ARGS__)
