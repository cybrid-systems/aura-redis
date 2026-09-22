#include "ar_internal.h"
#include <stdint.h>
#include <limits.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <time.h>


uint64_t ar_now_ms(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_REALTIME, &ts) != 0)
    return 0;
  return (uint64_t)ts.tv_sec * 1000ull + (uint64_t)(ts.tv_nsec / 1000000ull);
}

static void entry_clear_expire(ArCore* core, ArEntry* e) {
  if (!e || !e->expire_at)
    return;
  if (core->keys_with_ttl)
    core->keys_with_ttl--;
  if (core->expire_at_sum >= e->expire_at)
    core->expire_at_sum -= e->expire_at;
  else
    core->expire_at_sum = 0;
  e->expire_at = 0;
}

static void entry_set_expire_at(ArCore* core, ArEntry* e, uint64_t expire_at) {
  if (!e)
    return;
  entry_clear_expire(core, e);
  if (expire_at) {
    e->expire_at = expire_at;
    core->keys_with_ttl++;
    core->expire_at_sum += expire_at;
  }
}

static void evict_noop_on_get(ArCore* db, void* entry) {
  (void)db;
  (void)entry;
}
static void evict_noop_on_set(ArCore* db, void* entry) {
  (void)db;
  (void)entry;
}
static int evict_noop_should(ArCore* db) {
  (void)db;
  return 0;
}
static int evict_noop_one(ArCore* db) {
  (void)db;
  return 0;
}

static const ArEvictOps kEvictNoop = {
    .on_get = evict_noop_on_get,
    .on_set = evict_noop_on_set,
    .should_evict = evict_noop_should,
    .evict_one = evict_noop_one,
    .name = "noop",
};

/* Sample entries from hot and (if present) cold; used by LRU/LFU.
 * Skips pinned keys (MVP M4). Sample count from db->evict_samples. */
static void sample_pick_lru(ArCore* db, ArEntry** best, size_t* best_b,
                            int* best_tier, uint64_t* best_t, int* samples) {
  int max_samples = db->evict_samples > 0 ? db->evict_samples : 16;
  for (int attempt = 0; attempt < 128 && *samples < max_samples; ++attempt) {
    int use_cold = (db->layout == AR_LAYOUT_HOT_COLD && db->cold_buckets &&
                    db->cold_nkeys > 0 && (attempt & 1));
    ArEntry** table = use_cold ? db->cold_buckets : db->buckets;
    size_t nb = use_cold ? db->cold_nbuckets : db->nbuckets;
    if (!table || nb == 0)
      continue;
    size_t i = (size_t)(rand() % (int)nb);
    for (ArEntry* e = table[i]; e && *samples < max_samples; e = e->next) {
      if (e->pinned)
        continue;
      (*samples)++;
      if (e->last_access < *best_t) {
        *best_t = e->last_access;
        *best = e;
        *best_b = i;
        *best_tier = use_cold ? 1 : 0;
      }
    }
  }
}

static void sample_pick_lfu(ArCore* db, ArEntry** best, size_t* best_b,
                            int* best_tier, uint8_t* best_f, uint64_t* best_t,
                            int* samples) {
  int max_samples = db->evict_samples > 0 ? db->evict_samples : 16;
  for (int attempt = 0; attempt < 128 && *samples < max_samples; ++attempt) {
    int use_cold = (db->layout == AR_LAYOUT_HOT_COLD && db->cold_buckets &&
                    db->cold_nkeys > 0 && (attempt & 1));
    ArEntry** table = use_cold ? db->cold_buckets : db->buckets;
    size_t nb = use_cold ? db->cold_nbuckets : db->nbuckets;
    if (!table || nb == 0)
      continue;
    size_t i = (size_t)(rand() % (int)nb);
    for (ArEntry* e = table[i]; e && *samples < max_samples; e = e->next) {
      if (e->pinned)
        continue;
      (*samples)++;
      if (e->lfu_freq < *best_f ||
          (e->lfu_freq == *best_f && e->last_access < *best_t)) {
        *best_f = e->lfu_freq;
        *best_t = e->last_access;
        *best = e;
        *best_b = i;
        *best_tier = use_cold ? 1 : 0;
      }
    }
  }
}

/* --- LRU (Iteration 5) --- */
static void evict_lru_on_get(ArCore* db, void* entry) {
  ArEntry* e = (ArEntry*)entry;
  e->last_access = ++db->clock;
}
static void evict_lru_on_set(ArCore* db, void* entry) {
  ArEntry* e = (ArEntry*)entry;
  e->last_access = ++db->clock;
}
static int evict_lru_should(ArCore* db) {
  return db->maxmemory > 0 && db->used_memory > db->maxmemory;
}
static int evict_lru_one(ArCore* db) {
  ArEntry* best = NULL;
  size_t best_b = 0;
  int best_tier = 0;
  uint64_t best_t = UINT64_MAX;
  int samples = 0;
  if (db->nkeys == 0)
    return 0;
  sample_pick_lru(db, &best, &best_b, &best_tier, &best_t, &samples);
  if (!best)
    return 0;
  ar_entry_free_ex(db, best_b, best_tier, best);
  db->evicted++;
  return 1;
}

static const ArEvictOps kEvictLru = {
    .on_get = evict_lru_on_get,
    .on_set = evict_lru_on_set,
    .should_evict = evict_lru_should,
    .evict_one = evict_lru_one,
    .name = "lru",
};

