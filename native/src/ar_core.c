#include "ar_core.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

typedef struct ArEntry {
  char* key;
  char* val;
  uint64_t last_access; /* for future LRU */
  struct ArEntry* next;
} ArEntry;

struct ArCore {
  ArEntry** buckets;
  size_t nbuckets;
  size_t nkeys;
  const ArEvictOps* evict;
  uint64_t ops, gets, sets, hits, misses;
  uint64_t clock;
};

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

static size_t hash_str(const char* s) {
  size_t h = 1469598103934665603ull;
  for (; *s; ++s) {
    h ^= (unsigned char)(*s);
    h *= 1099511628211ull;
  }
  return h;
}

static char* xstrdup(const char* s) {
  size_t n = strlen(s) + 1;
  char* p = (char*)malloc(n);
  if (p)
    memcpy(p, s, n);
  return p;
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
  return c;
}

void ar_core_destroy(ArCore* core) {
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
  }
  free(core->buckets);
  free(core);
}

static ArEntry* find_entry(ArCore* core, const char* key, size_t* bucket_out) {
  size_t b = hash_str(key) & (core->nbuckets - 1);
  if (bucket_out)
    *bucket_out = b;
  for (ArEntry* e = core->buckets[b]; e; e = e->next) {
    if (strcmp(e->key, key) == 0)
      return e;
  }
  return NULL;
}

int ar_set(ArCore* core, const char* key, const char* val) {
  if (!core || !key || !val)
    return 0;
  core->ops++;
  core->sets++;
  size_t b = 0;
  ArEntry* e = find_entry(core, key, &b);
  if (e) {
    char* nv = xstrdup(val);
    if (!nv)
      return 0;
    free(e->val);
    e->val = nv;
    e->last_access = ++core->clock;
    if (core->evict && core->evict->on_set)
      core->evict->on_set(core, e);
    return 1;
  }
  ArEntry* ne = (ArEntry*)calloc(1, sizeof(ArEntry));
  if (!ne)
    return 0;
  ne->key = xstrdup(key);
  ne->val = xstrdup(val);
  if (!ne->key || !ne->val) {
    free(ne->key);
    free(ne->val);
    free(ne);
    return 0;
  }
  ne->last_access = ++core->clock;
  ne->next = core->buckets[b];
  core->buckets[b] = ne;
  core->nkeys++;
  if (core->evict && core->evict->on_set)
    core->evict->on_set(core, ne);
  if (core->evict && core->evict->should_evict && core->evict->should_evict(core))
    core->evict->evict_one(core);
  return 1;
}

char* ar_get(ArCore* core, const char* key) {
  if (!core || !key)
    return NULL;
  core->ops++;
  core->gets++;
  ArEntry* e = find_entry(core, key, NULL);
  if (!e) {
    core->misses++;
    return NULL;
  }
  core->hits++;
  e->last_access = ++core->clock;
  if (core->evict && core->evict->on_get)
    core->evict->on_get(core, e);
  return xstrdup(e->val);
}

int ar_del(ArCore* core, const char* key) {
  if (!core || !key)
    return 0;
  size_t b = 0;
  ArEntry* e = find_entry(core, key, &b);
  if (!e)
    return 0;
  ArEntry** pp = &core->buckets[b];
  while (*pp) {
    if (*pp == e) {
      *pp = e->next;
      free(e->key);
      free(e->val);
      free(e);
      core->nkeys--;
      core->ops++;
      return 1;
    }
    pp = &(*pp)->next;
  }
  return 0;
}

int ar_exists(ArCore* core, const char* key) {
  if (!core || !key)
    return 0;
  return find_entry(core, key, NULL) ? 1 : 0;
}

void ar_free(char* p) { free(p); }

int ar_get_eq(ArCore* core, const char* key, const char* expect) {
  if (!core || !key || !expect)
    return 0;
  ArEntry* e = find_entry(core, key, NULL);
  if (!e)
    return 0;
  core->ops++;
  core->gets++;
  core->hits++;
  e->last_access = ++core->clock;
  if (core->evict && core->evict->on_get)
    core->evict->on_get(core, e);
  return strcmp(e->val, expect) == 0 ? 1 : 0;
}

int ar_ping(ArCore* core) {
  (void)core;
  return 1;
}

int ar_core_set_evict_by_name(ArCore* core, const char* name) {
  if (!core || !name)
    return 0;
  if (strcmp(name, "noop") == 0) {
    core->evict = &kEvictNoop;
    return 1;
  }
  /* lru/lfu/adaptive in later iterations */
  return 0;
}

const char* ar_core_evict_name(ArCore* core) {
  if (!core || !core->evict)
    return "none";
  return core->evict->name ? core->evict->name : "none";
}

uint64_t ar_metric_ops(ArCore* core) { return core ? core->ops : 0; }
uint64_t ar_metric_gets(ArCore* core) { return core ? core->gets : 0; }
uint64_t ar_metric_sets(ArCore* core) { return core ? core->sets : 0; }
uint64_t ar_metric_hits(ArCore* core) { return core ? core->hits : 0; }
uint64_t ar_metric_misses(ArCore* core) { return core ? core->misses : 0; }
