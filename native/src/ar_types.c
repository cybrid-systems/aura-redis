/* P3.16 — HASH / LIST / ZSET typed values + memory accounting. */
#include "ar_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

size_t ar_fnv_hash(const char* s, size_t n) {
  size_t h = 1469598103934665603ull;
  for (size_t i = 0; i < n; ++i) {
    h ^= (unsigned char)s[i];
    h *= 1099511628211ull;
  }
  return h;
}

char* ar_xmemdup(const char* s, size_t n) {
  char* p = (char*)malloc(n + 1);
  if (!p)
    return NULL;
  if (n)
    memcpy(p, s, n);
  p[n] = '\0';
  return p;
}

void ar_mem_add(ArCore* core, size_t n) {
  if (core)
    core->used_memory += n;
}

void ar_mem_sub(ArCore* core, size_t n) {
  if (!core)
    return;
  if (core->used_memory >= n)
    core->used_memory -= n;
  else
    core->used_memory = 0;
}

void ar_type_stats_reset(ArCore* core) {
  if (!core)
    return;
  for (int i = 0; i < 4; ++i) {
    core->type_nkeys[i] = 0;
    core->type_bytes[i] = 0;
  }
  core->bigkey_bytes = 0;
  core->bigkey_type = AR_TYPE_STRING;
}

void ar_type_stats_note_bigkey(ArCore* core, uint8_t type, size_t payload) {
  if (!core)
    return;
  if (payload > core->bigkey_bytes) {
    core->bigkey_bytes = payload;
    core->bigkey_type = type;
  }
}

void ar_type_stats_bytes_delta(ArCore* core, uint8_t type, int64_t delta) {
  if (!core || type > AR_TYPE_ZSET)
    return;
  if (delta >= 0)
    core->type_bytes[type] += (uint64_t)delta;
  else {
    uint64_t sub = (uint64_t)(-delta);
    if (core->type_bytes[type] >= sub)
      core->type_bytes[type] -= sub;
    else
      core->type_bytes[type] = 0;
  }
}

void ar_type_stats_add_key(ArCore* core, uint8_t type, size_t bytes) {
  if (!core || type > AR_TYPE_ZSET)
    return;
  core->type_nkeys[type]++;
  core->type_bytes[type] += bytes;
  if (bytes > sizeof(ArEntry))
    ar_type_stats_note_bigkey(core, type, bytes - sizeof(ArEntry));
}

void ar_type_stats_sub_key(ArCore* core, uint8_t type, size_t bytes) {
  if (!core || type > AR_TYPE_ZSET)
    return;
  if (core->type_nkeys[type])
    core->type_nkeys[type]--;
  if (core->type_bytes[type] >= bytes)
    core->type_bytes[type] -= bytes;
  else
    core->type_bytes[type] = 0;
}

/* maybe_evict is static in ar_core.c — call via over_maxmemory + evict_one loop */
void ar_maybe_evict_pub(ArCore* core) {
  if (!core || !core->evict || !core->evict->should_evict)
    return;
  int guard = 0;
  size_t max_rounds = core->nkeys + 16;
  if (max_rounds < 64)
    max_rounds = 64;
  if (max_rounds > 4096)
    max_rounds = 4096;
  while (core->evict->should_evict(core) && guard++ < (int)max_rounds) {
    if (!core->evict->evict_one(core))
      break;
  }
}

const char* ar_type_name(uint8_t t) {
  switch (t) {
    case AR_TYPE_STRING:
      return "string";
    case AR_TYPE_HASH:
      return "hash";
    case AR_TYPE_LIST:
      return "list";
    case AR_TYPE_ZSET:
      return "zset";
    default:
      return "unknown";
  }
}

static size_t hash_obj_bytes(const ArHash* h) {
  if (!h)
    return 0;
  size_t n = sizeof(ArHash) + h->nbuckets * sizeof(ArHashField*);
  for (size_t i = 0; i < h->nbuckets; ++i) {
    for (ArHashField* f = h->buckets[i]; f; f = f->next)
      n += sizeof(ArHashField) + f->flen + f->vlen;
  }
  return n;
}

static size_t list_obj_bytes(const ArList* l) {
  if (!l)
    return 0;
  size_t n = sizeof(ArList);
  for (ArListNode* x = l->head; x; x = x->next)
    n += sizeof(ArListNode) + x->vlen;
  return n;
}

static size_t zset_obj_bytes(const ArZSet* z) {
  if (!z)
    return 0;
  size_t n = sizeof(ArZSet) + z->cap * sizeof(ArZNode);
  for (size_t i = 0; i < z->len; ++i)
    n += z->arr[i].mlen;
  return n;
}