/* --- LFU approximate --- */
static void lfu_incr(ArEntry* e) {
  if (e->lfu_freq < 255)
    e->lfu_freq++;
}
static void evict_lfu_on_get(ArCore* db, void* entry) {
  (void)db;
  lfu_incr((ArEntry*)entry);
}
static void evict_lfu_on_set(ArCore* db, void* entry) {
  ArEntry* e = (ArEntry*)entry;
  e->last_access = ++db->clock;
  if (e->lfu_freq == 0)
    e->lfu_freq = 5;
}
static int evict_lfu_should(ArCore* db) {
  return db->maxmemory > 0 && db->used_memory > db->maxmemory;
}
static int evict_lfu_one(ArCore* db) {
  ArEntry* best = NULL;
  size_t best_b = 0;
  int best_tier = 0;
  uint8_t best_f = 255;
  uint64_t best_t = UINT64_MAX;
  int samples = 0;
  if (db->nkeys == 0)
    return 0;
  sample_pick_lfu(db, &best, &best_b, &best_tier, &best_f, &best_t, &samples);
  if (!best)
    return 0;
  ar_entry_free_ex(db, best_b, best_tier, best);
  db->evicted++;
  return 1;
}

static const ArEvictOps kEvictLfu = {
    .on_get = evict_lfu_on_get,
    .on_set = evict_lfu_on_set,
    .should_evict = evict_lfu_should,
    .evict_one = evict_lfu_one,
    .name = "lfu",
};


/* --- TTL-aware (M9): prefer soonest expire_at under maxmemory --- */
static void sample_pick_ttl_aware(ArCore* db, ArEntry** best, size_t* best_b,
                                  int* best_tier, uint64_t* best_exp,
                                  uint64_t* best_t, int* samples,
                                  int* found_ttl) {
  int max_samples = db->evict_samples > 0 ? db->evict_samples : 16;
  for (int attempt = 0; attempt < 128 && *samples < max_samples; ++attempt) {
    int use_cold = (db->layout == AR_LAYOUT_HOT_COLD && db->cold_buckets &&
                    db->cold_nkeys > 0 && (attempt & 1));
    ArEntry** table = use_cold ? db->cold_buckets : db->buckets;
    size_t nb = use_cold ? db->cold_nbuckets : db->nbuckets;
    if (!table || nb == 0)
      continue;
    size_t i = (size_t)(rand() % (int)nb);
    for (ArEntry* e = table[i]; e && *samples < max_samples; e = e->next) {
      if (e->pinned)
        continue;
      (*samples)++;
      if (e->expire_at) {
        if (!*found_ttl || e->expire_at < *best_exp ||
            (e->expire_at == *best_exp && e->last_access < *best_t)) {
          *found_ttl = 1;
          *best_exp = e->expire_at;
          *best_t = e->last_access;
          *best = e;
          *best_b = i;
          *best_tier = use_cold ? 1 : 0;
        }
      } else if (!*found_ttl) {
        /* Fallback: LRU among no-TTL only until a TTL key is sampled */
        if (e->last_access < *best_t) {
          *best_t = e->last_access;
          *best_exp = UINT64_MAX;
          *best = e;
          *best_b = i;
          *best_tier = use_cold ? 1 : 0;
        }
      }
    }
  }
}

static void evict_ttl_on_get(ArCore* db, void* entry) {
  ArEntry* e = (ArEntry*)entry;
  e->last_access = ++db->clock;
}

static void evict_ttl_on_set(ArCore* db, void* entry) {
  ArEntry* e = (ArEntry*)entry;
  e->last_access = ++db->clock;
}

static int evict_ttl_should(ArCore* db) {
  return db->maxmemory > 0 && db->used_memory > db->maxmemory;
}

static int evict_ttl_one(ArCore* db) {
  ArEntry* best = NULL;
  size_t best_b = 0;
  int best_tier = 0;
  uint64_t best_exp = UINT64_MAX;
  uint64_t best_t = UINT64_MAX;
  int samples = 0;
  int found_ttl = 0;
  if (db->nkeys == 0)
    return 0;
  sample_pick_ttl_aware(db, &best, &best_b, &best_tier, &best_exp, &best_t,
                        &samples, &found_ttl);
  if (!best)
    return 0;
  ar_entry_free_ex(db, best_b, best_tier, best);
  db->evicted++;
  return 1;
}

static const ArEvictOps kEvictTtlAware = {
    .on_get = evict_ttl_on_get,
    .on_set = evict_ttl_on_set,
    .should_evict = evict_ttl_should,
    .evict_one = evict_ttl_one,
    .name = "ttl_aware",
};


static size_t hash_bin(const char* s, size_t n) {
  size_t h = 1469598103934665603ull;
  for (size_t i = 0; i < n; ++i) {
    h ^= (unsigned char)s[i];
    h *= 1099511628211ull;
  }
  return h;
}

static int key_eq(const ArEntry* e, const char* key, size_t klen) {
  return e->klen == klen && memcmp(e->key, key, klen) == 0;
}

static char* xmemdup(const char* s, size_t n) {
  char* p = (char*)malloc(n + 1);
  if (!p)
    return NULL;
  if (n)
    memcpy(p, s, n);
  p[n] = '\0';
  return p;
}

