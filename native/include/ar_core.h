/* aura-redis C data plane — Iteration 1 skeleton
 * Loaded via Aura std/ffi (c-load / c-func). No Redis-shaped Aura prims.
 */
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

/* In-process string KV (Iteration 1 — no TCP yet) */
/* Returns 1 on ok, 0 on error. */
int ar_set(ArCore* core, const char* key, const char* val);
/* Returns malloc'd value (caller frees via ar_free) or NULL if missing. */
char* ar_get(ArCore* core, const char* key);
int ar_del(ArCore* core, const char* key);
int ar_exists(ArCore* core, const char* key);
void ar_free(char* p);
/* 1 if key exists and value equals expect, 0 otherwise (no malloc to caller). */
int ar_get_eq(ArCore* core, const char* key, const char* expect);

/* Health: returns 1 */
int ar_ping(ArCore* core);

/* Eviction (Iteration 1: noop only) */
int ar_core_set_evict_by_name(ArCore* core, const char* name);
const char* ar_core_evict_name(ArCore* core);

/* Metrics */
uint64_t ar_metric_ops(ArCore* core);
uint64_t ar_metric_gets(ArCore* core);
uint64_t ar_metric_sets(ArCore* core);
uint64_t ar_metric_hits(ArCore* core);
uint64_t ar_metric_misses(ArCore* core);

#ifdef __cplusplus
}
#endif
#endif