size_t ar_entry_payload_bytes(const ArEntry* e) {
  if (!e)
    return 0;
  switch (e->type) {
    case AR_TYPE_STRING:
      return e->vlen;
    case AR_TYPE_HASH:
      return hash_obj_bytes((const ArHash*)e->obj);
    case AR_TYPE_LIST:
      return list_obj_bytes((const ArList*)e->obj);
    case AR_TYPE_ZSET:
      return zset_obj_bytes((const ArZSet*)e->obj);
    default:
      return 0;
  }
}

static void free_hash(ArHash* h) {
  if (!h)
    return;
  if (h->buckets) {
    for (size_t i = 0; i < h->nbuckets; ++i) {
      ArHashField* f = h->buckets[i];
      while (f) {
        ArHashField* n = f->next;
        free(f->field);
        free(f->val);
        free(f);
        f = n;
      }
    }
    free(h->buckets);
  }
  free(h);
}

static void free_list(ArList* l) {
  if (!l)
    return;
  ArListNode* x = l->head;
  while (x) {
    ArListNode* n = x->next;
    free(x->val);
    free(x);
    x = n;
  }
  free(l);
}

static void free_zset(ArZSet* z) {
  if (!z)
    return;
  if (z->arr) {
    for (size_t i = 0; i < z->len; ++i)
      free(z->arr[i].member);
    free(z->arr);
  }
  free(z);
}

void ar_entry_free_obj(ArEntry* e) {
  if (!e)
    return;
  switch (e->type) {
    case AR_TYPE_HASH:
      free_hash((ArHash*)e->obj);
      break;
    case AR_TYPE_LIST:
      free_list((ArList*)e->obj);
      break;
    case AR_TYPE_ZSET:
      free_zset((ArZSet*)e->obj);
      break;
    default:
      break;
  }
  e->obj = NULL;
  e->type = AR_TYPE_STRING;
  /* string val freed by caller if present */
}

static ArHash* hash_create(void) {
  ArHash* h = (ArHash*)calloc(1, sizeof(ArHash));
  if (!h)
    return NULL;
  h->nbuckets = 16;
  h->buckets = (ArHashField**)calloc(h->nbuckets, sizeof(ArHashField*));
  if (!h->buckets) {
    free(h);
    return NULL;
  }
  return h;
}

static ArHashField* hash_find(ArHash* h, const char* field, size_t flen,
                              size_t* b_out) {
  size_t b = ar_fnv_hash(field, flen) & (h->nbuckets - 1);
  if (b_out)
    *b_out = b;
  for (ArHashField* f = h->buckets[b]; f; f = f->next) {
    if (f->flen == flen && memcmp(f->field, field, flen) == 0)
      return f;
  }
  return NULL;
}

static void hash_maybe_rehash(ArHash* h) {
  if (h->nfields <= h->nbuckets)
    return;
  size_t n2 = h->nbuckets * 2;
  ArHashField** nb = (ArHashField**)calloc(n2, sizeof(ArHashField*));
  if (!nb)
    return;
  for (size_t i = 0; i < h->nbuckets; ++i) {
    ArHashField* f = h->buckets[i];
    while (f) {
      ArHashField* next = f->next;
      size_t b = ar_fnv_hash(f->field, f->flen) & (n2 - 1);
      f->next = nb[b];
      nb[b] = f;
      f = next;
    }
  }
  free(h->buckets);
  h->buckets = nb;
  h->nbuckets = n2;
}

