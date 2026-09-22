#ifndef AURA_REDIS_AR_INTERNAL_H
#define AURA_REDIS_AR_INTERNAL_H

#include "ar_core.h"

#include <stddef.h>
#include <stdint.h>

typedef struct ArEntry {
  char* key;
  size_t klen;
  char* val;
  size_t vlen;
  uint64_t last_access;
  uint8_t lfu_freq; /* approximate LFU counter (Iteration 5) */
  uint8_t pinned;   /* MVP M4: skip in eviction when set */
  uint64_t expire_at; /* 0 = none; else absolute deadline ms (M9) */
  struct ArEntry* next;
} ArEntry;

typedef enum {
  AR_LAYOUT_FLAT = 0,
  AR_LAYOUT_HOT_COLD = 1,
} ArLayoutKind;

#define AR_MAX_CONN 1024
#define AR_RBUF_INIT (64 * 1024)
#define AR_WBUF_INIT (64 * 1024)

typedef struct ArConn {
  int fd;
  int in_use;
  char* rbuf;
  size_t rlen;
  size_t rcap;
  char* wbuf;
  size_t wlen;
  size_t wcap;
  size_t woff; /* bytes already written from wbuf */
  int should_close;
  int want_write;
} ArConn;

struct ArCore {
  /* Primary / hot hash (flat = only table; hot_cold = hot tier) */
  ArEntry** buckets;
  size_t nbuckets;
  size_t nkeys; /* total keys across all tiers */

  /* Cold tier (hot_cold only; NULL when flat) */
  ArEntry** cold_buckets;
  size_t cold_nbuckets;
  size_t hot_nkeys;
  size_t cold_nkeys;

  ArLayoutKind layout;
  uint64_t layout_gen; /* bumped on each successful migrate */
  int layout_busy;     /* 1 while migrate runs (quiescent guard) */
  uint64_t promotions; /* cold→hot on GET */
  uint64_t demotions;  /* hot→cold under pressure */
  uint64_t migrates;

  const ArEvictOps* evict;
  int evict_samples; /* MVP M4: approx sample size (default 16) */
  uint64_t ops, gets, sets, hits, misses;
  uint64_t evicted, expired;
  uint64_t pinned_keys; /* count of entries with pinned=1 */
  uint64_t keys_with_ttl; /* keys with expire_at != 0 (M9) */
  uint64_t expire_at_sum; /* sum of expire_at for avg_ttl proxy (M9) */
  uint64_t clock;
  uint64_t maxmemory; /* 0 = unlimited */
  uint64_t used_memory;

  /* network */
  int listen_fd;
  int epfd;
  ArConn conns[AR_MAX_CONN];
  int nconns;
  int quit;
  void* evict_plugin; /* dlopen handle; NULL if built-in */
  uint64_t plugin_reloads; /* successful ar_core_load_evict_plugin */

  /* M12 — per-prefix policy hints (RESP POLICY → INFO policy_hints) */
  char policy_pfx[4][16];
  char policy_prof[4][32];
  int policy_n;
};

/* dict helpers used by server */
/* tier_out: 0=hot/flat, 1=cold (only meaningful for hot_cold) */
ArEntry* ar_find_entry(ArCore* core, const char* key, size_t klen,
                       size_t* bucket_out);
ArEntry* ar_find_entry_ex(ArCore* core, const char* key, size_t klen,
                          size_t* bucket_out, int* tier_out);
int ar_entry_set(ArCore* core, const char* key, size_t klen, const char* val,
                 size_t vlen);
/* M9: SET with absolute expire_at ms (0 = no TTL). */
int ar_entry_set_ex(ArCore* core, const char* key, size_t klen, const char* val,
                    size_t vlen, uint64_t expire_at);
uint64_t ar_now_ms(void);
void ar_entry_free(ArCore* core, size_t bucket, ArEntry* e);
void ar_entry_free_ex(ArCore* core, size_t bucket, int tier, ArEntry* e);
void ar_rehash_if_needed(ArCore* core);
/* Promote cold→hot on GET hit; may demote if hot soft-full. */
void ar_touch_get(ArCore* core, ArEntry* e, size_t bucket, int tier);

#endif