static void maybe_evict(ArCore* core) {
  if (!core->evict || !core->evict->should_evict)
    return;
  /* P0.2: must reclaim enough for large SETs / floods — fixed 64 under-shot
   * maxmemory when many small keys needed eviction. Bound by nkeys. */
  int guard = 0;
  int limit = (int)core->nkeys + 8;
  if (limit < 64)
    limit = 64;
  if (limit > 1000000)
    limit = 1000000;
  while (core->evict->should_evict(core) && guard++ < limit) {
    if (!core->evict->evict_one || !core->evict->evict_one(core))
      break;
  }
}

static ArEntry* find_in_table(ArEntry** table, size_t nbuckets, const char* key,
                              size_t klen, size_t* bucket_out) {
  if (!table || nbuckets == 0)
    return NULL;
  size_t b = hash_bin(key, klen) & (nbuckets - 1);
  if (bucket_out)
    *bucket_out = b;
  for (ArEntry* e = table[b]; e; e = e->next) {
    if (key_eq(e, key, klen))
      return e;
  }
  return NULL;
}

static int entry_expired_now(ArCore* core, ArEntry* e, size_t b, int tier) {
  if (!e || !e->expire_at)
    return 0;
  if (ar_now_ms() < e->expire_at)
    return 0;
  ar_entry_free_ex(core, b, tier, e);
  core->expired++;
  return 1;
}

ArEntry* ar_find_entry_ex(ArCore* core, const char* key, size_t klen,
                          size_t* bucket_out, int* tier_out) {
  size_t b = 0;
  ArEntry* e = find_in_table(core->buckets, core->nbuckets, key, klen, &b);
  if (e) {
    if (entry_expired_now(core, e, b, 0)) {
      e = NULL;
    } else {
      if (bucket_out)
        *bucket_out = b;
      if (tier_out)
        *tier_out = 0;
      return e;
    }
  }
  if (core->layout == AR_LAYOUT_HOT_COLD && core->cold_buckets) {
    e = find_in_table(core->cold_buckets, core->cold_nbuckets, key, klen, &b);
    if (e) {
      if (entry_expired_now(core, e, b, 1)) {
        e = NULL;
      } else {
        if (bucket_out)
          *bucket_out = b;
        if (tier_out)
          *tier_out = 1;
        return e;
      }
    }
  }
  if (bucket_out)
    *bucket_out = hash_bin(key, klen) & (core->nbuckets - 1);
  if (tier_out)
    *tier_out = 0;
  return NULL;
}

ArEntry* ar_find_entry(ArCore* core, const char* key, size_t klen,
                       size_t* bucket_out) {
  return ar_find_entry_ex(core, key, klen, bucket_out, NULL);
}

static void unlink_entry(ArEntry** table, size_t bucket, ArEntry* e) {
  ArEntry** pp = &table[bucket];
  while (*pp) {
    if (*pp == e) {
      *pp = e->next;
      e->next = NULL;
      return;
    }
    pp = &(*pp)->next;
  }
}

void ar_entry_free_ex(ArCore* core, size_t bucket, int tier, ArEntry* e) {
  entry_clear_expire(core, e);
  ArEntry** table =
      (tier == 1 && core->cold_buckets) ? core->cold_buckets : core->buckets;
  unlink_entry(table, bucket, e);
  core->used_memory -= e->klen + e->vlen + sizeof(ArEntry);
  if (e->pinned && core->pinned_keys)
    core->pinned_keys--;
  if (core->layout == AR_LAYOUT_HOT_COLD) {
    if (tier == 1) {
      if (core->cold_nkeys)
        core->cold_nkeys--;
    } else {
      if (core->hot_nkeys)
        core->hot_nkeys--;
    }
  }
  free(e->key);
  free(e->val);
  free(e);
  if (core->nkeys)
    core->nkeys--;
}

void ar_entry_free(ArCore* core, size_t bucket, ArEntry* e) {
  /* Legacy: assume hot/flat table. Prefer ar_entry_free_ex. */
  ar_entry_free_ex(core, bucket, 0, e);
}

/* Soft cap for hot tier: keep ~1/4 of keys hot (floor 256).
 * Independent of nbuckets so rehash cannot disable demotion. */
static size_t hot_soft_cap(ArCore* core) {
  size_t cap = core->nkeys / 4;
  if (cap < 256)
    cap = 256;
  return cap;
}

static int demote_one_hot_to_cold(ArCore* core) {
  if (core->layout != AR_LAYOUT_HOT_COLD || !core->cold_buckets ||
      core->hot_nkeys == 0)
    return 0;
  ArEntry* best = NULL;
  size_t best_b = 0;
  uint64_t best_t = UINT64_MAX;
  int samples = 0;
  for (int attempt = 0; attempt < 64 && samples < 16; ++attempt) {
    size_t i = (size_t)(rand() % (int)core->nbuckets);
    for (ArEntry* e = core->buckets[i]; e && samples < 16; e = e->next) {
      samples++;
      if (e->last_access < best_t) {
        best_t = e->last_access;
        best = e;
        best_b = i;
      }
    }
  }
  if (!best)
    return 0;
  unlink_entry(core->buckets, best_b, best);
  core->hot_nkeys--;
  size_t cb = hash_bin(best->key, best->klen) & (core->cold_nbuckets - 1);
  best->next = core->cold_buckets[cb];
  core->cold_buckets[cb] = best;
  core->cold_nkeys++;
  core->demotions++;
  return 1;
}