/* Create empty typed entry or return existing; WRONGTYPE if mismatch. */
ArEntry* ar_entry_get_or_create(ArCore* core, const char* key, size_t klen,
                                uint8_t type, size_t* bucket_out, int* tier_out,
                                int* wrongtype) {
  if (wrongtype)
    *wrongtype = 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (e) {
    if (e->type != type) {
      if (wrongtype)
        *wrongtype = 1;
      return NULL;
    }
    if (bucket_out)
      *bucket_out = b;
    if (tier_out)
      *tier_out = tier;
    return e;
  }
  /* create */
  ArEntry* ne = (ArEntry*)calloc(1, sizeof(ArEntry));
  if (!ne)
    return NULL;
  ne->key = ar_xmemdup(key, klen);
  if (!ne->key) {
    free(ne);
    return NULL;
  }
  ne->klen = klen;
  ne->type = type;
  ne->val = NULL;
  ne->vlen = 0;
  ne->last_access = ++core->clock;
  ne->lfu_freq = 5;
  if (type == AR_TYPE_HASH) {
    ne->obj = hash_create();
    if (!ne->obj) {
      free(ne->key);
      free(ne);
      return NULL;
    }
  } else if (type == AR_TYPE_LIST) {
    ArList* l = (ArList*)calloc(1, sizeof(ArList));
    if (!l) {
      free(ne->key);
      free(ne);
      return NULL;
    }
    ne->obj = l;
  } else if (type == AR_TYPE_ZSET) {
    ArZSet* z = (ArZSet*)calloc(1, sizeof(ArZSet));
    if (!z) {
      free(ne->key);
      free(ne);
      return NULL;
    }
    ne->obj = z;
  }
  b = ar_fnv_hash(key, klen) & (core->nbuckets - 1);
  ne->next = core->buckets[b];
  core->buckets[b] = ne;
  core->nkeys++;
  if (core->layout == AR_LAYOUT_HOT_COLD)
    core->hot_nkeys++;
  {
    size_t tot = klen + sizeof(ArEntry) + ar_entry_payload_bytes(ne);
    ar_mem_add(core, tot);
    ar_type_stats_add_key(core, type, tot);
    ar_type_stats_note_bigkey(core, type, ar_entry_payload_bytes(ne));
  }
  if (core->evict && core->evict->on_set)
    core->evict->on_set(core, ne);
  ar_rehash_if_needed(core);
  ar_maybe_evict_pub(core);
  /* re-find after possible rehash */
  e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (bucket_out)
    *bucket_out = b;
  if (tier_out)
    *tier_out = tier;
  return e;
}

ArEntry* ar_entry_get_typed(ArCore* core, const char* key, size_t klen,
                            uint8_t want, size_t* bucket_out, int* tier_out,
                            int* wrongtype) {
  if (wrongtype)
    *wrongtype = 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e)
    return NULL;
  if (e->type != want) {
    if (wrongtype)
      *wrongtype = 1;
    return NULL;
  }
  if (bucket_out)
    *bucket_out = b;
  if (tier_out)
    *tier_out = tier;
  return e;
}

int ar_entry_ensure_type(ArCore* core, ArEntry* e, uint8_t want) {
  (void)core;
  if (!e)
    return 0;
  return e->type == want ? 0 : -1;
}

/* ---------- HASH ---------- */

int ar_hash_hset(ArCore* core, const char* key, size_t klen, int nfields,
                 const char** fields, const size_t* flens, const char** vals,
                 const size_t* vlens, int* wrongtype) {
  if (wrongtype)
    *wrongtype = 0;
  if (!core || nfields <= 0)
    return 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e =
      ar_entry_get_or_create(core, key, klen, AR_TYPE_HASH, &b, &tier, wrongtype);
  if (!e)
    return -1;
  ArHash* h = (ArHash*)e->obj;
  int added = 0;
  size_t old_pay = hash_obj_bytes(h);
  for (int i = 0; i < nfields; ++i) {
    size_t fb = 0;
    ArHashField* f = hash_find(h, fields[i], flens[i], &fb);
    if (f) {
      char* nv = ar_xmemdup(vals[i], vlens[i]);
      if (!nv)
        continue;
      free(f->val);
      f->val = nv;
      f->vlen = vlens[i];
    } else {
      ArHashField* nf = (ArHashField*)calloc(1, sizeof(ArHashField));
      if (!nf)
        continue;
      nf->field = ar_xmemdup(fields[i], flens[i]);
      nf->val = ar_xmemdup(vals[i], vlens[i]);
      if (!nf->field || !nf->val) {
        free(nf->field);
        free(nf->val);
        free(nf);
        continue;
      }
      nf->flen = flens[i];
      nf->vlen = vlens[i];
      nf->next = h->buckets[fb];
      h->buckets[fb] = nf;
      h->nfields++;
      added++;
      hash_maybe_rehash(h);
      /* refresh find bucket after rehash */
      h = (ArHash*)e->obj;
    }
  }
  size_t new_pay = hash_obj_bytes(h);
  if (new_pay >= old_pay) {
    ar_mem_add(core, new_pay - old_pay);
    ar_type_stats_bytes_delta(core, e->type, (int64_t)(new_pay - old_pay));
    ar_type_stats_note_bigkey(core, e->type, new_pay);
  } else {
    ar_mem_sub(core, old_pay - new_pay);
    ar_type_stats_bytes_delta(core, e->type, -(int64_t)(old_pay - new_pay));
  }
  e->last_access = ++core->clock;
  ar_maybe_evict_pub(core);
  ar_watch_touch(core, key, klen);
  return added;
}

