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
  struct ArEntry* next;
} ArEntry;

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
  ArEntry** buckets;
  size_t nbuckets;
  size_t nkeys;
  const ArEvictOps* evict;
  uint64_t ops, gets, sets, hits, misses;
  uint64_t evicted, expired;
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
};

/* dict helpers used by server */
ArEntry* ar_find_entry(ArCore* core, const char* key, size_t klen,
                       size_t* bucket_out);
int ar_entry_set(ArCore* core, const char* key, size_t klen, const char* val,
                 size_t vlen);
void ar_entry_free(ArCore* core, size_t bucket, ArEntry* e);
void ar_rehash_if_needed(ArCore* core);

#endif