static void maybe_demote_hot(ArCore* core) {
  if (core->layout != AR_LAYOUT_HOT_COLD)
    return;
  int guard = 0;
  while (core->hot_nkeys > hot_soft_cap(core) && guard++ < 64) {
    if (!demote_one_hot_to_cold(core))
      break;
  }
}

void ar_touch_get(ArCore* core, ArEntry* e, size_t bucket, int tier) {
  e->last_access = ++core->clock;
  if (core->layout == AR_LAYOUT_HOT_COLD && tier == 1 && core->cold_buckets) {
    /* Promote cold → hot */
    unlink_entry(core->cold_buckets, bucket, e);
    if (core->cold_nkeys)
      core->cold_nkeys--;
    size_t hb = hash_bin(e->key, e->klen) & (core->nbuckets - 1);
    e->next = core->buckets[hb];
    core->buckets[hb] = e;
    core->hot_nkeys++;
    core->promotions++;
    maybe_demote_hot(core);
  }
}

static void rehash_table(ArEntry*** table_io, size_t* nb_io) {
  size_t n2 = (*nb_io) * 2;
  ArEntry** nb = (ArEntry**)calloc(n2, sizeof(ArEntry*));
  if (!nb)
    return;
  for (size_t i = 0; i < *nb_io; ++i) {
    ArEntry* e = (*table_io)[i];
    while (e) {
      ArEntry* next = e->next;
      size_t b = hash_bin(e->key, e->klen) & (n2 - 1);
      e->next = nb[b];
      nb[b] = e;
      e = next;
    }
  }
  free(*table_io);
  *table_io = nb;
  *nb_io = n2;
}

void ar_rehash_if_needed(ArCore* core) {
  if (core->layout == AR_LAYOUT_FLAT) {
    if (core->nkeys <= core->nbuckets)
      return;
    rehash_table(&core->buckets, &core->nbuckets);
    return;
  }
  /* hot_cold: rehash each tier by its own count */
  if (core->hot_nkeys > core->nbuckets)
    rehash_table(&core->buckets, &core->nbuckets);
  if (core->cold_buckets && core->cold_nkeys > core->cold_nbuckets)
    rehash_table(&core->cold_buckets, &core->cold_nbuckets);
}

int ar_entry_set_ex(ArCore* core, const char* key, size_t klen, const char* val,
                    size_t vlen, uint64_t expire_at) {
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (e) {
    char* nv = xmemdup(val, vlen);
    if (!nv)
      return 0;
    core->used_memory -= e->vlen;
    free(e->val);
    e->val = nv;
    e->vlen = vlen;
    core->used_memory += vlen;
    e->last_access = ++core->clock;
    entry_set_expire_at(core, e, expire_at);
    /* Updates land in hot: promote if currently cold */
    if (core->layout == AR_LAYOUT_HOT_COLD && tier == 1) {
      ar_touch_get(core, e, b, tier);
    }
    if (core->evict && core->evict->on_set)
      core->evict->on_set(core, e);
    maybe_evict(core);
    return 1;
  }
  ArEntry* ne = (ArEntry*)calloc(1, sizeof(ArEntry));
  if (!ne)
    return 0;
  ne->key = xmemdup(key, klen);
  ne->val = xmemdup(val, vlen);
  if (!ne->key || !ne->val) {
    free(ne->key);
    free(ne->val);
    free(ne);
    return 0;
  }
  ne->klen = klen;
  ne->vlen = vlen;
  ne->last_access = ++core->clock;
  ne->lfu_freq = 5;
  ne->expire_at = 0;
  /* New keys always enter hot/flat */
  b = hash_bin(key, klen) & (core->nbuckets - 1);
  ne->next = core->buckets[b];
  core->buckets[b] = ne;
  core->nkeys++;
  if (core->layout == AR_LAYOUT_HOT_COLD)
    core->hot_nkeys++;
  core->used_memory += klen + vlen + sizeof(ArEntry);
  entry_set_expire_at(core, ne, expire_at);
  if (core->evict && core->evict->on_set)
    core->evict->on_set(core, ne);
  maybe_demote_hot(core);
  ar_rehash_if_needed(core);
  maybe_evict(core);
  return 1;
}

int ar_entry_set(ArCore* core, const char* key, size_t klen, const char* val,
                 size_t vlen) {
  /* Plain SET clears TTL (Redis default without KEEPTTL). */
  return ar_entry_set_ex(core, key, klen, val, vlen, 0);
}

static int migrate_to_hot_cold(ArCore* core) {
  if (core->layout == AR_LAYOUT_HOT_COLD)
    return 1;
  size_t cn = core->nbuckets;
  if (cn < 256)
    cn = 256;
  ArEntry** cold = (ArEntry**)calloc(cn, sizeof(ArEntry*));
  if (!cold)
    return 0;
  core->cold_buckets = cold;
  core->cold_nbuckets = cn;
  core->cold_nkeys = 0;
  core->hot_nkeys = core->nkeys;
  core->layout = AR_LAYOUT_HOT_COLD;
  core->layout_gen++;
  core->migrates++;
  fprintf(stderr, "ar_core: layout migrate → hot_cold gen=%llu keys=%zu\n",
          (unsigned long long)core->layout_gen, core->nkeys);
  return 1;
}