char* ar_hash_hget(ArCore* core, const char* key, size_t klen,
                   const char* field, size_t flen, size_t* out_len,
                   int* wrongtype) {
  if (out_len)
    *out_len = 0;
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_HASH, NULL, NULL, wrongtype);
  if (!e || *wrongtype)
    return NULL;
  ArHash* h = (ArHash*)e->obj;
  ArHashField* f = hash_find(h, field, flen, NULL);
  if (!f)
    return NULL;
  if (out_len)
    *out_len = f->vlen;
  return ar_xmemdup(f->val, f->vlen);
}

int ar_hash_hdel(ArCore* core, const char* key, size_t klen, int nfields,
                 const char** fields, const size_t* flens, int* wrongtype) {
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_HASH, NULL, NULL, wrongtype);
  if (!e)
    return 0;
  if (wrongtype && *wrongtype)
    return -1;
  ArHash* h = (ArHash*)e->obj;
  size_t old_pay = hash_obj_bytes(h);
  int removed = 0;
  for (int i = 0; i < nfields; ++i) {
    size_t fb = ar_fnv_hash(fields[i], flens[i]) & (h->nbuckets - 1);
    ArHashField** pp = &h->buckets[fb];
    while (*pp) {
      if ((*pp)->flen == flens[i] &&
          memcmp((*pp)->field, fields[i], flens[i]) == 0) {
        ArHashField* dead = *pp;
        *pp = dead->next;
        free(dead->field);
        free(dead->val);
        free(dead);
        h->nfields--;
        removed++;
        break;
      }
      pp = &(*pp)->next;
    }
  }
  size_t new_pay = hash_obj_bytes(h);
  if (old_pay >= new_pay) {
    ar_mem_sub(core, old_pay - new_pay);
    ar_type_stats_bytes_delta(core, e->type, -(int64_t)(old_pay - new_pay));
  }
  /* delete empty hash key */
  if (h->nfields == 0) {
    size_t b = 0;
    int tier = 0;
    ArEntry* ee = ar_find_entry_ex(core, key, klen, &b, &tier);
    if (ee)
      ar_entry_free_ex(core, b, tier, ee);
  } else if (removed > 0) {
    ar_watch_touch(core, key, klen);
  }
  return removed;
}

int ar_hash_hexists(ArCore* core, const char* key, size_t klen,
                    const char* field, size_t flen, int* wrongtype) {
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_HASH, NULL, NULL, wrongtype);
  if (!e)
    return 0;
  if (wrongtype && *wrongtype)
    return -1;
  return hash_find((ArHash*)e->obj, field, flen, NULL) ? 1 : 0;
}

int64_t ar_hash_hlen(ArCore* core, const char* key, size_t klen,
                     int* wrongtype) {
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_HASH, NULL, NULL, wrongtype);
  if (!e)
    return 0;
  if (wrongtype && *wrongtype)
    return -1;
  return (int64_t)((ArHash*)e->obj)->nfields;
}

int64_t ar_hash_hincrby(ArCore* core, const char* key, size_t klen,
                        const char* field, size_t flen, int64_t incr,
                        int* wrongtype, int* notint) {
  if (notint)
    *notint = 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e =
      ar_entry_get_or_create(core, key, klen, AR_TYPE_HASH, &b, &tier, wrongtype);
  if (!e)
    return 0;
  ArHash* h = (ArHash*)e->obj;
  size_t old_pay = hash_obj_bytes(h);
  size_t fb = 0;
  ArHashField* f = hash_find(h, field, flen, &fb);
  int64_t v = 0;
  if (f) {
    char tmp[64];
    if (f->vlen >= sizeof(tmp)) {
      if (notint)
        *notint = 1;
      return 0;
    }
    memcpy(tmp, f->val, f->vlen);
    tmp[f->vlen] = '\0';
    char* end = NULL;
    v = strtoll(tmp, &end, 10);
    if (end == tmp || *end != '\0') {
      if (notint)
        *notint = 1;
      return 0;
    }
  } else {
    f = (ArHashField*)calloc(1, sizeof(ArHashField));
    if (!f)
      return 0;
    f->field = ar_xmemdup(field, flen);
    if (!f->field) {
      free(f);
      return 0;
    }
    f->flen = flen;
    f->val = ar_xmemdup("0", 1);
    f->vlen = 1;
    f->next = h->buckets[fb];
    h->buckets[fb] = f;
    h->nfields++;
  }
  v += incr;
  char buf[32];
  int n = snprintf(buf, sizeof(buf), "%lld", (long long)v);
  char* nv = ar_xmemdup(buf, (size_t)n);
  if (!nv)
    return 0;
  free(f->val);
  f->val = nv;
  f->vlen = (size_t)n;
  size_t new_pay = hash_obj_bytes(h);
  if (new_pay >= old_pay) {
    ar_mem_add(core, new_pay - old_pay);
    ar_type_stats_bytes_delta(core, e->type, (int64_t)(new_pay - old_pay));
    ar_type_stats_note_bigkey(core, e->type, new_pay);
  } else {
    ar_mem_sub(core, old_pay - new_pay);
    ar_type_stats_bytes_delta(core, e->type, -(int64_t)(old_pay - new_pay));
  }
  e->last_access = ++core->clock;
  ar_maybe_evict_pub(core);
  ar_watch_touch(core, key, klen);
  return v;
}

