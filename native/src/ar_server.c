#include "ar_internal.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <unistd.h>
#include <time.h>
#include <signal.h>

#define AR_MAX_ARGV 64
#define AR_MAX_EVENTS 64
/* epoll data.ptr sentinels for listen fds (client conns are real ptrs) */
#define AR_EPOLL_LISTEN_PLAIN ((void*)0)
#define AR_EPOLL_LISTEN_TLS ((void*)1)

void ar_net_shutdown(ArCore* core);

static int flush_writes(ArCore* core, ArConn* c);
static void conn_close(ArCore* core, ArConn* c);

static int set_nonblock(int fd) {
  int fl = fcntl(fd, F_GETFL, 0);
  if (fl < 0)
    return -1;
  return fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}


static void multi_clear(ArConn* c) {
  if (!c)
    return;
  for (int i = 0; i < c->multi_n; ++i) {
    for (int a = 0; a < c->multi_q[i].argc; ++a) {
      free(c->multi_q[i].args[a]);
      c->multi_q[i].args[a] = NULL;
    }
    c->multi_q[i].argc = 0;
  }
  c->multi_n = 0;
  c->in_multi = 0;
}


static void pubsub_clear(ArConn* c) {
  if (!c)
    return;
  for (int i = 0; i < c->nsubs; ++i) {
    free(c->sub_channels[i]);
    c->sub_channels[i] = NULL;
    c->sub_clens[i] = 0;
  }
  c->nsubs = 0;
  c->pubsub_mode = 0;
}

static void conn_reset(ArConn* c) {
  multi_clear(c);
  pubsub_clear(c);
  c->fd = -1;
  c->in_use = 0;
  c->rlen = 0;
  c->wlen = 0;
  c->woff = 0;
  c->should_close = 0;
  c->want_write = 0;
  c->authenticated = 0;
  c->is_replica = 0;
  c->is_master_link = 0;
  c->ssl = NULL;
  c->is_tls = 0;
  c->ssl_hs_done = 0;
}

static ArConn* conn_alloc(ArCore* core, int fd) {
  for (int i = 0; i < AR_MAX_CONN; ++i) {
    ArConn* c = &core->conns[i];
    if (!c->in_use) {
      if (!c->rbuf) {
        c->rcap = AR_RBUF_INIT;
        c->rbuf = (char*)malloc(c->rcap);
      }
      if (!c->wbuf) {
        c->wcap = AR_WBUF_INIT;
        c->wbuf = (char*)malloc(c->wcap);
      }
      if (!c->rbuf || !c->wbuf)
        return NULL;
      conn_reset(c);
      c->fd = fd;
      c->in_use = 1;
      c->last_active_ms = ar_now_ms();
      /* No password configured → treat as authenticated. */
      if (!core->requirepass || !core->requirepass[0])
        c->authenticated = 1;
      if (i + 1 > core->nconns)
        core->nconns = i + 1;
      return c;
    }
  }
  return NULL;
}

static void conn_close(ArCore* core, ArConn* c) {
  if (!c->in_use)
    return;
  if (c->ssl)
    ar_tls_conn_free(c);
  if (c->fd >= 0) {
    epoll_ctl(core->epfd, EPOLL_CTL_DEL, c->fd, NULL);
    close(c->fd);
  }
  conn_reset(c);
}

static int wbuf_reserve(ArConn* c, size_t need) {
  if (c->wlen + need <= c->wcap)
    return 0;
  size_t ncap = c->wcap;
  while (c->wlen + need > ncap)
    ncap *= 2;
  char* p = (char*)realloc(c->wbuf, ncap);
  if (!p)
    return -1;
  c->wbuf = p;
  c->wcap = ncap;
  return 0;
}

static int wbuf_append(ArConn* c, const char* data, size_t n) {
  if (wbuf_reserve(c, n) < 0)
    return -1;
  memcpy(c->wbuf + c->wlen, data, n);
  c->wlen += n;
  return 0;
}


static int reply_err(ArConn* c, const char* s) {
  size_t n = strlen(s);
  if (wbuf_reserve(c, n + 3) < 0)
    return -1;
  c->wbuf[c->wlen++] = '-';
  memcpy(c->wbuf + c->wlen, s, n);
  c->wlen += n;
  c->wbuf[c->wlen++] = '\r';
  c->wbuf[c->wlen++] = '\n';
  return 0;
}

static int reply_int(ArConn* c, int64_t v) {
  char tmp[48];
  int n = snprintf(tmp, sizeof(tmp), ":%lld\r\n", (long long)v);
  return wbuf_append(c, tmp, (size_t)n);
}

static int reply_null_bulk(ArConn* c) {
  return wbuf_append(c, "$-1\r\n", 5);
}

static int reply_bulk(ArConn* c, const char* data, size_t n) {
  char hdr[32];
  int hn = snprintf(hdr, sizeof(hdr), "$%zu\r\n", n);
  if (wbuf_reserve(c, (size_t)hn + n + 2) < 0)
    return -1;
  memcpy(c->wbuf + c->wlen, hdr, (size_t)hn);
  c->wlen += (size_t)hn;
  if (n)
    memcpy(c->wbuf + c->wlen, data, n);
  c->wlen += n;
  c->wbuf[c->wlen++] = '\r';
  c->wbuf[c->wlen++] = '\n';
  return 0;
}

static int reply_ok(ArConn* c) { return wbuf_append(c, "+OK\r\n", 5); }
static int reply_pong(ArConn* c) { return wbuf_append(c, "+PONG\r\n", 7); }


static int parse_double(const char* p, size_t n, double* out) {
  if (!p || n == 0 || n >= 64)
    return 0;
  char buf[64];
  memcpy(buf, p, n);
  buf[n] = '\0';
  if (n == 4 && (memcmp(buf, "-inf", 4) == 0 || memcmp(buf, "-Inf", 4) == 0 ||
                 memcmp(buf, "-INF", 4) == 0)) {
    *out = -1.0 / 0.0;
    return 1;
  }
  if ((n == 3 && (memcmp(buf, "inf", 3) == 0 || memcmp(buf, "Inf", 3) == 0 ||
                  memcmp(buf, "INF", 3) == 0)) ||
      (n == 4 && (memcmp(buf, "+inf", 4) == 0 || memcmp(buf, "+Inf", 4) == 0 ||
                  memcmp(buf, "+INF", 4) == 0))) {
    *out = 1.0 / 0.0;
    return 1;
  }
  char* end = NULL;
  *out = strtod(buf, &end);
  return end && end != buf && *end == '\0';
}

static int cmd_eq(const char* a, size_t alen, const char* lit) {
  size_t n = strlen(lit);
  if (alen != n)
    return 0;
  for (size_t i = 0; i < n; ++i) {
    char ca = a[i];
    char cb = lit[i];
    if (ca >= 'A' && ca <= 'Z')
      ca = (char)(ca - 'A' + 'a');
    if (cb >= 'A' && cb <= 'Z')
      cb = (char)(cb - 'A' + 'a');
    if (ca != cb)
      return 0;
  }
  return 1;
}

typedef struct {
  const char* p;
  size_t len;
} Arg;

/* --- P2.14 replication (best-effort string KV) --- */
static int resp_append_bulk(ArConn* dst, const char* p, size_t n) {
  char hdr[48];
  int hn = snprintf(hdr, sizeof(hdr), "$%zu\r\n", n);
  if (hn < 0 || wbuf_append(dst, hdr, (size_t)hn) < 0)
    return -1;
  if (n && wbuf_append(dst, p, n) < 0)
    return -1;
  return wbuf_append(dst, "\r\n", 2);
}

static int resp_append_array_hdr(ArConn* dst, int argc) {
  char hdr[32];
  int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", argc);
  if (hn < 0)
    return -1;
  return wbuf_append(dst, hdr, (size_t)hn);
}

static void repl_arm_flush(ArCore* core, ArConn* r) {
  if (!r || !r->in_use)
    return;
  if (flush_writes(core, r) < 0)
    conn_close(core, r);
}

static void repl_propagate(ArCore* core, Arg* argv, int argc) {
  if (!core || !argv || argc < 1)
    return;
  if (core->repl_readonly || core->repl_applying)
    return;
  for (int i = 0; i < AR_MAX_CONN; ++i) {
    ArConn* r = &core->conns[i];
    if (!r->in_use || !r->is_replica)
      continue;
    if (resp_append_array_hdr(r, argc) < 0) {
      conn_close(core, r);
      continue;
    }
    int bad = 0;
    for (int a = 0; a < argc; ++a) {
      if (resp_append_bulk(r, argv[a].p, argv[a].len) < 0) {
        bad = 1;
        break;
      }
    }
    if (bad)
      conn_close(core, r);
    else
      repl_arm_flush(core, r);
  }
}

static int repl_fullsync(ArCore* core, ArConn* c) {
  if (!core || !c)
    return -1;
  uint64_t now = ar_now_ms();
  for (int tier = 0; tier < 2; ++tier) {
    ArEntry** table = (tier == 0) ? core->buckets : core->cold_buckets;
    size_t nb = (tier == 0) ? core->nbuckets : core->cold_nbuckets;
    if (!table)
      continue;
    for (size_t i = 0; i < nb; ++i) {
      for (ArEntry* e = table[i]; e; e = e->next) {
        if (e->type != AR_TYPE_STRING)
          continue;
        if (e->expire_at && now >= e->expire_at)
          continue;
        int64_t ttl_sec = -1;
        if (e->expire_at) {
          ttl_sec = (int64_t)((e->expire_at - now + 999) / 1000);
          if (ttl_sec <= 0)
            continue;
        }
        if (ttl_sec > 0) {
          char ttlbuf[32];
          int tn = snprintf(ttlbuf, sizeof(ttlbuf), "%lld", (long long)ttl_sec);
          if (resp_append_array_hdr(c, 5) < 0 || resp_append_bulk(c, "SET", 3) < 0 ||
              resp_append_bulk(c, e->key, e->klen) < 0 ||
              resp_append_bulk(c, e->val, e->vlen) < 0 ||
              resp_append_bulk(c, "EX", 2) < 0 ||
              resp_append_bulk(c, ttlbuf, (size_t)tn) < 0)
            return -1;
        } else {
          if (resp_append_array_hdr(c, 3) < 0 || resp_append_bulk(c, "SET", 3) < 0 ||
              resp_append_bulk(c, e->key, e->klen) < 0 ||
              resp_append_bulk(c, e->val, e->vlen) < 0)
            return -1;
        }
      }
    }
  }
  return 0;
}

static void repl_close_master_links(ArCore* core) {
  for (int i = 0; i < AR_MAX_CONN; ++i) {
    ArConn* c = &core->conns[i];
    if (c->in_use && c->is_master_link)
      conn_close(core, c);
  }
}

