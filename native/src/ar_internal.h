#ifndef AURA_REDIS_AR_INTERNAL_H
#define AURA_REDIS_AR_INTERNAL_H

#include "ar_core.h"

#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

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
  int authenticated; /* P0.4: 1 if AUTH ok or no requirepass */
  uint64_t last_active_ms; /* P1.12: idle timeout clock */
  int is_replica; /* P2.14: master→replica feed connection */
  int is_master_link; /* P2.14: replica's outbound link to master */
  /* P2.15 TLS */
  void* ssl; /* SSL* when AURA_REDIS_HAS_TLS */
  int is_tls;
  int ssl_hs_done;
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
  int tcp_port; /* bound port for INFO */
  char bind_addr[64]; /* e.g. 127.0.0.1 or 0.0.0.0 */
  int protected_mode; /* P0.4: Redis-ish; default 1 */
  char* requirepass; /* P0.4: NULL/empty = no AUTH required */
  int maxclients; /* P1.12: soft cap ≤ AR_MAX_CONN; default 128 */
  int timeout_sec; /* P1.12: idle client timeout seconds; 0=off */
  int tcp_backlog; /* P1.12: listen backlog; default 512 */
  /* P1.10 — command latency / slowlog */
  int slowlog_slower_than_us; /* threshold µs; default 10000 */
  uint64_t slowlog_count; /* commands slower than threshold */
  uint64_t cmd_lt_1ms, cmd_lt_10ms, cmd_lt_100ms, cmd_ge_100ms;
  uint64_t cmd_latency_sum_us; /* for avg */
  uint64_t cmd_latency_samples;
  ArConn conns[AR_MAX_CONN];
  int nconns;
  int quit;
  int shutting_down; /* P0.5: stop accept; drain then exit */
  void* evict_plugin; /* dlopen handle; NULL if built-in */
  uint64_t plugin_reloads; /* successful ar_core_load_evict_plugin */

  /* M12 — per-prefix policy hints (RESP POLICY → INFO policy_hints) */
  char policy_pfx[4][16];
  char policy_prof[4][32];
  int policy_n;

  /* P2.13 — aura-rdb snapshot */
  char rdb_dir[256];
  char rdb_filename[128];
  uint64_t rdb_last_save_time; /* unix seconds; 0 = never */
  int rdb_bgsave_pid;          /* >0 while BGSAVE child runs */
  int rdb_loading;
  int rdb_last_bgsave_ok; /* 1 ok / 0 fail */

  /* P2.14 — best-effort single-replica async replication */
  int repl_readonly; /* 1 if this node is a replica */
  char master_host[64];
  int master_port;
  int repl_applying; /* 1 while applying master stream (no re-entry) */

  /* P2.15 — optional native TLS (second listen fd) */
  int tls_listen_fd;
  int tls_port;
  char tls_cert_file[512];
  char tls_key_file[512];
  char tls_ca_file[512];
  void* ssl_ctx; /* SSL_CTX* when built with OpenSSL */
};

/* P2.13 RDB (implemented in ar_rdb.c) */
int ar_rdb_build_path(ArCore* core, char* out, size_t outsz);
int ar_rdb_save(ArCore* core);
int ar_rdb_bgsave(ArCore* core); /* 1 started/ok, 0 fail, -1 already in progress */
int ar_rdb_load(ArCore* core);   /* 1 ok (incl missing file), 0 corrupt/error */
void ar_rdb_poll_bgsave(ArCore* core);
void ar_rdb_wait_bgsave(ArCore* core);
int ar_core_set_rdb_dir(ArCore* core, const char* dir);
const char* ar_core_rdb_dir(ArCore* core);
int ar_core_set_rdb_filename(ArCore* core, const char* name);
const char* ar_core_rdb_filename(ArCore* core);

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


/* P2.15 TLS helpers (ar_tls.c; soft stubs without OpenSSL) */
int ar_tls_available(void);
void ar_tls_free_ctx(ArCore* core);
int ar_tls_setup_ctx(ArCore* core);
int ar_tls_accept_setup(ArCore* core, ArConn* c);
void ar_tls_conn_free(ArConn* c);
int ar_tls_handshake(ArCore* core, ArConn* c); /* 1 done, 0 want-io, -1 fail */
ssize_t ar_tls_read(ArConn* c, void* buf, size_t n, int* want_write);
ssize_t ar_tls_write(ArConn* c, const void* buf, size_t n, int* want_read);

#endif