/* ---------- LIST ---------- */

int64_t ar_list_push(ArCore* core, const char* key, size_t klen, int left,
                     int nvals, const char** vals, const size_t* vlens,
                     int* wrongtype) {
  if (wrongtype)
    *wrongtype = 0;
  if (!core || nvals <= 0)
    return 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e =
      ar_entry_get_or_create(core, key, klen, AR_TYPE_LIST, &b, &tier, wrongtype);
  if (!e)
    return -1;
  ArList* l = (ArList*)e->obj;
  size_t old_pay = list_obj_bytes(l);
  for (int i = 0; i < nvals; ++i) {
    ArListNode* node = (ArListNode*)calloc(1, sizeof(ArListNode));
    if (!node)
      continue;
    node->val = ar_xmemdup(vals[i], vlens[i]);
    if (!node->val) {
      free(node);
      continue;
    }
    node->vlen = vlens[i];
    if (left) {
      node->next = l->head;
      node->prev = NULL;
      if (l->head)
        l->head->prev = node;
      else
        l->tail = node;
      l->head = node;
    } else {
      node->prev = l->tail;
      node->next = NULL;
      if (l->tail)
        l->tail->next = node;
      else
        l->head = node;
      l->tail = node;
    }
    l->len++;
  }
  size_t new_pay = list_obj_bytes(l);
  if (new_pay >= old_pay) {
    ar_mem_add(core, new_pay - old_pay);
    ar_type_stats_bytes_delta(core, e->type, (int64_t)(new_pay - old_pay));
    ar_type_stats_note_bigkey(core, e->type, new_pay);
  } else {
    ar_mem_sub(core, old_pay - new_pay);
    ar_type_stats_bytes_delta(core, e->type, -(int64_t)(old_pay - new_pay));
  }
  e->last_access = ++core->clock;
  ar_maybe_evict_pub(core);
  ar_watch_touch(core, key, klen);
  return (int64_t)l->len;
}

char* ar_list_pop(ArCore* core, const char* key, size_t klen, int left,
                  size_t* out_len, int* wrongtype) {
  if (out_len)
    *out_len = 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_LIST, &b, &tier, wrongtype);
  if (!e)
    return NULL;
  if (wrongtype && *wrongtype)
    return NULL;
  ArList* l = (ArList*)e->obj;
  if (l->len == 0)
    return NULL;
  size_t old_pay = list_obj_bytes(l);
  ArListNode* node = left ? l->head : l->tail;
  if (left) {
    l->head = node->next;
    if (l->head)
      l->head->prev = NULL;
    else
      l->tail = NULL;
  } else {
    l->tail = node->prev;
    if (l->tail)
      l->tail->next = NULL;
    else
      l->head = NULL;
  }
  l->len--;
  char* out = node->val;
  size_t ol = node->vlen;
  node->val = NULL;
  free(node);
  size_t new_pay = list_obj_bytes(l);
  if (old_pay >= new_pay) {
    ar_mem_sub(core, old_pay - new_pay);
    ar_type_stats_bytes_delta(core, e->type, -(int64_t)(old_pay - new_pay));
  }
  if (l->len == 0) {
    /* free empty list key — transfer ownership of out first */
    if (out_len)
      *out_len = ol;
    ar_entry_free_ex(core, b, tier, e);
    return out;
  }
  ar_watch_touch(core, key, klen);
  if (out_len)
    *out_len = ol;
  return out;
}

int64_t ar_list_llen(ArCore* core, const char* key, size_t klen,
                     int* wrongtype) {
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_LIST, NULL, NULL, wrongtype);
  if (!e)
    return 0;
  if (wrongtype && *wrongtype)
    return -1;
  return (int64_t)((ArList*)e->obj)->len;
}