static int migrate_to_flat(ArCore* core) {
  if (core->layout == AR_LAYOUT_FLAT)
    return 1;
  /* Fold cold into hot */
  if (core->cold_buckets) {
    for (size_t i = 0; i < core->cold_nbuckets; ++i) {
      ArEntry* e = core->cold_buckets[i];
      while (e) {
        ArEntry* next = e->next;
        size_t b = hash_bin(e->key, e->klen) & (core->nbuckets - 1);
        e->next = core->buckets[b];
        core->buckets[b] = e;
        e = next;
      }
      core->cold_buckets[i] = NULL;
    }
    free(core->cold_buckets);
    core->cold_buckets = NULL;
    core->cold_nbuckets = 0;
    core->cold_nkeys = 0;
  }
  core->hot_nkeys = 0;
  core->layout = AR_LAYOUT_FLAT;
  core->layout_gen++;
  core->migrates++;
  ar_rehash_if_needed(core);
  fprintf(stderr, "ar_core: layout migrate → flat gen=%llu keys=%zu\n",
          (unsigned long long)core->layout_gen, core->nkeys);
  return 1;
}

static ArLayoutKind parse_layout_name(const char* name) {
  if (!name)
    return (ArLayoutKind)-1;
  if (strcmp(name, "flat") == 0 || strcmp(name, "flat_hash") == 0)
    return AR_LAYOUT_FLAT;
  if (strcmp(name, "hot_cold") == 0 || strcmp(name, "hot-cold") == 0)
    return AR_LAYOUT_HOT_COLD;
  return (ArLayoutKind)-1;
}

int ar_core_set_layout(ArCore* core, const char* name) {
  if (!core || !name)
    return 0;
  ArLayoutKind want = parse_layout_name(name);
  if ((int)want < 0)
    return 0;
  if (core->layout_busy)
    return 0; /* nested / non-quiescent */
  if (core->layout == want)
    return 1;
  core->layout_busy = 1;
  int ok = 0;
  if (want == AR_LAYOUT_HOT_COLD)
    ok = migrate_to_hot_cold(core);
  else
    ok = migrate_to_flat(core);
  core->layout_busy = 0;
  return ok;
}

const char* ar_core_layout_name(ArCore* core) {
  if (!core)
    return "none";
  return core->layout == AR_LAYOUT_HOT_COLD ? "hot_cold" : "flat";
}

uint64_t ar_core_layout_gen(ArCore* core) {
  return core ? core->layout_gen : 0;
}

uint64_t ar_core_hot_keys(ArCore* core) {
  if (!core)
    return 0;
  if (core->layout == AR_LAYOUT_HOT_COLD)
    return core->hot_nkeys;
  return core->nkeys;
}

uint64_t ar_core_cold_keys(ArCore* core) {
  if (!core)
    return 0;
  if (core->layout == AR_LAYOUT_HOT_COLD)
    return core->cold_nkeys;
  return 0;
}

uint64_t ar_metric_promotions(ArCore* core) {
  return core ? core->promotions : 0;
}

uint64_t ar_metric_demotions(ArCore* core) {
  return core ? core->demotions : 0;
}

uint64_t ar_metric_migrates(ArCore* core) {
  return core ? core->migrates : 0;
}

/* Simple adaptive rule: large working set + GET-heavy → hot_cold; tiny → flat. */
int ar_core_adapt_layout(ArCore* core) {
  if (!core || core->layout_busy)
    return 0;
  uint64_t gets = core->gets;
  uint64_t sets = core->sets;
  if (core->nkeys >= 500 && gets > sets * 3) {
    if (core->layout != AR_LAYOUT_HOT_COLD)
      return ar_core_set_layout(core, "hot_cold");
    return 0;
  }
  if (core->nkeys < 64 && core->layout == AR_LAYOUT_HOT_COLD) {
    return ar_core_set_layout(core, "flat");
  }
  return 0;
}

ArCore* ar_core_create(void) {
  ArCore* c = (ArCore*)calloc(1, sizeof(ArCore));
  if (!c)
    return NULL;
  c->nbuckets = 1024;
  c->buckets = (ArEntry**)calloc(c->nbuckets, sizeof(ArEntry*));
  if (!c->buckets) {
    free(c);
    return NULL;
  }
  c->layout = AR_LAYOUT_FLAT;
  c->evict = &kEvictNoop;
  c->evict_samples = 16;
  c->evict_plugin = NULL;
  c->plugin_reloads = 0;
  c->listen_fd = -1;
  c->epfd = -1;
  return c;
}

void ar_core_destroy(ArCore* core) {
  if (!core)
    return;
  if (core->listen_fd >= 0) {
    extern void ar_net_shutdown(ArCore* core);
    ar_net_shutdown(core);
  }
  if (core->evict_plugin) {
    dlclose(core->evict_plugin);
    core->evict_plugin = NULL;
    core->evict = &kEvictNoop;
  }
  for (size_t i = 0; i < core->nbuckets; ++i) {
    ArEntry* e = core->buckets[i];
    while (e) {
      ArEntry* n = e->next;
      free(e->key);
      free(e->val);
      free(e);
      e = n;
    }
  }
  free(core->buckets);
  if (core->cold_buckets) {
    for (size_t i = 0; i < core->cold_nbuckets; ++i) {
      ArEntry* e = core->cold_buckets[i];
      while (e) {
        ArEntry* n = e->next;
        free(e->key);
        free(e->val);
        free(e);
        e = n;
      }
    }
    free(core->cold_buckets);
  }
  free(core);
}

