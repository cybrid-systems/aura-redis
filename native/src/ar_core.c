#include "ar_internal.h"
#include <stdint.h>
#include <limits.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>

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

/* --- LRU (Iteration 5, also linked early as stub-ready) --- */
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
  /* Redis-like: sample up to 16 random entries; evict oldest last_access. */
  ArEntry* best = NULL;
  size_t best_b = 0;
  uint64_t best_t = UINT64_MAX;
  int samples = 0;
  if (db->nkeys == 0)
    return 0;
  for (int attempt = 0; attempt < 64 && samples < 16; ++attempt) {
    size_t i = (size_t)(rand() % (int)db->nbuckets);
    for (ArEntry* e = db->buckets[i]; e && samples < 16; e = e->next) {
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
  ar_entry_free(db, best_b, best);
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
    e->lfu_freq = 5; /* initial */
}
static int evict_lfu_should(ArCore* db) {
  return db->maxmemory > 0 && db->used_memory > db->maxmemory;
}
static int evict_lfu_one(ArCore* db) {
  ArEntry* best = NULL;
  size_t best_b = 0;
  uint8_t best_f = 255;
  uint64_t best_t = UINT64_MAX;
  int samples = 0;
  if (db->nkeys == 0)
    return 0;
  for (int attempt = 0; attempt < 64 && samples < 16; ++attempt) {
    size_t i = (size_t)(rand() % (int)db->nbuckets);
    for (ArEntry* e = db->buckets[i]; e && samples < 16; e = e->next) {
      samples++;
      if (e->lfu_freq < best_f ||
          (e->lfu_freq == best_f && e->last_access < best_t)) {
        best_f = e->lfu_freq;
        best_t = e->last_access;
        best = e;
        best_b = i;
      }
    }
  }
  if (!best)
    return 0;
  ar_entry_free(db, best_b, best);
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
  int guard = 0;
  while (core->evict->should_evict(core) && guard++ < 64) {
    if (!core->evict->evict_one || !core->evict->evict_one(core))
      break;
  }
}

ArEntry* ar_find_entry(ArCore* core, const char* key, size_t klen,
                       size_t* bucket_out) {
  size_t b = hash_bin(key, klen) & (core->nbuckets - 1);
  if (bucket_out)
    *bucket_out = b;
  for (ArEntry* e = core->buckets[b]; e; e = e->next) {
    if (key_eq(e, key, klen))
      return e;
  }
  return NULL;
}

void ar_entry_free(ArCore* core, size_t bucket, ArEntry* e) {
  ArEntry** pp = &core->buckets[bucket];
  while (*pp) {
    if (*pp == e) {
      *pp = e->next;
      core->used_memory -= e->klen + e->vlen + sizeof(ArEntry);
      free(e->key);
      free(e->val);
      free(e);
      core->nkeys--;
      return;
    }
    pp = &(*pp)->next;
  }
}

void ar_rehash_if_needed(ArCore* core) {
  if (core->nkeys <= core->nbuckets)
    return;
  size_t n2 = core->nbuckets * 2;
  ArEntry** nb = (ArEntry**)calloc(n2, sizeof(ArEntry*));
  if (!nb)
    return;
  for (size_t i = 0; i < core->nbuckets; ++i) {
    ArEntry* e = core->buckets[i];
    while (e) {
      ArEntry* next = e->next;
      size_t b = hash_bin(e->key, e->klen) & (n2 - 1);
      e->next = nb[b];
      nb[b] = e;
      e = next;
    }
  }
  free(core->buckets);
  core->buckets = nb;
  core->nbuckets = n2;
}

int ar_entry_set(ArCore* core, const char* key, size_t klen, const char* val,
                 size_t vlen) {
  size_t b = 0;
  ArEntry* e = ar_find_entry(core, key, klen, &b);
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
  ne->next = core->buckets[b];
  core->buckets[b] = ne;
  core->nkeys++;
  core->used_memory += klen + vlen + sizeof(ArEntry);
  if (core->evict && core->evict->on_set)
    core->evict->on_set(core, ne);
  ar_rehash_if_needed(core);
  maybe_evict(core);
  return 1;
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
  c->evict = &kEvictNoop;
  c->evict_plugin = NULL;
  c->listen_fd = -1;
  c->epfd = -1;
  return c;
}

void ar_core_destroy(ArCore* core) {
  if (!core)
    return;
  if (core->listen_fd >= 0) {
    /* close handled in server module if linked; best-effort here */
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

int ar_set(ArCore* core, const char* key, const char* val) {
  if (!key || !val)
    return 0;
  return ar_set_bin(core, key, strlen(key), val, strlen(val));
}

char* ar_get_bin(ArCore* core, const char* key, size_t klen, size_t* out_len) {
  if (!core || !key)
    return NULL;
  core->ops++;
  core->gets++;
  ArEntry* e = ar_find_entry(core, key, klen, NULL);
  if (!e) {
    core->misses++;
    if (out_len)
      *out_len = 0;
    return NULL;
  }
  core->hits++;
  e->last_access = ++core->clock;
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
  ArEntry* e = ar_find_entry(core, key, klen, &b);
  if (!e)
    return 0;
  ar_entry_free(core, b, e);
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
  ArEntry* e = ar_find_entry(core, key, strlen(key), NULL);
  if (!e)
    return 0;
  core->ops++;
  core->gets++;
  core->hits++;
  e->last_access = ++core->clock;
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
    /* ensure null-terminated for strtoll */
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
  core->nkeys = 0;
  core->used_memory = 0;
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


int ar_core_over_maxmemory(ArCore* core) {
  return core && core->maxmemory > 0 && core->used_memory > core->maxmemory;
}

int ar_core_evict_random_one(ArCore* core) {
  if (!core || core->nkeys == 0)
    return 0;
  /* Sample up to 16 occupied buckets; free first entry found after random start. */
  size_t start = (size_t)(core->clock++ % core->nbuckets);
  for (size_t n = 0; n < core->nbuckets && n < 64; ++n) {
    size_t b = (start + n) % core->nbuckets;
    ArEntry* e = core->buckets[b];
    if (!e)
      continue;
    ar_entry_free(core, b, e);
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
  if (old)
    dlclose(old);
  fprintf(stderr, "ar_core: loaded eviction plugin %s name=%s\n", so_path,
          ops->name ? ops->name : "?");
  maybe_evict(core);
  return 1;
}
