#pragma once
#include <cstdio>
#ifdef OPDS_HOST_VERBOSE
#define LOG_DBG(origin, fmt, ...) std::fprintf(stderr, "[DBG] [%s] " fmt "\n", origin, ##__VA_ARGS__)
#else
#define LOG_DBG(...) ((void)0)
#endif
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
