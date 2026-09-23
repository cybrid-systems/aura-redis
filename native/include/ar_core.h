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
/* P1.12 — client limits */
int ar_core_set_maxclients(ArCore* core, int n); /* 1..AR_MAX_CONN */
int ar_core_maxclients(ArCore* core);
int ar_core_set_timeout(ArCore* core, int sec); /* 0=disabled */
int ar_core_timeout(ArCore* core);
int ar_core_set_tcp_backlog(ArCore* core, int n); /* ≥1; applied on next listen */
int ar_core_tcp_backlog(ArCore* core);
int ar_core_set_slowlog_slower_than(ArCore* core, int us);
int ar_core_slowlog_slower_than(ArCore* core);

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
/* SET with PX milliseconds (expire_ms<=0 clears TTL). */
int ar_set_bin_px(ArCore* core, const char* key, size_t klen, const char* val,
                  size_t vlen, int64_t expire_ms);
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

/* A7 — typed pressure signals (INFO; policy may react; kernels stay C) */
uint64_t ar_core_type_keys(ArCore* core, int type);   /* 0=string..3=zset */
uint64_t ar_core_type_bytes(ArCore* core, int type);
uint64_t ar_core_bigkey_bytes(ArCore* core);
const char* ar_core_bigkey_type_name(ArCore* core);

/* A9 — hot_cold promote/demote / soft-cap knobs (CONFIG/RESP) */
int ar_core_set_hot_soft_cap_pct(ArCore* core, int pct); /* 1..100; default 25 */
int ar_core_hot_soft_cap_pct(ArCore* core);
int ar_core_set_hot_soft_cap_min(ArCore* core, int n); /* 0..1M; default 256 */
int ar_core_hot_soft_cap_min(ArCore* core);
int ar_core_set_hot_promote_on_get(ArCore* core, int on); /* 0/1; default 1 */
int ar_core_hot_promote_on_get(ArCore* core);
uint64_t ar_core_hot_soft_cap(ArCore* core); /* effective current soft-cap */

/* A10 — shadow / A/B sample (CONFIG/INFO; agent dry-run never applies loser) */
int ar_core_set_shadow_policy(ArCore* core, const char* name); /* "" clears */
const char* ar_core_shadow_policy(ArCore* core);
int ar_core_set_shadow_sample_pct(ArCore* core, int pct); /* clamp 0..100 */
int ar_core_shadow_sample_pct(ArCore* core);
void ar_core_shadow_reset(ArCore* core);
uint64_t ar_core_shadow_samples(ArCore* core);
uint64_t ar_core_shadow_hits(ArCore* core);
uint64_t ar_core_shadow_misses(ArCore* core);
uint64_t ar_core_shadow_diverges(ArCore* core);
void ar_core_shadow_note_diverge(ArCore* core); /* agent dual-choice diverge */

/* P0.3 — active expire sampling (Redis-ish). Walks random buckets and frees
 * keys past expire_at. Returns number expired this call. effort ≈ samples. */
int ar_core_active_expire(ArCore* core, int effort);

/* M12 — per-prefix policy namespace hints */
int ar_core_policy_set(ArCore* core, const char* prefix, const char* profile);
/* Write "pfx:=prof;..." into buf; returns length (excl NUL). */
int ar_core_policy_hints(ArCore* core, char* buf, size_t buflen);

/* P2.13 — optional aura-rdb snapshot (string keys + TTL) */
int ar_core_set_rdb_dir(ArCore* core, const char* dir);
const char* ar_core_rdb_dir(ArCore* core);
int ar_core_set_rdb_filename(ArCore* core, const char* name);
const char* ar_core_rdb_filename(ArCore* core);
int ar_rdb_save(ArCore* core);   /* SAVE — sync write; 1 ok */
int ar_rdb_bgsave(ArCore* core); /* BGSAVE — fork child or sync fallback */
int ar_rdb_load(ArCore* core);   /* load on startup; missing file = ok */

/* P2.14 — REPLICAOF (best-effort async string KV) */
int ar_core_repl_readonly(ArCore* core);
const char* ar_core_master_host(ArCore* core);
int ar_core_master_port(ArCore* core);