int ar_set_bin(ArCore* core, const char* key, size_t klen, const char* val,
               size_t vlen) {
  if (!core || !key || !val)
    return 0;
  core->ops++;
  core->sets++;
  return ar_entry_set(core, key, klen, val, vlen);
}

int ar_set_bin_ex(ArCore* core, const char* key, size_t klen, const char* val,
                  size_t vlen, int64_t expire_sec) {
  if (!core || !key || !val)
    return 0;
  core->ops++;
  core->sets++;
  uint64_t exp = 0;
  if (expire_sec > 0) {
    exp = ar_now_ms() + (uint64_t)expire_sec * 1000ull;
  }
  return ar_entry_set_ex(core, key, klen, val, vlen, exp);
}

int ar_set(ArCore* core, const char* key, const char* val) {
  if (!key || !val)
    return 0;
  return ar_set_bin(core, key, strlen(key), val, strlen(val));
}

int ar_expire(ArCore* core, const char* key, size_t klen, int64_t seconds) {
  if (!core || !key)
    return 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e)
    return 0;
  if (seconds <= 0) {
    entry_clear_expire(core, e);
    return 1;
  }
  entry_set_expire_at(core, e, ar_now_ms() + (uint64_t)seconds * 1000ull);
  return 1;
}

int64_t ar_ttl(ArCore* core, const char* key, size_t klen) {
  if (!core || !key)
    return -2;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e)
    return -2;
  if (!e->expire_at)
    return -1;
  uint64_t now = ar_now_ms();
  if (now >= e->expire_at)
    return -2; /* should have been lazy-deleted */
  return (int64_t)((e->expire_at - now + 999) / 1000);
}

char* ar_get_bin(ArCore* core, const char* key, size_t klen, size_t* out_len) {
  if (!core || !key)
    return NULL;
  core->ops++;
  core->gets++;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e) {
    core->misses++;
    if (out_len)
      *out_len = 0;
    return NULL;
  }
  core->hits++;
  ar_touch_get(core, e, b, tier);
  if (core->evict && core->evict->on_get)
    core->evict->on_get(core, e);
  if (out_len)
    *out_len = e->vlen;
  return xmemdup(e->val, e->vlen);
}

char* ar_get(ArCore* core, const char* key) {
  if (!key)
    return NULL;
  return ar_get_bin(core, key, strlen(key), NULL);
}

int ar_del_bin(ArCore* core, const char* key, size_t klen) {
  if (!core || !key)
    return 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e)
    return 0;
  ar_entry_free_ex(core, b, tier, e);
  core->ops++;
  return 1;
}

int ar_del(ArCore* core, const char* key) {
  if (!key)
    return 0;
  return ar_del_bin(core, key, strlen(key));
}

int ar_exists_bin(ArCore* core, const char* key, size_t klen) {
  if (!core || !key)
    return 0;
  return ar_find_entry(core, key, klen, NULL) ? 1 : 0;
}

int ar_exists(ArCore* core, const char* key) {
  if (!key)
    return 0;
  return ar_exists_bin(core, key, strlen(key));
}

void ar_free(char* p) { free(p); }

int ar_get_eq(ArCore* core, const char* key, const char* expect) {
  if (!core || !key || !expect)
    return 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, strlen(key), &b, &tier);
  if (!e)
    return 0;
  core->ops++;
  core->gets++;
  core->hits++;
  ar_touch_get(core, e, b, tier);
  if (core->evict && core->evict->on_get)
    core->evict->on_get(core, e);
  size_t elen = strlen(expect);
  return (e->vlen == elen && memcmp(e->val, expect, elen) == 0) ? 1 : 0;
}

int64_t ar_incr(ArCore* core, const char* key, size_t klen, int64_t delta,
                int* ok) {
  if (ok)
    *ok = 0;
  if (!core || !key)
    return 0;
  core->ops++;
  core->sets++;
  ArEntry* e = ar_find_entry(core, key, klen, NULL);
  int64_t v = 0;
  if (e) {
    char* end = NULL;
    char tmp[64];
    if (e->vlen >= sizeof(tmp)) {
      if (ok)
        *ok = 0;
      return 0;
    }
    memcpy(tmp, e->val, e->vlen);
    tmp[e->vlen] = '\0';
    v = strtoll(tmp, &end, 10);
    if (end == tmp || *end != '\0') {
      if (ok)
        *ok = 0;
      return 0;
    }
  }
  v += delta;
  char buf[32];
  int n = snprintf(buf, sizeof(buf), "%lld", (long long)v);
  if (n < 0 || !ar_entry_set(core, key, klen, buf, (size_t)n)) {
    if (ok)
      *ok = 0;
    return 0;
  }
  if (ok)
    *ok = 1;
  return v;
}