char* ar_list_lindex(ArCore* core, const char* key, size_t klen, int64_t index,
                     size_t* out_len, int* wrongtype) {
  if (out_len)
    *out_len = 0;
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_LIST, NULL, NULL, wrongtype);
  if (!e)
    return NULL;
  if (wrongtype && *wrongtype)
    return NULL;
  ArList* l = (ArList*)e->obj;
  if (l->len == 0)
    return NULL;
  if (index < 0)
    index = (int64_t)l->len + index;
  if (index < 0 || (size_t)index >= l->len)
    return NULL;
  ArListNode* node = l->head;
  for (int64_t i = 0; i < index && node; ++i)
    node = node->next;
  if (!node)
    return NULL;
  if (out_len)
    *out_len = node->vlen;
  return ar_xmemdup(node->val, node->vlen);
}

ArList* ar_list_get(ArCore* core, const char* key, size_t klen,
                    int* wrongtype) {
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_LIST, NULL, NULL, wrongtype);
  if (!e)
    return NULL;
  if (wrongtype && *wrongtype)
    return NULL;
  return (ArList*)e->obj;
}

/* ---------- ZSET (sorted array) ---------- */

static int zcmp(double sa, const char* ma, size_t mla, double sb,
                const char* mb, size_t mlb) {
  if (sa < sb)
    return -1;
  if (sa > sb)
    return 1;
  size_t n = mla < mlb ? mla : mlb;
  int c = memcmp(ma, mb, n);
  if (c != 0)
    return c;
  if (mla < mlb)
    return -1;
  if (mla > mlb)
    return 1;
  return 0;
}

static ssize_t zfind_member(ArZSet* z, const char* member, size_t mlen) {
  for (size_t i = 0; i < z->len; ++i) {
    if (z->arr[i].mlen == mlen && memcmp(z->arr[i].member, member, mlen) == 0)
      return (ssize_t)i;
  }
  return -1;
}

static int zensure_cap(ArZSet* z, size_t need) {
  if (z->cap >= need)
    return 1;
  size_t nc = z->cap ? z->cap * 2 : 8;
  while (nc < need)
    nc *= 2;
  ArZNode* na = (ArZNode*)realloc(z->arr, nc * sizeof(ArZNode));
  if (!na)
    return 0;
  z->arr = na;
  z->cap = nc;
  return 1;
}

int ar_zset_zadd(ArCore* core, const char* key, size_t klen, int n,
                 const double* scores, const char** members,
                 const size_t* mlens, int* wrongtype) {
  if (wrongtype)
    *wrongtype = 0;
  if (!core || n <= 0)
    return 0;
  size_t b = 0;
  int tier = 0;
  ArEntry* e =
      ar_entry_get_or_create(core, key, klen, AR_TYPE_ZSET, &b, &tier, wrongtype);
  if (!e)
    return -1;
  ArZSet* z = (ArZSet*)e->obj;
  size_t old_pay = zset_obj_bytes(z);
  int added = 0;
  for (int i = 0; i < n; ++i) {
    ssize_t idx = zfind_member(z, members[i], mlens[i]);
    if (idx >= 0) {
      /* update score: remove and reinsert */
      free(z->arr[idx].member);
      memmove(&z->arr[idx], &z->arr[idx + 1],
              (z->len - (size_t)idx - 1) * sizeof(ArZNode));
      z->len--;
    } else {
      added++;
    }
    if (!zensure_cap(z, z->len + 1))
      continue;
    /* find insert pos */
    size_t pos = 0;
    while (pos < z->len &&
           zcmp(z->arr[pos].score, z->arr[pos].member, z->arr[pos].mlen,
                scores[i], members[i], mlens[i]) < 0)
      pos++;
    memmove(&z->arr[pos + 1], &z->arr[pos], (z->len - pos) * sizeof(ArZNode));
    z->arr[pos].member = ar_xmemdup(members[i], mlens[i]);
    z->arr[pos].mlen = mlens[i];
    z->arr[pos].score = scores[i];
    if (!z->arr[pos].member) {
      /* rollback slot */
      memmove(&z->arr[pos], &z->arr[pos + 1], (z->len - pos) * sizeof(ArZNode));
      if (idx < 0)
        added--;
      continue;
    }
    z->len++;
  }
  size_t new_pay = zset_obj_bytes(z);
  if (new_pay >= old_pay) {
    ar_mem_add(core, new_pay - old_pay);
    ar_type_stats_bytes_delta(core, e->type, (int64_t)(new_pay - old_pay));
    ar_type_stats_note_bigkey(core, e->type, new_pay);
  } else {
    ar_mem_sub(core, old_pay - new_pay);
    ar_type_stats_bytes_delta(core, e->type, -(int64_t)(old_pay - new_pay));
  }
  e->last_access = ++core->clock;
  ar_maybe_evict_pub(core);
  ar_watch_touch(core, key, klen);
  return added;
}