/* P2.15 — optional native TLS (OpenSSL when AURA_REDIS_HAS_TLS) */
int ar_tls_available(void); /* 1 if built with OpenSSL */
int ar_core_set_tls_cert_file(ArCore* core, const char* path);
int ar_core_set_tls_key_file(ArCore* core, const char* path);
int ar_core_set_tls_ca_file(ArCore* core, const char* path); /* optional */
const char* ar_core_tls_cert_file(ArCore* core);
const char* ar_core_tls_key_file(ArCore* core);
const char* ar_core_tls_ca_file(ArCore* core);
int ar_core_listen_tls(ArCore* core, int port); /* after ar_core_listen; needs cert/key */
int ar_core_tls_port(ArCore* core);
int ar_core_tls_enabled(ArCore* core);


/* P3.16 thin public FFI — C-string keys for Aura std/ffi (≤6 args).
 * wrongtype: optional int* out; set to 1 on WRONGTYPE (return -1 / NULL).
 * malloc'd returns: caller frees via ar_free. */
const char* ar_type(ArCore* core, const char* key); /* none|string|hash|list|zset */

int64_t ar_hset(ArCore* core, const char* key, const char* field, const char* val,
               int* wrongtype);
char* ar_hget(ArCore* core, const char* key, const char* field, int* wrongtype);
int64_t ar_hdel(ArCore* core, const char* key, const char* field, int* wrongtype);
int64_t ar_hexists(ArCore* core, const char* key, const char* field, int* wrongtype);
int64_t ar_hlen(ArCore* core, const char* key, int* wrongtype);
int64_t ar_hincrby(ArCore* core, const char* key, const char* field, int64_t incr,
                   int* wrongtype, int* ok);

int64_t ar_lpush(ArCore* core, const char* key, const char* val, int* wrongtype);
int64_t ar_rpush(ArCore* core, const char* key, const char* val, int* wrongtype);
char* ar_lpop(ArCore* core, const char* key, int* wrongtype);
char* ar_rpop(ArCore* core, const char* key, int* wrongtype);
int64_t ar_llen(ArCore* core, const char* key, int* wrongtype);
char* ar_lindex(ArCore* core, const char* key, int64_t index, int* wrongtype);

int64_t ar_zadd(ArCore* core, const char* key, int64_t score, const char* member,
               int* wrongtype);
char* ar_zscore(ArCore* core, const char* key, const char* member, int* ok,
                int* wrongtype);
int64_t ar_zrem(ArCore* core, const char* key, const char* member, int* wrongtype);
int64_t ar_zcard(ArCore* core, const char* key, int* wrongtype);

/* Tier-2 keyspace iteration (string + HASH/LIST/ZSET keys; not fields).
 * Glob MATCH: '*' and '?' only (no [class]). Cursor is opaque bucket index;
 * next_cursor 0 means iteration complete. COUNT is a buckets-examined hint
 * (default 10). Expired keys are purged as encountered.
 * out_keys/out_klens: malloc'd arrays of n entries (or NULL if n==0);
 * free via ar_scan_free. */
int ar_glob_match(const char* pat, size_t plen, const char* str, size_t slen);
void ar_scan_free(char** keys, size_t* klens, size_t n);
size_t ar_scan(ArCore* core, uint64_t cursor, const char* pattern, size_t plen,
               int count, char*** out_keys, size_t** out_klens,
               uint64_t* next_cursor);
/* KEYS — full keyspace scan (O(N)); same match/free contract as ar_scan. */
size_t ar_keys(ArCore* core, const char* pattern, size_t plen, char*** out_keys,
               size_t** out_klens);

/* Tier-2 string/key ops */
/* APPEND: new length; *wrongtype=1 on non-string; -1 on OOM/wrongtype. */
int64_t ar_append(ArCore* core, const char* key, size_t klen, const char* val,
                  size_t vlen, int* wrongtype);
/* RENAME: 1=ok, 0=no such key, 2=RENAMENX dest exists, -1=OOM.
 * nx!=0 → do not overwrite destination. Moves any type; preserves TTL. */
int ar_rename(ArCore* core, const char* key, size_t klen, const char* newkey,
              size_t nklen, int nx);

#ifdef __cplusplus
}
#endif
#endif