void ar_flushdb(ArCore* core) {
  if (!core)
    return;
  for (size_t i = 0; i < core->nbuckets; ++i) {
    ArEntry* e = core->buckets[i];
    while (e) {
      ArEntry* n = e->next;
      free(e->key);
      free(e->val);
      free(e);
      e = n;
    }
    core->buckets[i] = NULL;
  }
  if (core->cold_buckets) {
    for (size_t i = 0; i < core->cold_nbuckets; ++i) {
      ArEntry* e = core->cold_buckets[i];
      while (e) {
        ArEntry* n = e->next;
        free(e->key);
        free(e->val);
        free(e);
        e = n;
      }
      core->cold_buckets[i] = NULL;
    }
    core->cold_nkeys = 0;
  }
  core->nkeys = 0;
  core->hot_nkeys = 0;
  core->used_memory = 0;
  core->keys_with_ttl = 0;
  core->expire_at_sum = 0;
  core->ops++;
}

int ar_mset(ArCore* core, size_t n, const char** keys, const size_t* klens,
            const char** vals, const size_t* vlens) {
  if (!core || !keys || !vals)
    return 0;
  for (size_t i = 0; i < n; ++i) {
    core->ops++;
    core->sets++;
    if (!ar_entry_set(core, keys[i], klens[i], vals[i], vlens[i]))
      return 0;
  }
  return 1;
}

int ar_ping(ArCore* core) {
  (void)core;
  return 1;
}

int ar_core_set_evict_by_name(ArCore* core, const char* name) {
  if (!core || !name)
    return 0;
  const ArEvictOps* ops = NULL;
  if (strcmp(name, "noop") == 0)
    ops = &kEvictNoop;
  else if (strcmp(name, "lru") == 0)
    ops = &kEvictLru;
  else if (strcmp(name, "lfu") == 0)
    ops = &kEvictLfu;
  else if (strcmp(name, "ttl_aware") == 0 || strcmp(name, "ttl") == 0)
    ops = &kEvictTtlAware;
  else
    return 0;
  if (core->evict_plugin) {
    dlclose(core->evict_plugin);
    core->evict_plugin = NULL;
  }
  core->evict = ops;
  return 1;
}

const char* ar_core_evict_name(ArCore* core) {
  if (!core || !core->evict)
    return "none";
  return core->evict->name ? core->evict->name : "none";
}

int ar_core_set_maxmemory(ArCore* core, uint64_t bytes) {
  if (!core)
    return 0;
  core->maxmemory = bytes;
  maybe_evict(core);
  return 1;
}

uint64_t ar_core_maxmemory(ArCore* core) {
  return core ? core->maxmemory : 0;
}

uint64_t ar_core_used_memory(ArCore* core) {
  return core ? core->used_memory : 0;
}

uint64_t ar_metric_ops(ArCore* core) { return core ? core->ops : 0; }
uint64_t ar_metric_gets(ArCore* core) { return core ? core->gets : 0; }
uint64_t ar_metric_sets(ArCore* core) { return core ? core->sets : 0; }
uint64_t ar_metric_hits(ArCore* core) { return core ? core->hits : 0; }
uint64_t ar_metric_misses(ArCore* core) { return core ? core->misses : 0; }
uint64_t ar_metric_evicted(ArCore* core) { return core ? core->evicted : 0; }
uint64_t ar_metric_expired(ArCore* core) { return core ? core->expired : 0; }

uint64_t ar_core_keys_with_ttl(ArCore* core) {
  return core ? core->keys_with_ttl : 0;
}

uint64_t ar_core_avg_ttl_ms(ArCore* core) {
  if (!core || core->keys_with_ttl == 0)
    return 0;
  uint64_t avg_at = core->expire_at_sum / core->keys_with_ttl;
  uint64_t now = ar_now_ms();
  if (avg_at <= now)
    return 0;
  return avg_at - now;
}

int ar_core_over_maxmemory(ArCore* core) {
  return core && core->maxmemory > 0 && core->used_memory > core->maxmemory;
}

int ar_core_evict_random_one(ArCore* core) {
  if (!core || core->nkeys == 0)
    return 0;
  /* Prefer cold when present (less important), else hot */
  if (core->layout == AR_LAYOUT_HOT_COLD && core->cold_buckets &&
      core->cold_nkeys > 0) {
    size_t start = (size_t)(core->clock++ % core->cold_nbuckets);
    for (size_t n = 0; n < core->cold_nbuckets && n < 64; ++n) {
      size_t b = (start + n) % core->cold_nbuckets;
      ArEntry* e = core->cold_buckets[b];
      if (!e)
        continue;
      ar_entry_free_ex(core, b, 1, e);
      core->evicted++;
      return 1;
    }
  }
  size_t start = (size_t)(core->clock++ % core->nbuckets);
  for (size_t n = 0; n < core->nbuckets && n < 64; ++n) {
    size_t b = (start + n) % core->nbuckets;
    ArEntry* e = core->buckets[b];
    if (!e)
      continue;
    ar_entry_free_ex(core, b, 0, e);
    core->evicted++;
    return 1;
  }
  return 0;
}

