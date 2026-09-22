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

/* TCP: bind bind_addr:port (default 127.0.0.1). Returns 1 on success, 0 on error. */
int ar_core_listen(ArCore* core, int port);
/* Pump event loop up to ms milliseconds (0 = process ready events once).
 * Returns 0 normally, 1 if quit requested, -1 on fatal error. */
int ar_core_serve_ms(ArCore* core, int ms);
/* Blocking serve until quit/shutdown or fatal error. Returns 0 on clean exit. */
int ar_core_serve_forever(ArCore* core);
/* P0.4 — AUTH / bind / protected-mode */
int ar_core_set_requirepass(ArCore* core, const char* pass); /* NULL/"" clears */
const char* ar_core_requirepass(ArCore* core); /* may be NULL */
int ar_core_set_bind(ArCore* core, const char* addr); /* e.g. 127.0.0.1 / 0.0.0.0 */
const char* ar_core_bind_addr(ArCore* core);
int ar_core_set_protected_mode(ArCore* core, int on); /* 1=on 0=off; default 1 */
int ar_core_protected_mode(ArCore* core);
/* P0.5 — request graceful shutdown (also from SIGTERM/SIGINT when installed). */
void ar_core_request_shutdown(ArCore* core);
void ar_core_install_signal_handlers(ArCore* core);

/* In-process string KV (also used by TCP command path). */
int ar_set(ArCore* core, const char* key, const char* val);
/* Binary-safe SET; returns 1 on ok. */
int ar_set_bin(ArCore* core, const char* key, size_t klen, const char* val,
               size_t vlen);
/* M9: SET with EX seconds (expire_sec<=0 clears TTL). */
int ar_set_bin_ex(ArCore* core, const char* key, size_t klen, const char* val,
                  size_t vlen, int64_t expire_sec);
/* EXPIRE key seconds — 1 if key exists, 0 else. */
int ar_expire(ArCore* core, const char* key, size_t klen, int64_t seconds);
/* TTL: -2 missing, -1 no expire, else remaining seconds. */
int64_t ar_ttl(ArCore* core, const char* key, size_t klen);
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
uint64_t ar_core_keys_with_ttl(ArCore* core);
/* Average remaining TTL ms among keys with expire; 0 if none. */
uint64_t ar_core_avg_ttl_ms(ArCore* core);

/* Iteration 7 — hot-load / live-reload eviction plugin .so
 * Plugin must export: const ArEvictOps* ar_plugin_evict_ops(void);
 * Safe to call while listen socket is up (between commands / serve_ms ticks).
 * Swaps vtable then dlclose(old); single-threaded — no reconnect storm.
 * Helpers for plugin authors (also used by sample plugins): */
int ar_core_over_maxmemory(ArCore* core);
int ar_core_evict_random_one(ArCore* core);
int ar_core_load_evict_plugin(ArCore* core, const char* so_path);
uint64_t ar_metric_plugin_reloads(ArCore* core);
int ar_core_has_evict_plugin(ArCore* core); /* 1 if current ops from dlopen */

/* Iteration 8 — dict layout evolution
 * Names: "flat" (alias "flat_hash"), "hot_cold" (alias "hot-cold").
 * Migrate is synchronous and single-threaded-safe between commands
 * (layout_busy / generation). May block briefly while re-linking entries. */
int ar_core_set_layout(ArCore* core, const char* name);
const char* ar_core_layout_name(ArCore* core);
uint64_t ar_core_layout_gen(ArCore* core);
uint64_t ar_core_hot_keys(ArCore* core);
uint64_t ar_core_cold_keys(ArCore* core);
uint64_t ar_metric_promotions(ArCore* core);
uint64_t ar_metric_demotions(ArCore* core);
uint64_t ar_metric_migrates(ArCore* core);
/* Simple rule: nkeys>=500 && GET-heavy → hot_cold; tiny store → flat.
 * Returns 1 if a migrate ran. Safe to call from Aura adaptive tick. */
int ar_core_adapt_layout(ArCore* core);

/* MVP M4 — eviction sample size + hot-key pin set */
int ar_core_set_evict_samples(ArCore* core, int n); /* clamp 1..256; default 16 */
int ar_core_evict_samples(ArCore* core);
int ar_core_pin(ArCore* core, const char* key, size_t klen);   /* 1 if newly pinned / already */
int ar_core_unpin(ArCore* core, const char* key, size_t klen); /* 1 if was pinned */
uint64_t ar_core_pinned_keys(ArCore* core);
/* Write up to max_out pinned key names into out_keys (malloc'd; caller frees each
 * via ar_free). Returns count written. */
size_t ar_core_list_pinned(ArCore* core, char** out_keys, size_t max_out);
uint64_t ar_core_nkeys(ArCore* core);

/* P0.3 — active expire sampling (Redis-ish). Walks random buckets and frees
 * keys past expire_at. Returns number expired this call. effort ≈ samples. */
int ar_core_active_expire(ArCore* core, int effort);

/* M12 — per-prefix policy namespace hints */
int ar_core_policy_set(ArCore* core, const char* prefix, const char* profile);
/* Write "pfx:=prof;..." into buf; returns length (excl NUL). */
int ar_core_policy_hints(ArCore* core, char* buf, size_t buflen);

#ifdef __cplusplus
}
#endif
#endif