char* ar_zset_zscore(ArCore* core, const char* key, size_t klen,
                     const char* member, size_t mlen, size_t* out_len,
                     int* wrongtype) {
  if (out_len)
    *out_len = 0;
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_ZSET, NULL, NULL, wrongtype);
  if (!e)
    return NULL;
  if (wrongtype && *wrongtype)
    return NULL;
  ArZSet* z = (ArZSet*)e->obj;
  ssize_t idx = zfind_member(z, member, mlen);
  if (idx < 0)
    return NULL;
  char buf[64];
  int n = snprintf(buf, sizeof(buf), "%.17g", z->arr[idx].score);
  if (n < 0)
    return NULL;
  if (out_len)
    *out_len = (size_t)n;
  return ar_xmemdup(buf, (size_t)n);
}

int ar_zset_zrem(ArCore* core, const char* key, size_t klen, int n,
                 const char** members, const size_t* mlens, int* wrongtype) {
  size_t b = 0;
  int tier = 0;
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_ZSET, &b, &tier, wrongtype);
  if (!e)
    return 0;
  if (wrongtype && *wrongtype)
    return -1;
  ArZSet* z = (ArZSet*)e->obj;
  size_t old_pay = zset_obj_bytes(z);
  int removed = 0;
  for (int i = 0; i < n; ++i) {
    ssize_t idx = zfind_member(z, members[i], mlens[i]);
    if (idx < 0)
      continue;
    free(z->arr[idx].member);
    memmove(&z->arr[idx], &z->arr[idx + 1],
            (z->len - (size_t)idx - 1) * sizeof(ArZNode));
    z->len--;
    removed++;
  }
  size_t new_pay = zset_obj_bytes(z);
  if (old_pay >= new_pay) {
    ar_mem_sub(core, old_pay - new_pay);
    ar_type_stats_bytes_delta(core, e->type, -(int64_t)(old_pay - new_pay));
  }
  if (z->len == 0)
    ar_entry_free_ex(core, b, tier, e);
  else if (removed > 0)
    ar_watch_touch(core, key, klen);
  return removed;
}

int64_t ar_zset_zcard(ArCore* core, const char* key, size_t klen,
                      int* wrongtype) {
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_ZSET, NULL, NULL, wrongtype);
  if (!e)
    return 0;
  if (wrongtype && *wrongtype)
    return -1;
  return (int64_t)((ArZSet*)e->obj)->len;
}

ArZSet* ar_zset_get(ArCore* core, const char* key, size_t klen,
                    int* wrongtype) {
  ArEntry* e =
      ar_entry_get_typed(core, key, klen, AR_TYPE_ZSET, NULL, NULL, wrongtype);
  if (!e)
    return NULL;
  if (wrongtype && *wrongtype)
    return NULL;
  return (ArZSet*)e->obj;
}

/* ---------- Thin public FFI wrappers (C-string keys for Aura) ---------- */

const char* ar_type(ArCore* core, const char* key) {
  if (!core || !key)
    return "none";
  ArEntry* e = ar_find_entry(core, key, strlen(key), NULL);
  if (!e)
    return "none";
  return ar_type_name(e->type);
}