int ar_core_load_evict_plugin(ArCore* core, const char* so_path) {
  if (!core || !so_path || !so_path[0])
    return 0;
  void* h = dlopen(so_path, RTLD_NOW | RTLD_LOCAL);
  if (!h) {
    fprintf(stderr, "ar_core_load_evict_plugin: dlopen %s: %s\n", so_path,
            dlerror());
    return 0;
  }
  dlerror();
  typedef const ArEvictOps* (*get_ops_fn)(void);
  get_ops_fn get = (get_ops_fn)dlsym(h, "ar_plugin_evict_ops");
  const char* err = dlerror();
  if (err || !get) {
    fprintf(stderr, "ar_core_load_evict_plugin: dlsym ar_plugin_evict_ops: %s\n",
            err ? err : "null");
    dlclose(h);
    return 0;
  }
  const ArEvictOps* ops = get();
  if (!ops || !ops->evict_one) {
    fprintf(stderr, "ar_core_load_evict_plugin: invalid ops\n");
    dlclose(h);
    return 0;
  }
  void* old = core->evict_plugin;
  core->evict = ops;
  core->evict_plugin = h;
  core->plugin_reloads++;
  if (old)
    dlclose(old);
  fprintf(stderr, "ar_core: loaded eviction plugin %s name=%s reloads=%llu\n", so_path,
          ops->name ? ops->name : "?",
          (unsigned long long)core->plugin_reloads);
  maybe_evict(core);
  return 1;
}

uint64_t ar_metric_plugin_reloads(ArCore* core) {
  return core ? core->plugin_reloads : 0;
}

int ar_core_has_evict_plugin(ArCore* core) {
  return (core && core->evict_plugin) ? 1 : 0;
}

/* --- MVP M4: samples + pin set --- */

int ar_core_set_evict_samples(ArCore* core, int n) {
  if (!core)
    return 0;
  if (n < 1)
    n = 1;
  if (n > 256)
    n = 256;
  core->evict_samples = n;
  return 1;
}

int ar_core_evict_samples(ArCore* core) {
  return core ? (core->evict_samples > 0 ? core->evict_samples : 16) : 16;
}

uint64_t ar_core_nkeys(ArCore* core) {
  return core ? (uint64_t)core->nkeys : 0;
}

uint64_t ar_core_pinned_keys(ArCore* core) {
  return core ? core->pinned_keys : 0;
}

int ar_core_pin(ArCore* core, const char* key, size_t klen) {
  if (!core || !key)
    return 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e)
    return 0;
  if (!e->pinned) {
    e->pinned = 1;
    core->pinned_keys++;
  }
  return 1;
}

int ar_core_unpin(ArCore* core, const char* key, size_t klen) {
  if (!core || !key)
    return 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e || !e->pinned)
    return 0;
  e->pinned = 0;
  if (core->pinned_keys)
    core->pinned_keys--;
  return 1;
}

static size_t collect_pinned_table(ArEntry** table, size_t nb, char** out_keys,
                                   size_t max_out, size_t written) {
  if (!table)
    return written;
  for (size_t i = 0; i < nb && written < max_out; ++i) {
    for (ArEntry* e = table[i]; e && written < max_out; e = e->next) {
      if (!e->pinned)
        continue;
      char* copy = (char*)malloc(e->klen + 1);
      if (!copy)
        continue;
      if (e->klen)
        memcpy(copy, e->key, e->klen);
      copy[e->klen] = '\0';
      out_keys[written++] = copy;
    }
  }
  return written;
}

size_t ar_core_list_pinned(ArCore* core, char** out_keys, size_t max_out) {
  if (!core || !out_keys || max_out == 0)
    return 0;
  size_t n = collect_pinned_table(core->buckets, core->nbuckets, out_keys,
                                  max_out, 0);
  if (core->layout == AR_LAYOUT_HOT_COLD && core->cold_buckets)
    n = collect_pinned_table(core->cold_buckets, core->cold_nbuckets, out_keys,
                             max_out, n);
  return n;
}

/* M12 — per-prefix policy namespace */
int ar_core_policy_set(ArCore* core, const char* prefix, const char* profile) {
  if (!core || !prefix || !profile || !prefix[0] || !profile[0])
    return 0;
  /* Update existing slot if prefix matches */
  for (int i = 0; i < core->policy_n; ++i) {
    if (strncmp(core->policy_pfx[i], prefix, sizeof(core->policy_pfx[i])) == 0) {
      strncpy(core->policy_prof[i], profile, sizeof(core->policy_prof[i]) - 1);
      core->policy_prof[i][sizeof(core->policy_prof[i]) - 1] = '\0';
      return 1;
    }
  }
  if (core->policy_n >= 4)
    return 0;
  int i = core->policy_n++;
  strncpy(core->policy_pfx[i], prefix, sizeof(core->policy_pfx[i]) - 1);
  core->policy_pfx[i][sizeof(core->policy_pfx[i]) - 1] = '\0';
  strncpy(core->policy_prof[i], profile, sizeof(core->policy_prof[i]) - 1);
  core->policy_prof[i][sizeof(core->policy_prof[i]) - 1] = '\0';
  return 1;
}

int ar_core_policy_hints(ArCore* core, char* buf, size_t buflen) {
  if (!core || !buf || buflen == 0)
    return 0;
  buf[0] = '\0';
  size_t used = 0;
  for (int i = 0; i < core->policy_n; ++i) {
    char piece[64];
    int n = snprintf(piece, sizeof(piece), "%s%s=%s",
                     (used ? ";" : ""), core->policy_pfx[i], core->policy_prof[i]);
    if (n < 0)
      break;
    if (used + (size_t)n + 1 > buflen)
      break;
    memcpy(buf + used, piece, (size_t)n);
    used += (size_t)n;
    buf[used] = '\0';
  }
  return (int)used;
}
