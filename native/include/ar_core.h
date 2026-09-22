/* aura-redis C data plane — loaded via Aura std/ffi (c-load / c-func). */
#ifndef AURA_REDIS_AR_CORE_H
#define AURA_REDIS_AR_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ArCore ArCore;

typedef struct ArEvictOps {
  void (*on_get)(ArCore* db, void* entry);
  void (*on_set)(ArCore* db, void* entry);
  int (*should_evict)(ArCore* db);
  int (*evict_one)(ArCore* db);
  const char* name;
} ArEvictOps;

/* Lifecycle */
ArCore* ar_core_create(void);
void ar_core_destroy(ArCore* core);

/* TCP (Iteration 2+): bind 127.0.0.1:port (loopback only; documented).
 * Returns 1 on success, 0 on error (Aura FFI-friendly). */
int ar_core_listen(ArCore* core, int port);
/* Pump event loop up to ms milliseconds (0 = process ready events once).
 * Returns 0 normally, 1 if quit requested, -1 on fatal error. */
int ar_core_serve_ms(ArCore* core, int ms);
/* Blocking serve until listen socket closed or fatal error. Returns 0. */
int ar_core_serve_forever(ArCore* core);

/* In-process string KV (also used by TCP command path). */
int ar_set(ArCore* core, const char* key, const char* val);
/* Binary-safe SET; returns 1 on ok. */
int ar_set_bin(ArCore* core, const char* key, size_t klen, const char* val,
               size_t vlen);
/* Returns malloc'd value (caller frees via ar_free) or NULL if missing. */
char* ar_get(ArCore* core, const char* key);
/* Binary get: *out_len set; caller frees via ar_free. NULL if missing. */
char* ar_get_bin(ArCore* core, const char* key, size_t klen, size_t* out_len);
int ar_del(ArCore* core, const char* key);
int ar_del_bin(ArCore* core, const char* key, size_t klen);
int ar_exists(ArCore* core, const char* key);
int ar_exists_bin(ArCore* core, const char* key, size_t klen);
void ar_free(char* p);
int ar_get_eq(ArCore* core, const char* key, const char* expect);

/* Extra commands (Iteration 3+) */
int64_t ar_incr(ArCore* core, const char* key, size_t klen, int64_t delta,
                int* ok);
void ar_flushdb(ArCore* core);
/* MSET: nkeys pairs of (k,klen,v,vlen) interleaved in arrays. Returns 1. */
int ar_mset(ArCore* core, size_t n, const char** keys, const size_t* klens,
            const char** vals, const size_t* vlens);

int ar_ping(ArCore* core);

/* Eviction */
int ar_core_set_evict_by_name(ArCore* core, const char* name);
const char* ar_core_evict_name(ArCore* core);
int ar_core_set_maxmemory(ArCore* core, uint64_t bytes);
uint64_t ar_core_maxmemory(ArCore* core);
uint64_t ar_core_used_memory(ArCore* core);

/* Metrics */
uint64_t ar_metric_ops(ArCore* core);
uint64_t ar_metric_gets(ArCore* core);
uint64_t ar_metric_sets(ArCore* core);
uint64_t ar_metric_hits(ArCore* core);
uint64_t ar_metric_misses(ArCore* core);
uint64_t ar_metric_evicted(ArCore* core);
uint64_t ar_metric_expired(ArCore* core);

/* Iteration 7 — hot-load eviction plugin .so
 * Plugin must export: const ArEvictOps* ar_plugin_evict_ops(void);
 * Helpers for plugin authors (also used by built-in random plugin): */
int ar_core_over_maxmemory(ArCore* core);
int ar_core_evict_random_one(ArCore* core);
int ar_core_load_evict_plugin(ArCore* core, const char* so_path);


#ifdef __cplusplus
}
#endif
#endif