int64_t ar_hset(ArCore* core, const char* key, const char* field, const char* val,
               int* wrongtype) {
  if (!core || !key || !field || !val)
    return -1;
  int wt = 0;
  const char* fields[1] = {field};
  size_t flens[1] = {strlen(field)};
  const char* vals[1] = {val};
  size_t vlens[1] = {strlen(val)};
  int added = ar_hash_hset(core, key, strlen(key), 1, fields, flens, vals, vlens, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : added;
}

char* ar_hget(ArCore* core, const char* key, const char* field, int* wrongtype) {
  if (!core || !key || !field) {
    if (wrongtype)
      *wrongtype = 0;
    return NULL;
  }
  int wt = 0;
  size_t ol = 0;
  char* v = ar_hash_hget(core, key, strlen(key), field, strlen(field), &ol, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return v;
}

int64_t ar_hdel(ArCore* core, const char* key, const char* field, int* wrongtype) {
  if (!core || !key || !field)
    return 0;
  int wt = 0;
  const char* fields[1] = {field};
  size_t flens[1] = {strlen(field)};
  int n = ar_hash_hdel(core, key, strlen(key), 1, fields, flens, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : n;
}

int64_t ar_hexists(ArCore* core, const char* key, const char* field, int* wrongtype) {
  if (!core || !key || !field)
    return 0;
  int wt = 0;
  int r = ar_hash_hexists(core, key, strlen(key), field, strlen(field), &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : r;
}

int64_t ar_hlen(ArCore* core, const char* key, int* wrongtype) {
  if (!core || !key)
    return 0;
  int wt = 0;
  int64_t n = ar_hash_hlen(core, key, strlen(key), &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : n;
}

int64_t ar_hincrby(ArCore* core, const char* key, const char* field, int64_t incr,
                   int* wrongtype, int* ok) {
  if (ok)
    *ok = 0;
  if (!core || !key || !field) {
    if (wrongtype)
      *wrongtype = 0;
    return 0;
  }
  int wt = 0, ni = 0;
  int64_t v =
      ar_hash_hincrby(core, key, strlen(key), field, strlen(field), incr, &wt, &ni);
  if (wrongtype)
    *wrongtype = wt;
  if (ok)
    *ok = (wt || ni) ? 0 : 1;
  return v;
}

int64_t ar_lpush(ArCore* core, const char* key, const char* val, int* wrongtype) {
  if (!core || !key || !val)
    return -1;
  int wt = 0;
  const char* vals[1] = {val};
  size_t vlens[1] = {strlen(val)};
  int64_t n = ar_list_push(core, key, strlen(key), 1, 1, vals, vlens, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : n;
}

int64_t ar_rpush(ArCore* core, const char* key, const char* val, int* wrongtype) {
  if (!core || !key || !val)
    return -1;
  int wt = 0;
  const char* vals[1] = {val};
  size_t vlens[1] = {strlen(val)};
  int64_t n = ar_list_push(core, key, strlen(key), 0, 1, vals, vlens, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : n;
}

char* ar_lpop(ArCore* core, const char* key, int* wrongtype) {
  if (!core || !key) {
    if (wrongtype)
      *wrongtype = 0;
    return NULL;
  }
  int wt = 0;
  size_t ol = 0;
  char* v = ar_list_pop(core, key, strlen(key), 1, &ol, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return v;
}

char* ar_rpop(ArCore* core, const char* key, int* wrongtype) {
  if (!core || !key) {
    if (wrongtype)
      *wrongtype = 0;
    return NULL;
  }
  int wt = 0;
  size_t ol = 0;
  char* v = ar_list_pop(core, key, strlen(key), 0, &ol, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return v;
}

int64_t ar_llen(ArCore* core, const char* key, int* wrongtype) {
  if (!core || !key)
    return 0;
  int wt = 0;
  int64_t n = ar_list_llen(core, key, strlen(key), &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : n;
}

char* ar_lindex(ArCore* core, const char* key, int64_t index, int* wrongtype) {
  if (!core || !key) {
    if (wrongtype)
      *wrongtype = 0;
    return NULL;
  }
  int wt = 0;
  size_t ol = 0;
  char* v = ar_list_lindex(core, key, strlen(key), index, &ol, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return v;
}

int64_t ar_zadd(ArCore* core, const char* key, int64_t score, const char* member,
               int* wrongtype) {
  if (!core || !key || !member)
    return -1;
  int wt = 0;
  double scores[1] = {(double)score};
  const char* members[1] = {member};
  size_t mlens[1] = {strlen(member)};
  int added = ar_zset_zadd(core, key, strlen(key), 1, scores, members, mlens, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : added;
}

char* ar_zscore(ArCore* core, const char* key, const char* member, int* ok,
                int* wrongtype) {
  if (ok)
    *ok = 0;
  if (!core || !key || !member) {
    if (wrongtype)
      *wrongtype = 0;
    return NULL;
  }
  int wt = 0;
  size_t ol = 0;
  char* v = ar_zset_zscore(core, key, strlen(key), member, strlen(member), &ol, &wt);
  if (wrongtype)
    *wrongtype = wt;
  if (ok)
    *ok = (wt || !v) ? 0 : 1;
  return v;
}

int64_t ar_zrem(ArCore* core, const char* key, const char* member, int* wrongtype) {
  if (!core || !key || !member)
    return 0;
  int wt = 0;
  const char* members[1] = {member};
  size_t mlens[1] = {strlen(member)};
  int n = ar_zset_zrem(core, key, strlen(key), 1, members, mlens, &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : n;
}

int64_t ar_zcard(ArCore* core, const char* key, int* wrongtype) {
  if (!core || !key)
    return 0;
  int wt = 0;
  int64_t n = ar_zset_zcard(core, key, strlen(key), &wt);
  if (wrongtype)
    *wrongtype = wt;
  return wt ? -1 : n;
}