static int repl_connect_master(ArCore* core, const char* host, int port) {
  if (!core || !host || port <= 0 || port > 65535)
    return 0;
  if (core->epfd < 0)
    return 0;
  repl_close_master_links(core);
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    return 0;
  struct timeval tv = {.tv_sec = 2, .tv_usec = 0};
  setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons((uint16_t)port);
  if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
    struct hostent* he = gethostbyname(host);
    if (!he || !he->h_addr_list || !he->h_addr_list[0]) {
      close(fd);
      return 0;
    }
    memcpy(&addr.sin_addr, he->h_addr_list[0], (size_t)he->h_length);
  }
  if (connect(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
    close(fd);
    return 0;
  }
  set_nonblock(fd);
  int one = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  ArConn* c = conn_alloc(core, fd);
  if (!c) {
    close(fd);
    return 0;
  }
  c->is_master_link = 1;
  c->authenticated = 1;
  struct epoll_event ev;
  ev.events = EPOLLIN | EPOLLET;
  ev.data.ptr = c;
  if (epoll_ctl(core->epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {
    conn_close(core, c);
    return 0;
  }
  if (resp_append_array_hdr(c, 1) < 0 || resp_append_bulk(c, "SYNC", 4) < 0) {
    conn_close(core, c);
    return 0;
  }
  if (flush_writes(core, c) < 0) {
    conn_close(core, c);
    return 0;
  }
  snprintf(core->master_host, sizeof(core->master_host), "%s", host);
  core->master_port = port;
  core->repl_readonly = 1;
  fprintf(stderr, "ar_repl: replicaof %s:%d (master link up)\n", host, port);
  fflush(stderr);
  return 1;
}

static int cmd_is_write(const char* cmd, size_t clen) {
  return cmd_eq(cmd, clen, "set") || cmd_eq(cmd, clen, "del") ||
         cmd_eq(cmd, clen, "expire") || cmd_eq(cmd, clen, "flushdb") ||
         cmd_eq(cmd, clen, "mset") || cmd_eq(cmd, clen, "incr") ||
         cmd_eq(cmd, clen, "decr") || cmd_eq(cmd, clen, "pin") ||
         cmd_eq(cmd, clen, "unpin") || cmd_eq(cmd, clen, "policy") ||
         cmd_eq(cmd, clen, "evict") || cmd_eq(cmd, clen, "layout") ||
         cmd_eq(cmd, clen, "plugin") || cmd_eq(cmd, clen, "save") ||
         cmd_eq(cmd, clen, "bgsave") || cmd_eq(cmd, clen, "hset") ||
         cmd_eq(cmd, clen, "hdel") || cmd_eq(cmd, clen, "hincrby") ||
         cmd_eq(cmd, clen, "lpush") || cmd_eq(cmd, clen, "rpush") ||
         cmd_eq(cmd, clen, "lpop") || cmd_eq(cmd, clen, "rpop") ||
         cmd_eq(cmd, clen, "zadd") || cmd_eq(cmd, clen, "zrem") ||
         cmd_eq(cmd, clen, "publish");
}



/* Parse one RESP value starting at *pos; for command arrays of bulk strings.
 * Returns bytes consumed (>0), 0 if incomplete, -1 on protocol error. */
static int parse_bulk(const char* buf, size_t len, size_t pos, Arg* out,
                      size_t* next) {
  if (pos >= len || buf[pos] != '$')
    return -1;
  size_t i = pos + 1;
  int neg = 0;
  if (i < len && buf[i] == '-') {
    neg = 1;
    i++;
  }
  long n = 0;
  int digits = 0;
  while (i < len && buf[i] >= '0' && buf[i] <= '9') {
    /* Cap digit run + magnitude: rbuf hard-limit is 16MiB; reject protocol bombs. */
    if (digits > 9 || n > (16L * 1024 * 1024))
      return -1;
    n = n * 10 + (buf[i] - '0');
    i++;
    digits++;
  }
  if (!digits) {
    if (i >= len)
      return 0; /* incomplete: "$" only */
    return -1; /* non-digit in length */
  }
  if (i >= len)
    return 0;
  if (buf[i] != '\r')
    return -1;
  if (i + 1 >= len)
    return 0;
  if (buf[i + 1] != '\n')
    return -1;
  i += 2;
  if (neg) {
    /* Null bulk ($-1) is valid RESP but not a command argv element. */
    out->p = NULL;
    out->len = (size_t)-1;
    *next = i;
    return (int)(i - pos);
  }
  if (n > 16L * 1024 * 1024)
    return -1;
  if (i + (size_t)n + 2 > len)
    return 0;
  out->p = buf + i;
  out->len = (size_t)n;
  if (buf[i + (size_t)n] != '\r' || buf[i + (size_t)n + 1] != '\n')
    return -1;
  *next = i + (size_t)n + 2;
  return (int)(*next - pos);
}

/* Try parse one command array; fills argv[0..argc). Returns consumed bytes,
 * 0 incomplete, -1 error. */
static int parse_command(const char* buf, size_t len, size_t pos, Arg* argv,
                         int* argc_out) {
  if (pos >= len)
    return 0;
  if (buf[pos] != '*') {
    /* inline: rare; reject for now */
    return -1;
  }
  size_t i = pos + 1;
  long n = 0;
  int digits = 0;
  while (i < len && buf[i] >= '0' && buf[i] <= '9') {
    n = n * 10 + (buf[i] - '0');
    i++;
    digits++;
  }
  if (!digits) {
    if (i >= len)
      return 0;
    return -1;
  }
  if (i >= len)
    return 0;
  if (buf[i] != '\r')
    return -1;
  if (i + 1 >= len)
    return 0;
  if (buf[i + 1] != '\n')
    return -1;
  i += 2;
  if (n < 0 || n > AR_MAX_ARGV)
    return -1;
  int argc = (int)n;
  for (int a = 0; a < argc; ++a) {
    size_t next = 0;
    int r = parse_bulk(buf, len, i, &argv[a], &next);
    if (r == 0)
      return 0;
    if (r < 0)
      return -1;
    /* Command arrays must be bulk strings, not null bulks ($-1). */
    if (argv[a].p == NULL)
      return -1;
    i = next;
  }
  *argc_out = argc;
  return (int)(i - pos);
}

/* Pointer-stable get without malloc: returns entry val pointer.
 * Promotes cold→hot under hot_cold layout. */
/* Returns: 1 hit, 0 miss, -1 WRONGTYPE */
static int get_ptr(ArCore* core, const char* key, size_t klen,
                   const char** val, size_t* vlen) {
  core->ops++;
  core->gets++;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e) {
    core->misses++;
    /* A10: sample miss under live champ */
    if (core->shadow_sample_pct > 0 &&
        (rand() % 100) < core->shadow_sample_pct) {
      core->shadow_samples++;
      core->shadow_misses++;
    }
    *val = NULL;
    *vlen = 0;
    return 0;
  }
  if (e->type != AR_TYPE_STRING) {
    *val = NULL;
    *vlen = 0;
    return -1;
  }
  core->hits++;
  if (core->shadow_sample_pct > 0 &&
      (rand() % 100) < core->shadow_sample_pct) {
    core->shadow_samples++;
    core->shadow_hits++;
  }
  ar_touch_get(core, e, b, tier);
  if (core->evict && core->evict->on_get)
    core->evict->on_get(core, e);
  *val = e->val;
  *vlen = e->vlen;
  return 1;
}

static int pass_eq(const char* a, size_t alen, const char* pass) {
  if (!pass)
    return 0;
  size_t n = strlen(pass);
  if (alen != n)
    return 0;
  return memcmp(a, pass, n) == 0;
}

static int cmd_allowed_unauth(const char* cmd, size_t clen) {
  return cmd_eq(cmd, clen, "auth") || cmd_eq(cmd, clen, "ping") ||
         cmd_eq(cmd, clen, "quit") || cmd_eq(cmd, clen, "hello");
}


static uint64_t ar_now_us(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
    return 0;
  return (uint64_t)ts.tv_sec * 1000000ull + (uint64_t)(ts.tv_nsec / 1000ull);
}

static void note_cmd_latency(ArCore* core, uint64_t us) {
  if (!core)
    return;
  core->cmd_latency_sum_us += us;
  core->cmd_latency_samples++;
  if (us < 1000)
    core->cmd_lt_1ms++;
  else if (us < 10000)
    core->cmd_lt_10ms++;
  else if (us < 100000)
    core->cmd_lt_100ms++;
  else
    core->cmd_ge_100ms++;
  /* 0 = log all (Redis-ish); negative disabled via CONFIG guard */
  if (core->slowlog_slower_than_us >= 0 &&
      us >= (uint64_t)core->slowlog_slower_than_us)
    core->slowlog_count++;
}

static int connected_clients(ArCore* core) {
  int n = 0;
  for (int i = 0; i < AR_MAX_CONN; ++i)
    if (core->conns[i].in_use)
      n++;
  return n;
}

static int pubsub_is_allowed(const char* cmd, size_t clen) {
  return cmd_eq(cmd, clen, "subscribe") || cmd_eq(cmd, clen, "unsubscribe") ||
         cmd_eq(cmd, clen, "psubscribe") || cmd_eq(cmd, clen, "punsubscribe") ||
         cmd_eq(cmd, clen, "ping") || cmd_eq(cmd, clen, "quit") ||
         cmd_eq(cmd, clen, "reset");
}

static int reply_pubsub_msg(ArConn* c, const char* kind, size_t klen,
                            const char* chan, size_t clen, int64_t nsubs) {
  /* *3\r\n $kind $chan :nsubs  OR for message *3 kind chan payload */
  if (resp_append_array_hdr(c, 3) < 0)
    return -1;
  if (resp_append_bulk(c, kind, klen) < 0)
    return -1;
  if (resp_append_bulk(c, chan, clen) < 0)
    return -1;
  return reply_int(c, nsubs);
}

static int conn_subscribed(ArConn* c, const char* chan, size_t clen) {
  for (int i = 0; i < c->nsubs; ++i) {
    if (c->sub_clens[i] == clen &&
        memcmp(c->sub_channels[i], chan, clen) == 0)
      return 1;
  }
  return 0;
}

static int conn_add_sub(ArConn* c, const char* chan, size_t clen) {
  if (conn_subscribed(c, chan, clen))
    return c->nsubs;
  if (c->nsubs >= AR_PUBSUB_MAX)
    return -1;
  char* dup = ar_xmemdup(chan, clen);
  if (!dup)
    return -1;
  c->sub_channels[c->nsubs] = dup;
  c->sub_clens[c->nsubs] = clen;
  c->nsubs++;
  c->pubsub_mode = 1;
  return c->nsubs;
}

static int conn_del_sub(ArConn* c, const char* chan, size_t clen) {
  for (int i = 0; i < c->nsubs; ++i) {
    if (c->sub_clens[i] == clen &&
        memcmp(c->sub_channels[i], chan, clen) == 0) {
      free(c->sub_channels[i]);
      for (int j = i; j + 1 < c->nsubs; ++j) {
        c->sub_channels[j] = c->sub_channels[j + 1];
        c->sub_clens[j] = c->sub_clens[j + 1];
      }
      c->nsubs--;
      if (c->nsubs == 0)
        c->pubsub_mode = 0;
      return c->nsubs;
    }
  }
  return c->nsubs;
}

static int publish_local(ArCore* core, const char* chan, size_t clen,
                         const char* msg, size_t mlen) {
  int receivers = 0;
  for (int i = 0; i < AR_MAX_CONN; ++i) {
    ArConn* sub = &core->conns[i];
    if (!sub->in_use || !sub->pubsub_mode)
      continue;
    if (!conn_subscribed(sub, chan, clen))
      continue;
    if (resp_append_array_hdr(sub, 3) < 0)
      continue;
    if (resp_append_bulk(sub, "message", 7) < 0)
      continue;
    if (resp_append_bulk(sub, chan, clen) < 0)
      continue;
    if (resp_append_bulk(sub, msg, mlen) < 0)
      continue;
    if (flush_writes(core, sub) < 0) {
      /* keep message buffered; arm EPOLLOUT */
      struct epoll_event ev;
      ev.events = EPOLLIN | EPOLLOUT | EPOLLET;
      ev.data.ptr = sub;
      epoll_ctl(core->epfd, EPOLL_CTL_MOD, sub->fd, &ev);
      sub->want_write = 1;
    } else if (sub->wlen > sub->woff) {
      struct epoll_event ev;
      ev.events = EPOLLIN | EPOLLOUT | EPOLLET;
      ev.data.ptr = sub;
      epoll_ctl(core->epfd, EPOLL_CTL_MOD, sub->fd, &ev);
      sub->want_write = 1;
    }
    receivers++;
  }
  return receivers;
}


static int dispatch(ArCore* core, ArConn* c, Arg* argv, int argc) {
  if (argc < 1)
    return reply_err(c, "ERR empty command");
  const char* cmd = argv[0].p;
  size_t clen = argv[0].len;

  /* P0.4: requirepass — unauthenticated clients limited to AUTH/PING/QUIT/HELLO */
  if (core->requirepass && core->requirepass[0] && !c->authenticated &&
      !cmd_allowed_unauth(cmd, clen))
    return reply_err(c, "NOAUTH Authentication required.");

  /* P2.14: replica is read-only for client writes (master link may apply). */
  if (core->repl_readonly && cmd_is_write(cmd, clen) && !c->is_master_link)
    return reply_err(c, "READONLY You can't write against a read only replica.");

  /* P2.14: CONFIG SET denied on replica (CONFIG GET ok). */
  if (core->repl_readonly && !c->is_master_link && cmd_eq(cmd, clen, "config") &&
      argc >= 2 && cmd_eq(argv[1].p, argv[1].len, "set"))
    return reply_err(c, "READONLY You can't write against a read only replica.");

  /* P3.17b — pubsub mode restricts commands */
  if (c->pubsub_mode && !pubsub_is_allowed(cmd, clen))
    return reply_err(c, "ERR only (P)SUBSCRIBE / (P)UNSUBSCRIBE / PING / QUIT allowed in this context");

  /* P3.17a — MULTI/EXEC/DISCARD */
  if (cmd_eq(cmd, clen, "multi")) {
    if (argc != 1)
      return reply_err(c, "ERR wrong number of arguments for 'multi'");
    if (c->in_multi)
      return reply_err(c, "ERR MULTI calls can not be nested");
    c->in_multi = 1;
    c->multi_n = 0;
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "discard")) {
    if (argc != 1)
      return reply_err(c, "ERR wrong number of arguments for 'discard'");
    if (!c->in_multi)
      return reply_err(c, "ERR DISCARD without MULTI");
    multi_clear(c);
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "exec")) {
    if (argc != 1)
      return reply_err(c, "ERR wrong number of arguments for 'exec'");
    if (!c->in_multi)
      return reply_err(c, "ERR EXEC without MULTI");
    int nq = c->multi_n;
    c->in_multi = 0; /* run without re-queuing */
    char hdr[32];
    int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", nq);
    if (wbuf_append(c, hdr, (size_t)hn) < 0) {
      multi_clear(c);
      return -1;
    }
    for (int qi = 0; qi < nq; ++qi) {
      ArQueuedCmd* q = &c->multi_q[qi];
      Arg qargv[AR_MULTI_ARGV];
      for (int a = 0; a < q->argc; ++a) {
        qargv[a].p = q->args[a];
        qargv[a].len = q->alens[a];
      }
      if (dispatch(core, c, qargv, q->argc) < 0) {
        multi_clear(c);
        return -1;
      }
    }
    multi_clear(c);
    return 0;
  }
  /* Inside MULTI: queue (except handled above). */
  if (c->in_multi) {
    if (c->multi_n >= AR_MULTI_MAX)
      return reply_err(c, "ERR MULTI queue is full");
    if (argc > AR_MULTI_ARGV)
      return reply_err(c, "ERR too many arguments to queue");
    ArQueuedCmd* q = &c->multi_q[c->multi_n];
    q->argc = argc;
    for (int a = 0; a < argc; ++a) {
      q->args[a] = ar_xmemdup(argv[a].p, argv[a].len);
      q->alens[a] = argv[a].len;
      if (!q->args[a]) {
        for (int j = 0; j < a; ++j)
          free(q->args[j]);
        q->argc = 0;
        return reply_err(c, "ERR OOM");
      }
    }
    c->multi_n++;
    return wbuf_append(c, "+QUEUED\r\n", 9);
  }

  /* P2.14 — SYNC: mark feed + full sync (no +OK; stream SET commands). */
  if (cmd_eq(cmd, clen, "sync")) {
    if (argc != 1)
      return reply_err(c, "ERR wrong number of arguments for 'sync'");
    if (core->repl_readonly)
      return reply_err(c, "ERR SYNC against a replica");
    c->is_replica = 1;
    if (repl_fullsync(core, c) < 0)
      return reply_err(c, "ERR sync failed");
    return 0;
  }

  /* P2.14 — REPLICAOF host port | REPLICAOF NO ONE */
  if (cmd_eq(cmd, clen, "replicaof") || cmd_eq(cmd, clen, "slaveof")) {
    if (argc == 3 && cmd_eq(argv[1].p, argv[1].len, "no") &&
        cmd_eq(argv[2].p, argv[2].len, "one")) {
      repl_close_master_links(core);
      core->repl_readonly = 0;
      core->master_host[0] = '\0';
      core->master_port = 0;
      return reply_ok(c);
    }
    if (argc != 3)
      return reply_err(c, "ERR wrong number of arguments for 'replicaof'");
    char host[64];
    char portbuf[16];
    size_t hl = argv[1].len < sizeof(host) - 1 ? argv[1].len : sizeof(host) - 1;
    size_t pl = argv[2].len < sizeof(portbuf) - 1 ? argv[2].len : sizeof(portbuf) - 1;
    memcpy(host, argv[1].p, hl);
    host[hl] = '\0';
    memcpy(portbuf, argv[2].p, pl);
    portbuf[pl] = '\0';
    int port = atoi(portbuf);
    if (port <= 0)
      return reply_err(c, "ERR invalid port");
    if (!repl_connect_master(core, host, port))
      return reply_err(c, "ERR replicaof connect failed");
    return reply_ok(c);
  }

  if (cmd_eq(cmd, clen, "auth")) {
    if (!core->requirepass || !core->requirepass[0])
      return reply_err(c, "ERR AUTH called without any password configured for "
                          "the default user. Are you sure your configuration "
                          "is correct?");
    const char* pw;
    size_t pwlen;
    if (argc == 2) {
      pw = argv[1].p;
      pwlen = argv[1].len;
    } else if (argc == 3) {
      /* Redis 6 ACL form: AUTH username password — username ignored */
      pw = argv[2].p;
      pwlen = argv[2].len;
    } else {
      return reply_err(c, "ERR wrong number of arguments for 'auth'");
    }
    if (pass_eq(pw, pwlen, core->requirepass)) {
      c->authenticated = 1;
      return reply_ok(c);
    }
    return reply_err(c, "WRONGPASS invalid username-password pair");
  }
  if (cmd_eq(cmd, clen, "hello")) {
    /* Minimal HELLO: optional AUTH inline; reply simple map-ish array */
    for (int i = 1; i + 1 < argc; ++i) {
      if (cmd_eq(argv[i].p, argv[i].len, "auth")) {
        if (i + 2 < argc) {
          /* AUTH user pass */
          if (core->requirepass && core->requirepass[0] &&
              pass_eq(argv[i + 2].p, argv[i + 2].len, core->requirepass))
            c->authenticated = 1;
          else if (core->requirepass && core->requirepass[0])
            return reply_err(c, "WRONGPASS invalid username-password pair");
        } else if (i + 1 < argc) {
          if (core->requirepass && core->requirepass[0] &&
              pass_eq(argv[i + 1].p, argv[i + 1].len, core->requirepass))
            c->authenticated = 1;
          else if (core->requirepass && core->requirepass[0])
            return reply_err(c, "WRONGPASS invalid username-password pair");
        }
      }
    }
    char buf[128];
    int n = snprintf(buf, sizeof(buf),
                     "*4\r\n$6\r\nserver\r\n$10\r\naura-redis\r\n"
                     "$7\r\nversion\r\n$3\r\n0.1\r\n");
    if (n < 0)
      return reply_err(c, "ERR hello");
    return wbuf_append(c, buf, (size_t)n);
  }

  if (cmd_eq(cmd, clen, "ping")) {
    if (argc == 1)
      return reply_pong(c);
    if (argc == 2)
      return reply_bulk(c, argv[1].p, argv[1].len);
    return reply_err(c, "ERR wrong number of arguments for 'ping'");
  }
  if (cmd_eq(cmd, clen, "quit")) {
    reply_ok(c);
    c->should_close = 1;
    return 0;
  }
  /* P2.13 — SAVE / BGSAVE (aura-rdb snapshot) */
  if (cmd_eq(cmd, clen, "save")) {
    if (argc != 1)
      return reply_err(c, "ERR wrong number of arguments for 'save'");
    if (!ar_rdb_save(core))
      return reply_err(c, "ERR save failed");
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "bgsave")) {
    if (argc != 1)
      return reply_err(c, "ERR wrong number of arguments for 'bgsave'");
    int rc = ar_rdb_bgsave(core);
    if (rc < 0)
      return reply_err(c, "ERR Background save already in progress");
    if (rc == 0)
      return reply_err(c, "ERR bgsave failed");
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "get")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'get'");
    const char* v = NULL;
    size_t vl = 0;
    int gr = get_ptr(core, argv[1].p, argv[1].len, &v, &vl);
    if (gr < 0)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!gr)
      return reply_null_bulk(c);
    return reply_bulk(c, v, vl);
  }
  if (cmd_eq(cmd, clen, "set")) {
    /* SET key value [EX seconds] — minimal TTL path (M9) */
    if (argc < 3)
      return reply_err(c, "ERR wrong number of arguments for 'set'");
    int64_t ex_sec = -1; /* -1 = plain SET (clear TTL) */
    for (int oi = 3; oi + 1 < argc; oi += 2) {
      if (cmd_eq(argv[oi].p, argv[oi].len, "ex")) {
        char nbuf[32];
        if (argv[oi + 1].len == 0 || argv[oi + 1].len >= sizeof(nbuf))
          return reply_err(c, "ERR invalid expire time in 'set'");
        memcpy(nbuf, argv[oi + 1].p, argv[oi + 1].len);
        nbuf[argv[oi + 1].len] = '\0';
        ex_sec = (int64_t)atoll(nbuf);
        if (ex_sec <= 0)
          return reply_err(c, "ERR invalid expire time in 'set'");
      } else {
        return reply_err(c, "ERR syntax error");
      }
    }
    if (ex_sec > 0) {
      if (!ar_set_bin_ex(core, argv[1].p, argv[1].len, argv[2].p, argv[2].len,
                         ex_sec))
        return reply_err(c, "ERR OOM");
    } else {
      core->ops++;
      core->sets++;
      if (!ar_entry_set(core, argv[1].p, argv[1].len, argv[2].p, argv[2].len))
        return reply_err(c, "ERR OOM");
    }
    repl_propagate(core, argv, argc);
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "expire")) {
    if (argc != 3)
      return reply_err(c, "ERR wrong number of arguments for 'expire'");
    char nbuf[32];
    if (argv[2].len == 0 || argv[2].len >= sizeof(nbuf))
      return reply_err(c, "ERR value is not an integer or out of range");
    memcpy(nbuf, argv[2].p, argv[2].len);
    nbuf[argv[2].len] = '\0';
    int64_t sec = (int64_t)atoll(nbuf);
    int ok = ar_expire(core, argv[1].p, argv[1].len, sec);
    core->ops++;
    if (ok)
      repl_propagate(core, argv, argc);
    return reply_int(c, ok ? 1 : 0);
  }
  if (cmd_eq(cmd, clen, "ttl")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'ttl'");
    int64_t ttl = ar_ttl(core, argv[1].p, argv[1].len);
    core->ops++;
    return reply_int(c, ttl);
  }
  if (cmd_eq(cmd, clen, "del")) {
    if (argc < 2)
      return reply_err(c, "ERR wrong number of arguments for 'del'");
    int64_t n = 0;
    for (int i = 1; i < argc; ++i) {
      if (ar_del_bin(core, argv[i].p, argv[i].len))
        n++;
    }
    if (n > 0)
      repl_propagate(core, argv, argc);
    return reply_int(c, n);
  }
  if (cmd_eq(cmd, clen, "exists")) {
    if (argc < 2)
      return reply_err(c, "ERR wrong number of arguments for 'exists'");
    int64_t n = 0;
    for (int i = 1; i < argc; ++i) {
      if (ar_exists_bin(core, argv[i].p, argv[i].len))
        n++;
    }
    core->ops++;
    return reply_int(c, n);
  }
  if (cmd_eq(cmd, clen, "mget")) {
    if (argc < 2)
      return reply_err(c, "ERR wrong number of arguments for 'mget'");
    for (int i = 1; i < argc; ++i) {
      size_t b0 = 0;
      int tier0 = 0;
      ArEntry* e0 = ar_find_entry_ex(core, argv[i].p, argv[i].len, &b0, &tier0);
      if (e0 && e0->type != AR_TYPE_STRING)
        return reply_err(
            c,
            "WRONGTYPE Operation against a key holding the wrong kind of value");
    }
    char hdr[32];
    int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", argc - 1);
    if (wbuf_append(c, hdr, (size_t)hn) < 0)
      return -1;
    for (int i = 1; i < argc; ++i) {
      const char* v = NULL;
      size_t vl = 0;
      int gr = get_ptr(core, argv[i].p, argv[i].len, &v, &vl);
      if (gr <= 0) {
        if (reply_null_bulk(c) < 0)
          return -1;
      } else {
        if (reply_bulk(c, v, vl) < 0)
          return -1;
      }
    }
    return 0;
  }
  if (cmd_eq(cmd, clen, "mset")) {
    if (argc < 3 || ((argc - 1) % 2) != 0)
      return reply_err(c, "ERR wrong number of arguments for 'mset'");
    for (int i = 1; i + 1 < argc; i += 2) {
      core->ops++;
      core->sets++;
      if (!ar_entry_set(core, argv[i].p, argv[i].len, argv[i + 1].p,
                        argv[i + 1].len))
        return reply_err(c, "ERR OOM");
    }
    repl_propagate(core, argv, argc);
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "incr")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'incr'");
    int ok = 0;
    int64_t v = ar_incr(core, argv[1].p, argv[1].len, 1, &ok);
    if (!ok)
      return reply_err(c, "ERR value is not an integer or out of range");
    repl_propagate(core, argv, argc);
    return reply_int(c, v);
  }
  if (cmd_eq(cmd, clen, "decr")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'decr'");
    int ok = 0;
    int64_t v = ar_incr(core, argv[1].p, argv[1].len, -1, &ok);
    if (!ok)
      return reply_err(c, "ERR value is not an integer or out of range");
    repl_propagate(core, argv, argc);
    return reply_int(c, v);
  }
  if (cmd_eq(cmd, clen, "flushdb")) {
    ar_flushdb(core);
    repl_propagate(core, argv, argc);
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "command")) {
    /* redis-benchmark / some clients probe; reply empty array */
    return wbuf_append(c, "*0\r\n", 4);
  }
  /* Aura-native control plane: EVICT [name] | EVICT samples <n>
   * Policy agents apply swaps via RESP (see docs/aura-native-control.md). */
  if (cmd_eq(cmd, clen, "evict")) {
    if (argc == 1) {
      const char* name = ar_core_evict_name(core);
      return reply_bulk(c, name, strlen(name));
    }
    if (argc == 2) {
      char namebuf[64];
      if (argv[1].len == 0 || argv[1].len >= sizeof(namebuf))
        return reply_err(c, "ERR bad evict (want noop|lru|lfu|ttl_aware|slru|tinylfu)");
      memcpy(namebuf, argv[1].p, argv[1].len);
      namebuf[argv[1].len] = '\0';
      if (!ar_core_set_evict_by_name(core, namebuf))
        return reply_err(c, "ERR bad evict (want noop|lru|lfu|ttl_aware|slru|tinylfu)");
      return reply_ok(c);
    }
    if (argc == 3 && cmd_eq(argv[1].p, argv[1].len, "samples")) {
      char nbuf[32];
      if (argv[2].len == 0 || argv[2].len >= sizeof(nbuf))
        return reply_err(c, "ERR bad samples");
      memcpy(nbuf, argv[2].p, argv[2].len);
      nbuf[argv[2].len] = '\0';
      int n = atoi(nbuf);
      if (!ar_core_set_evict_samples(core, n))
        return reply_err(c, "ERR bad samples");
      return reply_ok(c);
    }
    return reply_err(c, "ERR wrong number of arguments for 'evict'");
  }
  /* P1.1 — CONFIG GET/SET (runtime knobs; not persisted). */
  if (cmd_eq(cmd, clen, "config")) {
    if (argc < 2)
      return reply_err(c, "ERR wrong number of arguments for 'config'");
    if (cmd_eq(argv[1].p, argv[1].len, "get")) {
      if (argc != 3)
        return reply_err(c, "ERR wrong number of arguments for 'config|get'");
      char pat[64];
      size_t pl = argv[2].len < sizeof(pat) - 1 ? argv[2].len : sizeof(pat) - 1;
      memcpy(pat, argv[2].p, pl);
      pat[pl] = '\0';
      /* Collect matching pairs into temp buffer as RESP array */
      char pairs[2048];
      int pn = 0;
      int nitems = 0;
      char tmp[256];
#define AR_CFG_ADD(name, valfmt, ...)                                          \
  do {                                                                         \
    int match = (strcmp(pat, "*") == 0) || (strcmp(pat, name) == 0);             \
    if (match) {                                                               \
      int kn = snprintf(tmp, sizeof(tmp), "$%zu\r\n%s\r\n", strlen(name), name); \
      if (kn > 0 && pn + kn < (int)sizeof(pairs)) {                            \
        memcpy(pairs + pn, tmp, (size_t)kn);                                   \
        pn += kn;                                                              \
      }                                                                        \
      int vn = snprintf(tmp, sizeof(tmp), valfmt, __VA_ARGS__);                \
      if (vn >= 0) {                                                            \
        char bulk[320];                                                        \
        int bn = snprintf(bulk, sizeof(bulk), "$%d\r\n%.*s\r\n", vn, vn, tmp);  \
        if (bn > 0 && pn + bn < (int)sizeof(pairs)) {                          \
          memcpy(pairs + pn, bulk, (size_t)bn);                                \
          pn += bn;                                                            \
          nitems += 2;                                                         \
        }                                                                      \
      }                                                                        \
    }                                                                          \
  } while (0)
      {
        char vbuf[64];
        snprintf(vbuf, sizeof(vbuf), "%llu",
                 (unsigned long long)ar_core_maxmemory(core));
        AR_CFG_ADD("maxmemory", "%s", vbuf);
      }
      AR_CFG_ADD("requirepass", "%s",
                 (core->requirepass && core->requirepass[0]) ? core->requirepass
                                                             : "");
      AR_CFG_ADD("protected-mode", "%s",
                 core->protected_mode ? "yes" : "no");
      {
        char vbuf[32];
        snprintf(vbuf, sizeof(vbuf), "%d", ar_core_evict_samples(core));
        AR_CFG_ADD("evict-samples", "%s", vbuf);
      }
      AR_CFG_ADD("bind", "%s",
                 core->bind_addr[0] ? core->bind_addr : "127.0.0.1");
      {
        char vbuf[32];
        snprintf(vbuf, sizeof(vbuf), "%d", core->maxclients);
        AR_CFG_ADD("maxclients", "%s", vbuf);
      }
      {
        char vbuf[32];
        snprintf(vbuf, sizeof(vbuf), "%d", core->timeout_sec);
        AR_CFG_ADD("timeout", "%s", vbuf);
      }
      {
        char vbuf[32];
        snprintf(vbuf, sizeof(vbuf), "%d", core->tcp_backlog);
        AR_CFG_ADD("tcp-backlog", "%s", vbuf);
      }
      {
        char vbuf[32];
        snprintf(vbuf, sizeof(vbuf), "%d", core->slowlog_slower_than_us);
        AR_CFG_ADD("slowlog-log-slower-than", "%s", vbuf);
      }
      AR_CFG_ADD("dir", "%s", ar_core_rdb_dir(core));
      AR_CFG_ADD("dbfilename", "%s", ar_core_rdb_filename(core));
      AR_CFG_ADD("shadow-policy", "%s", ar_core_shadow_policy(core));
      {
        char vbuf[32];
        snprintf(vbuf, sizeof(vbuf), "%d", ar_core_shadow_sample_pct(core));
        AR_CFG_ADD("shadow-sample-pct", "%s", vbuf);
      }
      {
        char vbuf[32];
        snprintf(vbuf, sizeof(vbuf), "%d", ar_core_hot_soft_cap_pct(core));
        AR_CFG_ADD("hot-soft-cap-pct", "%s", vbuf);
      }
      {
        char vbuf[32];
        snprintf(vbuf, sizeof(vbuf), "%d", ar_core_hot_soft_cap_min(core));
        AR_CFG_ADD("hot-soft-cap-min", "%s", vbuf);
      }
      AR_CFG_ADD("hot-promote-on-get", "%s",
                 ar_core_hot_promote_on_get(core) ? "yes" : "no");
#undef AR_CFG_ADD
      char hdr[32];
      int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", nitems);
      if (hn < 0 || wbuf_append(c, hdr, (size_t)hn) < 0)
        return -1;
      if (pn > 0 && wbuf_append(c, pairs, (size_t)pn) < 0)
        return -1;
      return 0;
    }
    if (cmd_eq(argv[1].p, argv[1].len, "set")) {
      if (argc != 4)
        return reply_err(c, "ERR wrong number of arguments for 'config|set'");
      char key[64], val[256];
      size_t kl = argv[2].len < sizeof(key) - 1 ? argv[2].len : sizeof(key) - 1;
      size_t vl = argv[3].len < sizeof(val) - 1 ? argv[3].len : sizeof(val) - 1;
      memcpy(key, argv[2].p, kl);
      key[kl] = '\0';
      memcpy(val, argv[3].p, vl);
      val[vl] = '\0';
      /* case-insensitive key match via cmd_eq */
      if (cmd_eq(argv[2].p, argv[2].len, "maxmemory")) {
        uint64_t m = strtoull(val, NULL, 10);
        ar_core_set_maxmemory(core, m);
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "requirepass")) {
        if (!ar_core_set_requirepass(core, val))
          return reply_err(c, "ERR config set requirepass");
        /* Existing connections keep auth state; new ones need AUTH if set */
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "protected-mode")) {
        int on = !(strcmp(val, "no") == 0 || strcmp(val, "0") == 0 ||
                   strcmp(val, "false") == 0);
        ar_core_set_protected_mode(core, on);
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "evict-samples") ||
          cmd_eq(argv[2].p, argv[2].len, "samples")) {
        int n = atoi(val);
        if (!ar_core_set_evict_samples(core, n))
          return reply_err(c, "ERR config set evict-samples");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "maxclients")) {
        int n = atoi(val);
        if (!ar_core_set_maxclients(core, n))
          return reply_err(c, "ERR config set maxclients");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "timeout")) {
        int n = atoi(val);
        if (!ar_core_set_timeout(core, n))
          return reply_err(c, "ERR config set timeout");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "tcp-backlog")) {
        int n = atoi(val);
        if (!ar_core_set_tcp_backlog(core, n))
          return reply_err(c, "ERR config set tcp-backlog");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "slowlog-log-slower-than")) {
        int n = atoi(val);
        if (!ar_core_set_slowlog_slower_than(core, n))
          return reply_err(c, "ERR config set slowlog-log-slower-than");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "dir")) {
        if (!ar_core_set_rdb_dir(core, val))
          return reply_err(c, "ERR config set dir");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "dbfilename")) {
        if (!ar_core_set_rdb_filename(core, val))
          return reply_err(c, "ERR config set dbfilename");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "shadow-policy")) {
        if (!ar_core_set_shadow_policy(core, val))
          return reply_err(c, "ERR config set shadow-policy");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "shadow-sample-pct")) {
        int n = atoi(val);
        if (!ar_core_set_shadow_sample_pct(core, n))
          return reply_err(c, "ERR config set shadow-sample-pct");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "hot-soft-cap-pct")) {
        int n = atoi(val);
        if (!ar_core_set_hot_soft_cap_pct(core, n))
          return reply_err(c, "ERR config set hot-soft-cap-pct");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "hot-soft-cap-min")) {
        int n = atoi(val);
        if (!ar_core_set_hot_soft_cap_min(core, n))
          return reply_err(c, "ERR config set hot-soft-cap-min");
        return reply_ok(c);
      }
      if (cmd_eq(argv[2].p, argv[2].len, "hot-promote-on-get")) {
        int on = !(strcmp(val, "no") == 0 || strcmp(val, "0") == 0 ||
                   strcmp(val, "false") == 0);
        if (!ar_core_set_hot_promote_on_get(core, on))
          return reply_err(c, "ERR config set hot-promote-on-get");
        return reply_ok(c);
      }
      return reply_err(c, "ERR Unknown option or number of arguments for CONFIG "
                          "SET");
    }
    return reply_err(c, "ERR CONFIG subcommand must be GET or SET");
  }

  /* INFO — Redis-ish sections; keep flat metric keys for policy_agent (P0.6). */
  if (cmd_eq(cmd, clen, "info")) {
    char hints[160];
    ar_core_policy_hints(core, hints, sizeof(hints));
    int connected_slaves_count = 0;
    for (int i = 0; i < AR_MAX_CONN; ++i)
      if (core->conns[i].in_use && core->conns[i].is_replica)
        connected_slaves_count++;
    char buf[5120];
    int n = snprintf(
        buf, sizeof(buf),
        "# Server\n"
        "aura_redis_version:0.1\n"
        "tcp_port:%d\n"
        "tls_port:%d\n"
        "tls_enabled:%s\n"
        "binding:%s\n"
        "protected_mode:%s\n"
        "requirepass:%s\n"
        "role:%s\n"
        "master_host:%s\n"
        "master_port:%d\n"
        "connected_slaves:%d\n"
        "# Clients\n"
        "connected_clients:%d\n"
        "maxclients:%d\n"
        "timeout:%d\n"
        "tcp_backlog:%d\n"
        "# Memory\n"
        "used_memory:%llu\n"
        "maxmemory:%llu\n"
        "# Stats\n"
        "ops:%llu\n"
        "cmd_lt_1ms:%llu\n"
        "cmd_lt_10ms:%llu\n"
        "cmd_lt_100ms:%llu\n"
        "cmd_ge_100ms:%llu\n"
        "cmd_avg_us:%llu\n"
        "slowlog_count:%llu\n"
        "slowlog_log_slower_than:%d\n"
        "gets:%llu\n"
        "sets:%llu\n"
        "hits:%llu\n"
        "misses:%llu\n"
        "evicted:%llu\n"
        "expired:%llu\n"
        "# Keyspace\n"
        "keys:%llu\n"
        "keys_with_ttl:%llu\n"
        "avg_ttl_ms:%llu\n"
        "# Persistence\n"
        "loading:%d\n"
        "aof_enabled:0\n"
        "rdb_bgsave_in_progress:%d\n"
        "rdb_last_save_time:%llu\n"
        "rdb_last_bgsave_status:%s\n"
        "dir:%s\n"
        "dbfilename:%s\n"
        "aura_rdb:1\n"
        "# Aura\n"
        "evict:%s\n"
        "layout:%s\n"
        "samples:%d\n"
        "pinned:%llu\n"
        "plugin:%d\n"
        "plugin_reloads:%llu\n"
        "layout_gen:%llu\n"
        "hot_keys:%llu\n"
        "cold_keys:%llu\n"
        "policy_hints:%s\n"
        "keys_string:%llu\n"
        "keys_hash:%llu\n"
        "keys_list:%llu\n"
        "keys_zset:%llu\n"
        "mem_string:%llu\n"
        "mem_hash:%llu\n"
        "mem_list:%llu\n"
        "mem_zset:%llu\n"
        "bigkey_bytes:%llu\n"
        "bigkey_type:%s\n"
        "hot_soft_cap_pct:%d\n"
        "hot_soft_cap_min:%d\n"
        "hot_promote_on_get:%s\n"
        "hot_soft_cap:%llu\n"
        "shadow_policy:%s\n"
        "shadow_sample_pct:%d\n"
        "shadow_samples:%llu\n"
        "shadow_hits:%llu\n"
        "shadow_misses:%llu\n"
        "shadow_diverges:%llu\n",
        core->tcp_port,
        core->tls_port,
        core->tls_listen_fd >= 0 ? "yes" : "no",
        core->bind_addr[0] ? core->bind_addr : "127.0.0.1",
        core->protected_mode ? "yes" : "no",
        (core->requirepass && core->requirepass[0]) ? "yes" : "no",
        core->repl_readonly ? "slave" : "master",
        core->master_host[0] ? core->master_host : "",
        core->master_port,
        connected_slaves_count,
        connected_clients(core),
        core->maxclients,
        core->timeout_sec,
        core->tcp_backlog,
        (unsigned long long)ar_core_used_memory(core),
        (unsigned long long)ar_core_maxmemory(core),
        (unsigned long long)ar_metric_ops(core),
        (unsigned long long)core->cmd_lt_1ms,
        (unsigned long long)core->cmd_lt_10ms,
        (unsigned long long)core->cmd_lt_100ms,
        (unsigned long long)core->cmd_ge_100ms,
        (unsigned long long)(core->cmd_latency_samples
                                 ? core->cmd_latency_sum_us / core->cmd_latency_samples
                                 : 0),
        (unsigned long long)core->slowlog_count,
        core->slowlog_slower_than_us,
        (unsigned long long)ar_metric_gets(core),
        (unsigned long long)ar_metric_sets(core),
        (unsigned long long)ar_metric_hits(core),
        (unsigned long long)ar_metric_misses(core),
        (unsigned long long)ar_metric_evicted(core),
        (unsigned long long)ar_metric_expired(core),
        (unsigned long long)ar_core_nkeys(core),
        (unsigned long long)ar_core_keys_with_ttl(core),
        (unsigned long long)ar_core_avg_ttl_ms(core),
        core->rdb_loading ? 1 : 0,
        core->rdb_bgsave_pid > 0 ? 1 : 0,
        (unsigned long long)core->rdb_last_save_time,
        core->rdb_last_bgsave_ok ? "ok" : "err",
        ar_core_rdb_dir(core),
        ar_core_rdb_filename(core),
        ar_core_evict_name(core),
        ar_core_layout_name(core),
        ar_core_evict_samples(core),
        (unsigned long long)ar_core_pinned_keys(core),
        ar_core_has_evict_plugin(core),
        (unsigned long long)ar_metric_plugin_reloads(core),
        (unsigned long long)ar_core_layout_gen(core),
        (unsigned long long)ar_core_hot_keys(core),
        (unsigned long long)ar_core_cold_keys(core),
        hints,
        (unsigned long long)ar_core_type_keys(core, 0),
        (unsigned long long)ar_core_type_keys(core, 1),
        (unsigned long long)ar_core_type_keys(core, 2),
        (unsigned long long)ar_core_type_keys(core, 3),
        (unsigned long long)ar_core_type_bytes(core, 0),
        (unsigned long long)ar_core_type_bytes(core, 1),
        (unsigned long long)ar_core_type_bytes(core, 2),
        (unsigned long long)ar_core_type_bytes(core, 3),
        (unsigned long long)ar_core_bigkey_bytes(core),
        ar_core_bigkey_type_name(core),
        ar_core_hot_soft_cap_pct(core),
        ar_core_hot_soft_cap_min(core),
        ar_core_hot_promote_on_get(core) ? "yes" : "no",
        (unsigned long long)ar_core_hot_soft_cap(core),
        ar_core_shadow_policy(core),
        ar_core_shadow_sample_pct(core),
        (unsigned long long)ar_core_shadow_samples(core),
        (unsigned long long)ar_core_shadow_hits(core),
        (unsigned long long)ar_core_shadow_misses(core),
        (unsigned long long)ar_core_shadow_diverges(core));
    if (n < 0)
      return reply_err(c, "ERR info");
    return reply_bulk(c, buf, (size_t)n);
  }

  /* A10: SHADOW [policy <name>|sample-pct <n>|reset|diverge] — best-effort A/B */
  if (cmd_eq(cmd, clen, "shadow")) {
    if (argc == 1) {
      char buf[512];
      int n = snprintf(
          buf, sizeof(buf),
          "policy:%s sample_pct:%d samples:%llu hits:%llu misses:%llu "
          "diverges:%llu\n",
          ar_core_shadow_policy(core), ar_core_shadow_sample_pct(core),
          (unsigned long long)ar_core_shadow_samples(core),
          (unsigned long long)ar_core_shadow_hits(core),
          (unsigned long long)ar_core_shadow_misses(core),
          (unsigned long long)ar_core_shadow_diverges(core));
      if (n < 0)
        return reply_err(c, "ERR shadow");
      return reply_bulk(c, buf, (size_t)n);
    }
    if (argc == 2 && cmd_eq(argv[1].p, argv[1].len, "reset")) {
      ar_core_shadow_reset(core);
      return reply_ok(c);
    }
    if (argc == 2 && cmd_eq(argv[1].p, argv[1].len, "diverge")) {
      ar_core_shadow_note_diverge(core);
      return reply_ok(c);
    }
    if (argc == 3 && cmd_eq(argv[1].p, argv[1].len, "policy")) {
      char namebuf[64];
      if (argv[2].len >= sizeof(namebuf))
        return reply_err(c, "ERR bad shadow policy");
      memcpy(namebuf, argv[2].p, argv[2].len);
      namebuf[argv[2].len] = '\0';
      if (!ar_core_set_shadow_policy(core, namebuf))
        return reply_err(c, "ERR bad shadow policy");
      return reply_ok(c);
    }
    if (argc == 3 && (cmd_eq(argv[1].p, argv[1].len, "sample-pct") ||
                      cmd_eq(argv[1].p, argv[1].len, "sample_pct"))) {
      char nbuf[32];
      if (argv[2].len == 0 || argv[2].len >= sizeof(nbuf))
        return reply_err(c, "ERR bad sample-pct");
      memcpy(nbuf, argv[2].p, argv[2].len);
      nbuf[argv[2].len] = '\0';
      if (!ar_core_set_shadow_sample_pct(core, atoi(nbuf)))
        return reply_err(c, "ERR bad sample-pct");
      return reply_ok(c);
    }
    return reply_err(c, "ERR wrong number of arguments for 'shadow'");
  }
  /* A9: HOTCOLD [soft-cap-pct <n>|soft-cap-min <n>|promote-on-get <yes|no>] */
  if (cmd_eq(cmd, clen, "hotcold") || cmd_eq(cmd, clen, "hot_cold")) {
    if (argc == 1) {
      char buf[320];
      int n = snprintf(
          buf, sizeof(buf),
          "soft_cap_pct:%d soft_cap_min:%d promote_on_get:%s soft_cap:%llu "
          "hot_keys:%llu cold_keys:%llu demotions:%llu promotions:%llu\n",
          ar_core_hot_soft_cap_pct(core), ar_core_hot_soft_cap_min(core),
          ar_core_hot_promote_on_get(core) ? "yes" : "no",
          (unsigned long long)ar_core_hot_soft_cap(core),
          (unsigned long long)ar_core_hot_keys(core),
          (unsigned long long)ar_core_cold_keys(core),
          (unsigned long long)ar_metric_demotions(core),
          (unsigned long long)ar_metric_promotions(core));
      if (n < 0)
        return reply_err(c, "ERR hotcold");
      return reply_bulk(c, buf, (size_t)n);
    }
    if (argc == 3 && (cmd_eq(argv[1].p, argv[1].len, "soft-cap-pct") ||
                      cmd_eq(argv[1].p, argv[1].len, "soft_cap_pct"))) {
      char nbuf[32];
      if (argv[2].len == 0 || argv[2].len >= sizeof(nbuf))
        return reply_err(c, "ERR bad soft-cap-pct");
      memcpy(nbuf, argv[2].p, argv[2].len);
      nbuf[argv[2].len] = '\0';
      if (!ar_core_set_hot_soft_cap_pct(core, atoi(nbuf)))
        return reply_err(c, "ERR bad soft-cap-pct");
      return reply_ok(c);
    }
    if (argc == 3 && (cmd_eq(argv[1].p, argv[1].len, "soft-cap-min") ||
                      cmd_eq(argv[1].p, argv[1].len, "soft_cap_min"))) {
      char nbuf[32];
      if (argv[2].len == 0 || argv[2].len >= sizeof(nbuf))
        return reply_err(c, "ERR bad soft-cap-min");
      memcpy(nbuf, argv[2].p, argv[2].len);
      nbuf[argv[2].len] = '\0';
      if (!ar_core_set_hot_soft_cap_min(core, atoi(nbuf)))
        return reply_err(c, "ERR bad soft-cap-min");
      return reply_ok(c);
    }
    if (argc == 3 && (cmd_eq(argv[1].p, argv[1].len, "promote-on-get") ||
                      cmd_eq(argv[1].p, argv[1].len, "promote_on_get"))) {
      char vbuf[16];
      if (argv[2].len >= sizeof(vbuf))
        return reply_err(c, "ERR bad promote-on-get");
      memcpy(vbuf, argv[2].p, argv[2].len);
      vbuf[argv[2].len] = '\0';
      int on = !(strcmp(vbuf, "no") == 0 || strcmp(vbuf, "0") == 0 ||
                 strcmp(vbuf, "false") == 0);
      if (!ar_core_set_hot_promote_on_get(core, on))
        return reply_err(c, "ERR bad promote-on-get");
      return reply_ok(c);
    }
    return reply_err(c, "ERR wrong number of arguments for 'hotcold'");
  }
  /* M12: POLICY prefix profile | POLICY (list hints) */
  if (cmd_eq(cmd, clen, "policy")) {
    if (argc == 1) {
      char hints[160];
      int hn = ar_core_policy_hints(core, hints, sizeof(hints));
      return reply_bulk(c, hints, (size_t)(hn < 0 ? 0 : hn));
    }
    if (argc == 3) {
      char pfx[16], prof[32];
      size_t pl = argv[1].len < sizeof(pfx) - 1 ? argv[1].len : sizeof(pfx) - 1;
      size_t rl = argv[2].len < sizeof(prof) - 1 ? argv[2].len : sizeof(prof) - 1;
      memcpy(pfx, argv[1].p, pl);
      pfx[pl] = '\0';
      memcpy(prof, argv[2].p, rl);
      prof[rl] = '\0';
      if (!ar_core_policy_set(core, pfx, prof))
        return reply_err(c, "ERR policy");
      return reply_ok(c);
    }
    return reply_err(c, "ERR wrong number of arguments for 'policy'");
  }
  /* MVP M4: PIN key | UNPIN key | PIN (list) */
  if (cmd_eq(cmd, clen, "pin")) {
    if (argc == 1) {
      char* keys[256];
      size_t n = ar_core_list_pinned(core, keys, 256);
      char hdr[32];
      int hn = snprintf(hdr, sizeof(hdr), "*%zu\r\n", n);
      if (hn < 0 || wbuf_append(c, hdr, (size_t)hn) < 0) {
        for (size_t i = 0; i < n; ++i)
          ar_free(keys[i]);
        return -1;
      }
      for (size_t i = 0; i < n; ++i) {
        size_t kl = strlen(keys[i]);
        if (reply_bulk(c, keys[i], kl) < 0) {
          for (size_t j = i; j < n; ++j)
            ar_free(keys[j]);
          return -1;
        }
        ar_free(keys[i]);
      }
      return 0;
    }
    if (argc == 2) {
      if (!ar_core_pin(core, argv[1].p, argv[1].len))
        return reply_err(c, "ERR pin failed (missing key?)");
      return reply_ok(c);
    }
    return reply_err(c, "ERR wrong number of arguments for 'pin'");
  }
  if (cmd_eq(cmd, clen, "unpin")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'unpin'");
    if (!ar_core_unpin(core, argv[1].p, argv[1].len))
      return reply_int(c, 0);
    return reply_int(c, 1);
  }
  /* Iteration 8: LAYOUT [name] — query or migrate dict layout (quiescent). */
  if (cmd_eq(cmd, clen, "layout")) {
    if (argc == 1) {
      const char* name = ar_core_layout_name(core);
      char buf[128];
      int n = snprintf(buf, sizeof(buf),
                       "%s gen=%llu hot=%llu cold=%llu promo=%llu demo=%llu",
                       name, (unsigned long long)ar_core_layout_gen(core),
                       (unsigned long long)ar_core_hot_keys(core),
                       (unsigned long long)ar_core_cold_keys(core),
                       (unsigned long long)ar_metric_promotions(core),
                       (unsigned long long)ar_metric_demotions(core));
      if (n < 0)
        return reply_err(c, "ERR layout status");
      return reply_bulk(c, buf, (size_t)n);
    }
    if (argc == 2) {
      char namebuf[64];
      if (argv[1].len == 0 || argv[1].len >= sizeof(namebuf))
        return reply_err(c, "ERR bad layout (want flat|hot_cold)");
      memcpy(namebuf, argv[1].p, argv[1].len);
      namebuf[argv[1].len] = '\0';
      if (!ar_core_set_layout(core, namebuf))
        return reply_err(c, "ERR bad layout (want flat|hot_cold)");
      return reply_ok(c);
    }
    return reply_err(c, "ERR wrong number of arguments for 'layout'");
  }
  /* Iteration 7 stretch: PLUGIN [path] — query or live-reload eviction .so
   * without dropping the listen socket (swap between commands). */
  if (cmd_eq(cmd, clen, "plugin")) {
    if (argc == 1) {
      const char* name = ar_core_evict_name(core);
      char buf[192];
      int n = snprintf(buf, sizeof(buf),
                       "%s plugin=%d reloads=%llu",
                       name,
                       ar_core_has_evict_plugin(core),
                       (unsigned long long)ar_metric_plugin_reloads(core));
      if (n < 0)
        return reply_err(c, "ERR plugin status");
      return reply_bulk(c, buf, (size_t)n);
    }
    if (argc == 2) {
      char pathbuf[1024];
      if (argv[1].len == 0 || argv[1].len >= sizeof(pathbuf))
        return reply_err(c, "ERR bad plugin path");
      memcpy(pathbuf, argv[1].p, argv[1].len);
      pathbuf[argv[1].len] = '\0';
      {
        /* Sandbox / Aura-native profile: refuse .so escape hatch. */
        const char* deny = getenv("AURA_REDIS_DENY_PLUGIN");
        if (deny && deny[0] && strcmp(deny, "0") != 0 &&
            strcmp(deny, "false") != 0 && strcmp(deny, "off") != 0 &&
            strcmp(deny, "FALSE") != 0 && strcmp(deny, "OFF") != 0)
          return reply_err(c, "ERR PLUGIN denied (AURA_REDIS_DENY_PLUGIN; Aura-native path)");
      }
      if (!ar_core_load_evict_plugin(core, pathbuf))
        return reply_err(c, "ERR plugin load failed");
      return reply_ok(c);
    }
    return reply_err(c, "ERR wrong number of arguments for 'plugin'");
  }



  /* ---- P3.16c ZSET (sorted array, O(n) insert) ---- */
  if (cmd_eq(cmd, clen, "zadd")) {
    if (argc < 4 || ((argc - 2) % 2) != 0)
      return reply_err(c, "ERR wrong number of arguments for 'zadd'");
    int np = (argc - 2) / 2;
    if (np > 64)
      return reply_err(c, "ERR too many members");
    double scores[64];
    const char* members[64];
    size_t mlens[64];
    for (int i = 0; i < np; ++i) {
      if (!parse_double(argv[2 + 2 * i].p, argv[2 + 2 * i].len, &scores[i]))
        return reply_err(c, "ERR value is not a valid float");
      members[i] = argv[3 + 2 * i].p;
      mlens[i] = argv[3 + 2 * i].len;
    }
    int wt = 0;
    int added = ar_zset_zadd(core, argv[1].p, argv[1].len, np, scores, members,
                             mlens, &wt);
    if (wt || added < 0)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    return reply_int(c, added);
  }
  if (cmd_eq(cmd, clen, "zscore")) {
    if (argc != 3)
      return reply_err(c, "ERR wrong number of arguments for 'zscore'");
    int wt = 0;
    size_t ol = 0;
    char* v = ar_zset_zscore(core, argv[1].p, argv[1].len, argv[2].p,
                             argv[2].len, &ol, &wt);
    if (wt)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!v)
      return reply_null_bulk(c);
    int rc = reply_bulk(c, v, ol);
    free(v);
    return rc;
  }
  if (cmd_eq(cmd, clen, "zrem")) {
    if (argc < 3)
      return reply_err(c, "ERR wrong number of arguments for 'zrem'");
    int nm = argc - 2;
    if (nm > 64)
      return reply_err(c, "ERR too many members");
    const char* members[64];
    size_t mlens[64];
    for (int i = 0; i < nm; ++i) {
      members[i] = argv[2 + i].p;
      mlens[i] = argv[2 + i].len;
    }
    int wt = 0;
    int rem = ar_zset_zrem(core, argv[1].p, argv[1].len, nm, members, mlens, &wt);
    if (wt || rem < 0)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    return reply_int(c, rem);
  }
  if (cmd_eq(cmd, clen, "zcard")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'zcard'");
    int wt = 0;
    int64_t n = ar_zset_zcard(core, argv[1].p, argv[1].len, &wt);
    if (wt || n < 0)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    return reply_int(c, n);
  }
  if (cmd_eq(cmd, clen, "zrange")) {
    if (argc < 4 || argc > 5)
      return reply_err(c, "ERR wrong number of arguments for 'zrange'");
    int withscores = 0;
    if (argc == 5) {
      if (!cmd_eq(argv[4].p, argv[4].len, "withscores"))
        return reply_err(c, "ERR syntax error");
      withscores = 1;
    }
    char nbuf[32];
    long long start, stop;
    char* end = NULL;
    if (argv[2].len == 0 || argv[2].len >= sizeof(nbuf) ||
        argv[3].len == 0 || argv[3].len >= sizeof(nbuf))
      return reply_err(c, "ERR value is not an integer or out of range");
    memcpy(nbuf, argv[2].p, argv[2].len);
    nbuf[argv[2].len] = '\0';
    start = strtoll(nbuf, &end, 10);
    if (end == nbuf || *end != '\0')
      return reply_err(c, "ERR value is not an integer or out of range");
    memcpy(nbuf, argv[3].p, argv[3].len);
    nbuf[argv[3].len] = '\0';
    end = NULL;
    stop = strtoll(nbuf, &end, 10);
    if (end == nbuf || *end != '\0')
      return reply_err(c, "ERR value is not an integer or out of range");
    int wt = 0;
    ArZSet* z = ar_zset_get(core, argv[1].p, argv[1].len, &wt);
    if (wt)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!z || z->len == 0)
      return wbuf_append(c, "*0\r\n", 4);
    int64_t len = (int64_t)z->len;
    if (start < 0)
      start = len + start;
    if (stop < 0)
      stop = len + stop;
    if (start < 0)
      start = 0;
    if (stop >= len)
      stop = len - 1;
    if (start > stop || start >= len)
      return wbuf_append(c, "*0\r\n", 4);
    int nmem = (int)(stop - start + 1);
    int n = withscores ? nmem * 2 : nmem;
    char hdr[32];
    int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", n);
    if (wbuf_append(c, hdr, (size_t)hn) < 0)
      return -1;
    for (int64_t i = start; i <= stop; ++i) {
      if (reply_bulk(c, z->arr[i].member, z->arr[i].mlen) < 0)
        return -1;
      if (withscores) {
        char sbuf[64];
        int sn = snprintf(sbuf, sizeof(sbuf), "%.17g", z->arr[i].score);
        if (sn < 0 || reply_bulk(c, sbuf, (size_t)sn) < 0)
          return -1;
      }
    }
    return 0;
  }
  if (cmd_eq(cmd, clen, "zrangebyscore")) {
    if (argc < 4)
      return reply_err(c, "ERR wrong number of arguments for 'zrangebyscore'");
    double minv, maxv;
    if (!parse_double(argv[2].p, argv[2].len, &minv) ||
        !parse_double(argv[3].p, argv[3].len, &maxv))
      return reply_err(c, "ERR min or max is not a float");
    int withscores = 0;
    int64_t lim_off = 0, lim_cnt = -1;
    for (int ai = 4; ai < argc; ++ai) {
      if (cmd_eq(argv[ai].p, argv[ai].len, "withscores")) {
        withscores = 1;
      } else if (cmd_eq(argv[ai].p, argv[ai].len, "limit")) {
        if (ai + 2 >= argc)
          return reply_err(c, "ERR syntax error");
        char nbuf[32];
        char* end = NULL;
        if (argv[ai + 1].len >= sizeof(nbuf) || argv[ai + 2].len >= sizeof(nbuf))
          return reply_err(c, "ERR value is not an integer or out of range");
        memcpy(nbuf, argv[ai + 1].p, argv[ai + 1].len);
        nbuf[argv[ai + 1].len] = '\0';
        lim_off = strtoll(nbuf, &end, 10);
        if (end == nbuf || *end != '\0')
          return reply_err(c, "ERR value is not an integer or out of range");
        memcpy(nbuf, argv[ai + 2].p, argv[ai + 2].len);
        nbuf[argv[ai + 2].len] = '\0';
        end = NULL;
        lim_cnt = strtoll(nbuf, &end, 10);
        if (end == nbuf || *end != '\0')
          return reply_err(c, "ERR value is not an integer or out of range");
        ai += 2;
      } else {
        return reply_err(c, "ERR syntax error");
      }
    }
    int wt = 0;
    ArZSet* z = ar_zset_get(core, argv[1].p, argv[1].len, &wt);
    if (wt)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!z || z->len == 0)
      return wbuf_append(c, "*0\r\n", 4);
    /* collect matching indices */
    size_t match[256];
    int nm = 0;
    for (size_t i = 0; i < z->len && nm < 256; ++i) {
      double sc = z->arr[i].score;
      if (sc < minv || sc > maxv)
        continue;
      match[nm++] = i;
    }
    int skip = (int)lim_off;
    if (skip < 0)
      skip = 0;
    int take = (lim_cnt < 0) ? nm : (int)lim_cnt;
    if (skip > nm)
      skip = nm;
    int outn = nm - skip;
    if (outn > take)
      outn = take;
    if (outn < 0)
      outn = 0;
    int n = withscores ? outn * 2 : outn;
    char hdr[32];
    int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", n);
    if (wbuf_append(c, hdr, (size_t)hn) < 0)
      return -1;
    for (int i = 0; i < outn; ++i) {
      size_t idx = match[skip + i];
      if (reply_bulk(c, z->arr[idx].member, z->arr[idx].mlen) < 0)
        return -1;
      if (withscores) {
        char sbuf[64];
        int sn = snprintf(sbuf, sizeof(sbuf), "%.17g", z->arr[idx].score);
        if (sn < 0 || reply_bulk(c, sbuf, (size_t)sn) < 0)
          return -1;
      }
    }
    return 0;
  }

  /* ---- P3.16b LIST ---- */
  if (cmd_eq(cmd, clen, "lpush") || cmd_eq(cmd, clen, "rpush")) {
    if (argc < 3)
      return reply_err(c, cmd_eq(cmd, clen, "lpush")
                              ? "ERR wrong number of arguments for 'lpush'"
                              : "ERR wrong number of arguments for 'rpush'");
    int left = cmd_eq(cmd, clen, "lpush");
    int nv = argc - 2;
    const char* vals[64];
    size_t vlens[64];
    if (nv > 64)
      return reply_err(c, "ERR too many values");
    for (int i = 0; i < nv; ++i) {
      vals[i] = argv[2 + i].p;
      vlens[i] = argv[2 + i].len;
    }
    int wt = 0;
    int64_t len = ar_list_push(core, argv[1].p, argv[1].len, left, nv, vals,
                               vlens, &wt);
    if (wt || len < 0)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    return reply_int(c, len);
  }
  if (cmd_eq(cmd, clen, "lpop") || cmd_eq(cmd, clen, "rpop")) {
    if (argc != 2)
      return reply_err(c, cmd_eq(cmd, clen, "lpop")
                              ? "ERR wrong number of arguments for 'lpop'"
                              : "ERR wrong number of arguments for 'rpop'");
    int left = cmd_eq(cmd, clen, "lpop");
    int wt = 0;
    size_t ol = 0;
    char* v = ar_list_pop(core, argv[1].p, argv[1].len, left, &ol, &wt);
    if (wt)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!v)
      return reply_null_bulk(c);
    int rc = reply_bulk(c, v, ol);
    free(v);
    return rc;
  }
  if (cmd_eq(cmd, clen, "llen")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'llen'");
    int wt = 0;
    int64_t n = ar_list_llen(core, argv[1].p, argv[1].len, &wt);
    if (wt || n < 0)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    return reply_int(c, n);
  }
  if (cmd_eq(cmd, clen, "lindex")) {
    if (argc != 3)
      return reply_err(c, "ERR wrong number of arguments for 'lindex'");
    char nbuf[32];
    if (argv[2].len == 0 || argv[2].len >= sizeof(nbuf))
      return reply_err(c, "ERR value is not an integer or out of range");
    memcpy(nbuf, argv[2].p, argv[2].len);
    nbuf[argv[2].len] = '\0';
    char* end = NULL;
    long long idx = strtoll(nbuf, &end, 10);
    if (end == nbuf || *end != '\0')
      return reply_err(c, "ERR value is not an integer or out of range");
    int wt = 0;
    size_t ol = 0;
    char* v = ar_list_lindex(core, argv[1].p, argv[1].len, (int64_t)idx, &ol,
                             &wt);
    if (wt)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!v)
      return reply_null_bulk(c);
    int rc = reply_bulk(c, v, ol);
    free(v);
    return rc;
  }
  if (cmd_eq(cmd, clen, "lrange")) {
    if (argc != 4)
      return reply_err(c, "ERR wrong number of arguments for 'lrange'");
    char nbuf[32];
    long long start, stop;
    char* end = NULL;
    if (argv[2].len == 0 || argv[2].len >= sizeof(nbuf) ||
        argv[3].len == 0 || argv[3].len >= sizeof(nbuf))
      return reply_err(c, "ERR value is not an integer or out of range");
    memcpy(nbuf, argv[2].p, argv[2].len);
    nbuf[argv[2].len] = '\0';
    start = strtoll(nbuf, &end, 10);
    if (end == nbuf || *end != '\0')
      return reply_err(c, "ERR value is not an integer or out of range");
    memcpy(nbuf, argv[3].p, argv[3].len);
    nbuf[argv[3].len] = '\0';
    end = NULL;
    stop = strtoll(nbuf, &end, 10);
    if (end == nbuf || *end != '\0')
      return reply_err(c, "ERR value is not an integer or out of range");
    int wt = 0;
    ArList* l = ar_list_get(core, argv[1].p, argv[1].len, &wt);
    if (wt)
      return reply_err(
          c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!l || l->len == 0)
      return wbuf_append(c, "*0\r\n", 4);
    int64_t len = (int64_t)l->len;
    if (start < 0)
      start = len + start;
    if (stop < 0)
      stop = len + stop;
    if (start < 0)
      start = 0;
    if (stop >= len)
      stop = len - 1;
    if (start > stop || start >= len)
      return wbuf_append(c, "*0\r\n", 4);
    int n = (int)(stop - start + 1);
    char hdr[32];
    int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", n);
    if (wbuf_append(c, hdr, (size_t)hn) < 0)
      return -1;
    ArListNode* node = l->head;
    for (int64_t i = 0; i < start && node; ++i)
      node = node->next;
    for (int i = 0; i < n && node; ++i) {
      if (reply_bulk(c, node->val, node->vlen) < 0)
        return -1;
      node = node->next;
    }
    return 0;
  }

  /* ---- P3.16a HASH ---- */
  if (cmd_eq(cmd, clen, "type")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'type'");
    size_t b = 0;
    int tier = 0;
    ArEntry* e = ar_find_entry_ex(core, argv[1].p, argv[1].len, &b, &tier);
    if (!e)
      return reply_bulk(c, "none", 4);
    const char* tn = ar_type_name(e->type);
    return reply_bulk(c, tn, strlen(tn));
  }
  if (cmd_eq(cmd, clen, "hset")) {
    if (argc < 4 || ((argc - 2) % 2) != 0)
      return reply_err(c, "ERR wrong number of arguments for 'hset'");
    int npairs = (argc - 2) / 2;
    const char* fields[64];
    size_t flens[64];
    const char* vals[64];
    size_t vlens[64];
    if (npairs > 64)
      return reply_err(c, "ERR too many fields");
    for (int i = 0; i < npairs; ++i) {
      fields[i] = argv[2 + 2 * i].p;
      flens[i] = argv[2 + 2 * i].len;
      vals[i] = argv[3 + 2 * i].p;
      vlens[i] = argv[3 + 2 * i].len;
    }
    int wt = 0;
    int added = ar_hash_hset(core, argv[1].p, argv[1].len, npairs, fields, flens,
                             vals, vlens, &wt);
    if (wt)
      return reply_err(c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (added < 0)
      return reply_err(c, "ERR hset failed");
    return reply_int(c, added);
  }
  if (cmd_eq(cmd, clen, "hget")) {
    if (argc != 3)
      return reply_err(c, "ERR wrong number of arguments for 'hget'");
    int wt = 0;
    size_t ol = 0;
    char* v = ar_hash_hget(core, argv[1].p, argv[1].len, argv[2].p, argv[2].len,
                           &ol, &wt);
    if (wt)
      return reply_err(c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!v)
      return reply_null_bulk(c);
    int rc = reply_bulk(c, v, ol);
    free(v);
    return rc;
  }
  if (cmd_eq(cmd, clen, "hmget")) {
    if (argc < 3)
      return reply_err(c, "ERR wrong number of arguments for 'hmget'");
    int wt = 0;
    ArEntry* e =
        ar_entry_get_typed(core, argv[1].p, argv[1].len, AR_TYPE_HASH, NULL, NULL,
                           &wt);
    if (wt)
      return reply_err(c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    char hdr[32];
    int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", argc - 2);
    if (wbuf_append(c, hdr, (size_t)hn) < 0)
      return -1;
    if (!e) {
      for (int i = 2; i < argc; ++i)
        if (reply_null_bulk(c) < 0)
          return -1;
      return 0;
    }
    for (int i = 2; i < argc; ++i) {
      size_t ol = 0;
      int wt2 = 0;
      char* v = ar_hash_hget(core, argv[1].p, argv[1].len, argv[i].p, argv[i].len,
                             &ol, &wt2);
      if (!v) {
        if (reply_null_bulk(c) < 0)
          return -1;
      } else {
        if (reply_bulk(c, v, ol) < 0) {
          free(v);
          return -1;
        }
        free(v);
      }
    }
    return 0;
  }
  if (cmd_eq(cmd, clen, "hgetall")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'hgetall'");
    int wt = 0;
    ArEntry* e =
        ar_entry_get_typed(core, argv[1].p, argv[1].len, AR_TYPE_HASH, NULL, NULL,
                           &wt);
    if (wt)
      return reply_err(c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (!e) {
      return wbuf_append(c, "*0\r\n", 4);
    }
    ArHash* h = (ArHash*)e->obj;
    int n = (int)(h->nfields * 2);
    char hdr[32];
    int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", n);
    if (wbuf_append(c, hdr, (size_t)hn) < 0)
      return -1;
    for (size_t bi = 0; bi < h->nbuckets; ++bi) {
      for (ArHashField* f = h->buckets[bi]; f; f = f->next) {
        if (reply_bulk(c, f->field, f->flen) < 0)
          return -1;
        if (reply_bulk(c, f->val, f->vlen) < 0)
          return -1;
      }
    }
    return 0;
  }
  if (cmd_eq(cmd, clen, "hdel")) {
    if (argc < 3)
      return reply_err(c, "ERR wrong number of arguments for 'hdel'");
    int nf = argc - 2;
    const char* fields[64];
    size_t flens[64];
    if (nf > 64)
      return reply_err(c, "ERR too many fields");
    for (int i = 0; i < nf; ++i) {
      fields[i] = argv[2 + i].p;
      flens[i] = argv[2 + i].len;
    }
    int wt = 0;
    int rem = ar_hash_hdel(core, argv[1].p, argv[1].len, nf, fields, flens, &wt);
    if (wt || rem < 0)
      return reply_err(c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    return reply_int(c, rem);
  }
  if (cmd_eq(cmd, clen, "hexists")) {
    if (argc != 3)
      return reply_err(c, "ERR wrong number of arguments for 'hexists'");
    int wt = 0;
    int ex = ar_hash_hexists(core, argv[1].p, argv[1].len, argv[2].p, argv[2].len,
                             &wt);
    if (wt || ex < 0)
      return reply_err(c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    return reply_int(c, ex);
  }
  if (cmd_eq(cmd, clen, "hlen")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'hlen'");
    int wt = 0;
    int64_t n = ar_hash_hlen(core, argv[1].p, argv[1].len, &wt);
    if (wt || n < 0)
      return reply_err(c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    return reply_int(c, n);
  }
  if (cmd_eq(cmd, clen, "hincrby")) {
    if (argc != 4)
      return reply_err(c, "ERR wrong number of arguments for 'hincrby'");
    char nbuf[32];
    if (argv[3].len == 0 || argv[3].len >= sizeof(nbuf))
      return reply_err(c, "ERR value is not an integer or out of range");
    memcpy(nbuf, argv[3].p, argv[3].len);
    nbuf[argv[3].len] = '\0';
    char* end = NULL;
    long long incr = strtoll(nbuf, &end, 10);
    if (end == nbuf || *end != '\0')
      return reply_err(c, "ERR value is not an integer or out of range");
    int wt = 0, ni = 0;
    int64_t v = ar_hash_hincrby(core, argv[1].p, argv[1].len, argv[2].p,
                                argv[2].len, (int64_t)incr, &wt, &ni);
    if (wt)
      return reply_err(c, "WRONGTYPE Operation against a key holding the wrong kind of value");
    if (ni)
      return reply_err(c, "ERR hash value is not an integer");
    return reply_int(c, v);
  }


  /* ---- P3.17b Pub/Sub ---- */
  if (cmd_eq(cmd, clen, "subscribe")) {
    if (argc < 2)
      return reply_err(c, "ERR wrong number of arguments for 'subscribe'");
    for (int i = 1; i < argc; ++i) {
      int n = conn_add_sub(c, argv[i].p, argv[i].len);
      if (n < 0)
        return reply_err(c, "ERR subscribe failed");
      if (reply_pubsub_msg(c, "subscribe", 9, argv[i].p, argv[i].len, n) < 0)
        return -1;
    }
    return 0;
  }
  if (cmd_eq(cmd, clen, "unsubscribe")) {
    if (argc == 1) {
      /* unsubscribe all */
      while (c->nsubs > 0) {
        char* ch = c->sub_channels[0];
        size_t cl = c->sub_clens[0];
        /* copy name before delete */
        char* keep = ar_xmemdup(ch, cl);
        size_t kl = cl;
        int n = conn_del_sub(c, ch, cl);
        if (reply_pubsub_msg(c, "unsubscribe", 11, keep ? keep : "", keep ? kl : 0,
                             n) < 0) {
          free(keep);
          return -1;
        }
        free(keep);
      }
      return 0;
    }
    for (int i = 1; i < argc; ++i) {
      int n = conn_del_sub(c, argv[i].p, argv[i].len);
      if (reply_pubsub_msg(c, "unsubscribe", 11, argv[i].p, argv[i].len, n) < 0)
        return -1;
    }
    return 0;
  }
  if (cmd_eq(cmd, clen, "publish")) {
    if (argc != 3)
      return reply_err(c, "ERR wrong number of arguments for 'publish'");
    int n = publish_local(core, argv[1].p, argv[1].len, argv[2].p, argv[2].len);
    return reply_int(c, n);
  }

  return reply_err(c, "ERR unknown command");
}


static int flush_writes(ArCore* core, ArConn* c) {
  while (c->woff < c->wlen) {
    ssize_t n;
    int want_read = 0;
    if (c->is_tls) {
      if (!c->ssl_hs_done) {
        int hs = ar_tls_handshake(core, c);
        if (hs < 0)
          return -1;
        if (hs == 0)
          return 0;
      }
      n = ar_tls_write(c, c->wbuf + c->woff, c->wlen - c->woff, &want_read);
      if (n < 0)
        return -1;
      if (n == 0) {
        c->want_write = want_read ? 0 : 1;
        struct epoll_event ev;
        ev.events = EPOLLIN | EPOLLET;
        if (!want_read)
          ev.events |= EPOLLOUT;
        ev.data.ptr = c;
        epoll_ctl(core->epfd, EPOLL_CTL_MOD, c->fd, &ev);
        return 0;
      }
    } else {
      n = write(c->fd, c->wbuf + c->woff, c->wlen - c->woff);
      if (n < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
          c->want_write = 1;
          struct epoll_event ev;
          ev.events = EPOLLIN | EPOLLOUT | EPOLLET;
          ev.data.ptr = c;
          epoll_ctl(core->epfd, EPOLL_CTL_MOD, c->fd, &ev);
          return 0;
        }
        return -1;
      }
    }
    c->woff += (size_t)n;
  }
  c->wlen = 0;
  c->woff = 0;
  if (c->want_write) {
    c->want_write = 0;
    struct epoll_event ev;
    ev.events = EPOLLIN | EPOLLET;
    ev.data.ptr = c;
    epoll_ctl(core->epfd, EPOLL_CTL_MOD, c->fd, &ev);
  }
  if (c->should_close) {
    conn_close(core, c);
  }
  return 0;
}

static int process_reads(ArCore* core, ArConn* c) {
  /* Master→replica feed: ignore replica ACKs/noise on the write link. */
  if (c->is_replica) {
    c->rlen = 0;
    return flush_writes(core, c);
  }
  Arg argv[AR_MAX_ARGV];
  size_t pos = 0;
  while (pos < c->rlen) {
    int argc = 0;
    int consumed = parse_command(c->rbuf, c->rlen, pos, argv, &argc);
    if (consumed == 0)
      break;
    if (consumed < 0) {
      reply_err(c, "ERR Protocol error");
      c->should_close = 1;
      break;
    }
    {
      uint64_t t0 = ar_now_us();
      if (c->is_master_link)
        core->repl_applying = 1;
      int dr = dispatch(core, c, argv, argc);
      if (c->is_master_link) {
        core->repl_applying = 0;
        /* Drop +OK etc. — master does not read replica replies as commands. */
        c->wlen = 0;
        c->woff = 0;
      }
      note_cmd_latency(core, ar_now_us() - t0);
      if (dr < 0) {
        c->should_close = 1;
        break;
      }
    }
    c->last_active_ms = ar_now_ms();
    pos += (size_t)consumed;
    if (c->should_close)
      break;
  }
  if (pos > 0) {
    size_t remain = c->rlen - pos;
    if (remain)
      memmove(c->rbuf, c->rbuf + pos, remain);
    c->rlen = remain;
  }
  return flush_writes(core, c);
}

static int handle_read(ArCore* core, ArConn* c) {
  if (c->is_tls && !c->ssl_hs_done) {
    int hs = ar_tls_handshake(core, c);
    if (hs < 0)
      return -1;
    if (hs == 0)
      return 0;
  }
  for (;;) {
    if (c->rlen == c->rcap) {
      size_t ncap = c->rcap * 2;
      if (ncap > 16 * 1024 * 1024) {
        c->should_close = 1;
        return -1;
      }
      char* p = (char*)realloc(c->rbuf, ncap);
      if (!p) {
        c->should_close = 1;
        return -1;
      }
      c->rbuf = p;
      c->rcap = ncap;
    }
    ssize_t n;
    if (c->is_tls) {
      int want_write = 0;
      n = ar_tls_read(c, c->rbuf + c->rlen, c->rcap - c->rlen, &want_write);
      if (n == -2) {
        conn_close(core, c);
        return 0;
      }
      if (n < 0)
        return -1;
      if (n == 0) {
        if (want_write) {
          c->want_write = 1;
          struct epoll_event ev;
          ev.events = EPOLLIN | EPOLLOUT | EPOLLET;
          ev.data.ptr = c;
          epoll_ctl(core->epfd, EPOLL_CTL_MOD, c->fd, &ev);
        }
        break;
      }
      c->rlen += (size_t)n;
      continue;
    }
    n = read(c->fd, c->rbuf + c->rlen, c->rcap - c->rlen);
    if (n < 0) {
      if (errno == EAGAIN || errno == EWOULDBLOCK)
        break;
      return -1;
    }
    if (n == 0) {
      conn_close(core, c);
      return 0;
    }
    c->rlen += (size_t)n;
  }
  if (!c->in_use)
    return 0;
  return process_reads(core, c);
}

static int peer_is_loopback(const struct sockaddr_in* addr) {
  uint32_t a = ntohl(addr->sin_addr.s_addr);
  return (a >> 24) == 127;
}

static int accept_clients_on(ArCore* core, int listen_fd, int is_tls) {
  if (core->shutting_down || listen_fd < 0)
    return 0;
  for (;;) {
    struct sockaddr_in addr;
    socklen_t alen = sizeof(addr);
    int fd = accept(listen_fd, (struct sockaddr*)&addr, &alen);
    if (fd < 0) {
      if (errno == EAGAIN || errno == EWOULDBLOCK)
        return 0;
      return -1;
    }
    /* P0.4 protected-mode: no password + non-loopback peer → refuse */
    int has_pass = core->requirepass && core->requirepass[0];
    if (core->protected_mode && !has_pass && !peer_is_loopback(&addr)) {
      if (!is_tls) {
        static const char denied[] =
            "-DENIED aura-redis is running in protected mode because "
            "protected-mode is enabled and no requirepass is set. Connect "
            "from loopback, set --requirepass, or --protected-mode no.\r\n";
        (void)!write(fd, denied, sizeof(denied) - 1);
      }
      close(fd);
      continue;
    }
    set_nonblock(fd);
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    if (connected_clients(core) >= core->maxclients) {
      if (!is_tls) {
        static const char full[] =
            "-ERR max number of clients reached\r\n";
        (void)!write(fd, full, sizeof(full) - 1);
      }
      close(fd);
      continue;
    }
    ArConn* c = conn_alloc(core, fd);
    if (!c) {
      if (!is_tls) {
        static const char full[] =
            "-ERR max number of clients reached\r\n";
        (void)!write(fd, full, sizeof(full) - 1);
      }
      close(fd);
      continue;
    }
    if (is_tls) {
      if (ar_tls_accept_setup(core, c) < 0) {
        conn_close(core, c);
        continue;
      }
    }
    struct epoll_event ev;
    ev.events = EPOLLIN | EPOLLET;
    ev.data.ptr = c;
    if (epoll_ctl(core->epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {
      conn_close(core, c);
      continue;
    }
    if (is_tls) {
      /* Kick off non-blocking handshake (may complete immediately). */
      if (ar_tls_handshake(core, c) < 0) {
        conn_close(core, c);
        continue;
      }
    }
  }
}


void ar_net_shutdown(ArCore* core) {
  if (!core)
    return;
  for (int i = 0; i < AR_MAX_CONN; ++i) {
    if (core->conns[i].in_use)
      conn_close(core, &core->conns[i]);
    free(core->conns[i].rbuf);
    free(core->conns[i].wbuf);
    core->conns[i].rbuf = NULL;
    core->conns[i].wbuf = NULL;
  }
  if (core->listen_fd >= 0) {
    close(core->listen_fd);
    core->listen_fd = -1;
  }
  if (core->tls_listen_fd >= 0) {
    if (core->epfd >= 0)
      epoll_ctl(core->epfd, EPOLL_CTL_DEL, core->tls_listen_fd, NULL);
    close(core->tls_listen_fd);
    core->tls_listen_fd = -1;
  }
  ar_tls_free_ctx(core);
  if (core->epfd >= 0) {
    close(core->epfd);
    core->epfd = -1;
  }
}

int ar_core_listen(ArCore* core, int port) {
  if (!core || port <= 0 || port > 65535)
    return 0;
  if (core->listen_fd >= 0)
    return 0;

  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    return 0;
  int one = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  set_nonblock(fd);

  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons((uint16_t)port);
  if (!core->bind_addr[0])
    snprintf(core->bind_addr, sizeof(core->bind_addr), "127.0.0.1");
  if (inet_pton(AF_INET, core->bind_addr, &addr.sin_addr) != 1) {
    fprintf(stderr, "aura-redis: bad --bind %s\n", core->bind_addr);
    close(fd);
    return 0;
  }

  if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
    close(fd);
    return 0;
  }
  int backlog = core->tcp_backlog > 0 ? core->tcp_backlog : 512;
  if (listen(fd, backlog) < 0) {
    close(fd);
    return 0;
  }

  int ep = epoll_create1(0);
  if (ep < 0) {
    close(fd);
    return 0;
  }
  struct epoll_event ev;
  ev.events = EPOLLIN | EPOLLET;
  ev.data.ptr = AR_EPOLL_LISTEN_PLAIN;
  if (epoll_ctl(ep, EPOLL_CTL_ADD, fd, &ev) < 0) {
    close(ep);
    close(fd);
    return 0;
  }

  core->listen_fd = fd;
  core->epfd = ep;
  core->tcp_port = port;
  core->quit = 0;
  core->shutting_down = 0;
  fprintf(stderr,
          "aura-redis-ffi listening on %s:%d (protected-mode=%s requirepass=%s; "
          "C epoll)\n",
          core->bind_addr, port, core->protected_mode ? "yes" : "no",
          (core->requirepass && core->requirepass[0]) ? "yes" : "no");
  fflush(stderr);
  return 1;
}



int ar_core_listen_tls(ArCore* core, int port) {
  if (!core || port <= 0 || port > 65535)
    return 0;
  if (!ar_tls_available()) {
    fprintf(stderr, "aura-redis: TLS requested but built without OpenSSL\n");
    return 0;
  }
  if (core->epfd < 0) {
    fprintf(stderr, "aura-redis: call ar_core_listen before ar_core_listen_tls\n");
    return 0;
  }
  if (core->tls_listen_fd >= 0)
    return 0;
  if (!ar_tls_setup_ctx(core))
    return 0;

  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    return 0;
  int one = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  set_nonblock(fd);

  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons((uint16_t)port);
  if (!core->bind_addr[0])
    snprintf(core->bind_addr, sizeof(core->bind_addr), "127.0.0.1");
  if (inet_pton(AF_INET, core->bind_addr, &addr.sin_addr) != 1) {
    fprintf(stderr, "aura-redis: bad --bind %s\n", core->bind_addr);
    close(fd);
    return 0;
  }
  if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
    fprintf(stderr, "aura-redis: tls bind %s:%d failed\n", core->bind_addr, port);
    close(fd);
    return 0;
  }
  int backlog = core->tcp_backlog > 0 ? core->tcp_backlog : 512;
  if (listen(fd, backlog) < 0) {
    close(fd);
    return 0;
  }
  struct epoll_event ev;
  ev.events = EPOLLIN | EPOLLET;
  ev.data.ptr = AR_EPOLL_LISTEN_TLS;
  if (epoll_ctl(core->epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {
    close(fd);
    return 0;
  }
  core->tls_listen_fd = fd;
  core->tls_port = port;
  fprintf(stderr,
          "aura-redis-ffi TLS listening on %s:%d (cert=%s)\n",
          core->bind_addr, port, core->tls_cert_file);
  fflush(stderr);
  return 1;
}

/* P1.12: close idle clients past timeout_sec (0 = disabled). */
static void close_idle_clients(ArCore* core) {
  if (!core || core->timeout_sec <= 0)
    return;
  uint64_t now = ar_now_ms();
  uint64_t limit = (uint64_t)core->timeout_sec * 1000ull;
  for (int i = 0; i < AR_MAX_CONN; ++i) {
    ArConn* c = &core->conns[i];
    if (!c->in_use)
      continue;
    if (c->is_replica || c->is_master_link)
      continue;
    if (c->last_active_ms && now > c->last_active_ms &&
        (now - c->last_active_ms) >= limit) {
      conn_close(core, c);
    }
  }
}

static int serve_once(ArCore* core, int timeout_ms) {
  /* P0.3: active expire between epoll wakes (also on idle timeout). */
  if (core->keys_with_ttl)
    ar_core_active_expire(core, 16);
  ar_rdb_poll_bgsave(core);
  close_idle_clients(core);
  struct epoll_event events[AR_MAX_EVENTS];
  int n = epoll_wait(core->epfd, events, AR_MAX_EVENTS, timeout_ms);
  if (n < 0) {
    if (errno == EINTR)
      return 0;
    return -1;
  }
  for (int i = 0; i < n; ++i) {
    ArConn* c = (ArConn*)events[i].data.ptr;
    if (c == AR_EPOLL_LISTEN_PLAIN) {
      accept_clients_on(core, core->listen_fd, 0);
      continue;
    }
    if (c == AR_EPOLL_LISTEN_TLS) {
      accept_clients_on(core, core->tls_listen_fd, 1);
      continue;
    }
    if (!c->in_use)
      continue;
    uint32_t ev = events[i].events;
    if (ev & (EPOLLERR | EPOLLHUP)) {
      conn_close(core, c);
      continue;
    }
    if (ev & EPOLLIN) {
      if (handle_read(core, c) < 0)
        conn_close(core, c);
    }
    if (c->in_use && (ev & EPOLLOUT)) {
      if (flush_writes(core, c) < 0)
        conn_close(core, c);
    }
  }
  return 0;
}

static void stop_accepting(ArCore* core) {
  if (!core)
    return;
  if (core->listen_fd >= 0) {
    epoll_ctl(core->epfd, EPOLL_CTL_DEL, core->listen_fd, NULL);
    close(core->listen_fd);
    core->listen_fd = -1;
  }
  if (core->tls_listen_fd >= 0) {
    epoll_ctl(core->epfd, EPOLL_CTL_DEL, core->tls_listen_fd, NULL);
    close(core->tls_listen_fd);
    core->tls_listen_fd = -1;
  }
}

/* Flush pending replies then close clients; hard-timeout closes remainder. */
static void drain_clients(ArCore* core, int timeout_ms) {
  uint64_t deadline = ar_now_ms() + (uint64_t)(timeout_ms > 0 ? timeout_ms : 0);
  for (;;) {
    int pending_write = 0;
    for (int i = 0; i < AR_MAX_CONN; ++i) {
      ArConn* c = &core->conns[i];
      if (!c->in_use)
        continue;
      if (c->woff < c->wlen) {
        pending_write = 1;
        if (flush_writes(core, c) < 0)
          conn_close(core, c);
      } else {
        conn_close(core, c);
      }
    }
    if (!pending_write)
      break;
    if (timeout_ms <= 0 || ar_now_ms() >= deadline)
      break;
    if (core->epfd >= 0)
      serve_once(core, 50);
  }
  for (int i = 0; i < AR_MAX_CONN; ++i) {
    if (core->conns[i].in_use)
      conn_close(core, &core->conns[i]);
  }
}

int ar_core_serve_ms(ArCore* core, int ms) {
  if (!core ||
      (core->listen_fd < 0 && core->tls_listen_fd < 0 && !core->shutting_down))
    return -1;
  if (core->quit && !core->shutting_down)
    return 1;
  if (core->epfd < 0)
    return core->quit ? 1 : -1;
  int rc = serve_once(core, ms < 0 ? -1 : ms);
  if (rc < 0)
    return -1;
  return core->quit ? 1 : 0;
}

int ar_core_serve_forever(ArCore* core) {
  if (!core || core->listen_fd < 0)
    return -1;
  while (!core->quit) {
    if (serve_once(core, 1000) < 0)
      return -1;
  }
  /* P0.5: stop accept, drain writes (2s), close, exit cleanly */
  core->shutting_down = 1;
  stop_accepting(core);
  drain_clients(core, 2000);
  fprintf(stderr, "aura-redis: graceful shutdown complete\n");
  fflush(stderr);
  return 0;
}

static ArCore* g_signal_core = NULL;

static void ar_on_signal(int sig) {
  (void)sig;
  if (g_signal_core) {
    g_signal_core->quit = 1;
    g_signal_core->shutting_down = 1;
  }
}

void ar_core_install_signal_handlers(ArCore* core) {
  g_signal_core = core;
  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sa.sa_handler = ar_on_signal;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;
  sigaction(SIGTERM, &sa, NULL);
  sigaction(SIGINT, &sa, NULL);
}

