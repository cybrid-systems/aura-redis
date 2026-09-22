#include "ar_internal.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
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

#define AR_MAX_ARGV 64
#define AR_MAX_EVENTS 64

void ar_net_shutdown(ArCore* core);

static int set_nonblock(int fd) {
  int fl = fcntl(fd, F_GETFL, 0);
  if (fl < 0)
    return -1;
  return fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

static void conn_reset(ArConn* c) {
  c->fd = -1;
  c->in_use = 0;
  c->rlen = 0;
  c->wlen = 0;
  c->woff = 0;
  c->should_close = 0;
  c->want_write = 0;
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
    n = n * 10 + (buf[i] - '0');
    i++;
    digits++;
  }
  if (!digits || i + 1 >= len || buf[i] != '\r' || buf[i + 1] != '\n')
    return 0; /* incomplete or bad */
  i += 2;
  if (neg) {
    out->p = NULL;
    out->len = (size_t)-1;
    *next = i;
    return (int)(i - pos);
  }
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
  if (!digits)
    return 0;
  if (i + 1 >= len || buf[i] != '\r' || buf[i + 1] != '\n')
    return 0;
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
    i = next;
  }
  *argc_out = argc;
  return (int)(i - pos);
}

/* Pointer-stable get without malloc: returns entry val pointer.
 * Promotes cold→hot under hot_cold layout. */
static int get_ptr(ArCore* core, const char* key, size_t klen,
                   const char** val, size_t* vlen) {
  core->ops++;
  core->gets++;
  size_t b = 0;
  int tier = 0;
  ArEntry* e = ar_find_entry_ex(core, key, klen, &b, &tier);
  if (!e) {
    core->misses++;
    *val = NULL;
    *vlen = 0;
    return 0;
  }
  core->hits++;
  ar_touch_get(core, e, b, tier);
  if (core->evict && core->evict->on_get)
    core->evict->on_get(core, e);
  *val = e->val;
  *vlen = e->vlen;
  return 1;
}

static int dispatch(ArCore* core, ArConn* c, Arg* argv, int argc) {
  if (argc < 1)
    return reply_err(c, "ERR empty command");
  const char* cmd = argv[0].p;
  size_t clen = argv[0].len;

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
  if (cmd_eq(cmd, clen, "get")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'get'");
    const char* v;
    size_t vl;
    if (!get_ptr(core, argv[1].p, argv[1].len, &v, &vl))
      return reply_null_bulk(c);
    return reply_bulk(c, v, vl);
  }
  if (cmd_eq(cmd, clen, "set")) {
    /* SET key value [ignored extras for now] */
    if (argc < 3)
      return reply_err(c, "ERR wrong number of arguments for 'set'");
    core->ops++;
    core->sets++;
    if (!ar_entry_set(core, argv[1].p, argv[1].len, argv[2].p, argv[2].len))
      return reply_err(c, "ERR OOM");
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "del")) {
    if (argc < 2)
      return reply_err(c, "ERR wrong number of arguments for 'del'");
    int64_t n = 0;
    for (int i = 1; i < argc; ++i) {
      if (ar_del_bin(core, argv[i].p, argv[i].len))
        n++;
    }
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
    /* *N\r\n then bulks */
    char hdr[32];
    int hn = snprintf(hdr, sizeof(hdr), "*%d\r\n", argc - 1);
    if (wbuf_append(c, hdr, (size_t)hn) < 0)
      return -1;
    for (int i = 1; i < argc; ++i) {
      const char* v;
      size_t vl;
      if (!get_ptr(core, argv[i].p, argv[i].len, &v, &vl)) {
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
    return reply_ok(c);
  }
  if (cmd_eq(cmd, clen, "incr")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'incr'");
    int ok = 0;
    int64_t v = ar_incr(core, argv[1].p, argv[1].len, 1, &ok);
    if (!ok)
      return reply_err(c, "ERR value is not an integer or out of range");
    return reply_int(c, v);
  }
  if (cmd_eq(cmd, clen, "decr")) {
    if (argc != 2)
      return reply_err(c, "ERR wrong number of arguments for 'decr'");
    int ok = 0;
    int64_t v = ar_incr(core, argv[1].p, argv[1].len, -1, &ok);
    if (!ok)
      return reply_err(c, "ERR value is not an integer or out of range");
    return reply_int(c, v);
  }
  if (cmd_eq(cmd, clen, "flushdb")) {
    ar_flushdb(core);
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
        return reply_err(c, "ERR bad evict (want noop|lru|lfu)");
      memcpy(namebuf, argv[1].p, argv[1].len);
      namebuf[argv[1].len] = '\0';
      if (!ar_core_set_evict_by_name(core, namebuf))
        return reply_err(c, "ERR bad evict (want noop|lru|lfu)");
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
  /* INFO — multi-signal metrics for Aura policy agent (MVP M1). */
  if (cmd_eq(cmd, clen, "info")) {
    char buf[768];
    int n = snprintf(buf, sizeof(buf),
                     "# aura-redis\n"
                     "evict:%s\n"
                     "layout:%s\n"
                     "gets:%llu\n"
                     "sets:%llu\n"
                     "hits:%llu\n"
                     "misses:%llu\n"
                     "evicted:%llu\n"
                     "keys:%llu\n"
                     "samples:%d\n"
                     "pinned:%llu\n"
                     "used_memory:%llu\n"
                     "maxmemory:%llu\n"
                     "plugin:%d\n"
                     "plugin_reloads:%llu\n"
                     "layout_gen:%llu\n"
                     "hot_keys:%llu\n"
                     "cold_keys:%llu\n",
                     ar_core_evict_name(core),
                     ar_core_layout_name(core),
                     (unsigned long long)ar_metric_gets(core),
                     (unsigned long long)ar_metric_sets(core),
                     (unsigned long long)ar_metric_hits(core),
                     (unsigned long long)ar_metric_misses(core),
                     (unsigned long long)ar_metric_evicted(core),
                     (unsigned long long)ar_core_nkeys(core),
                     ar_core_evict_samples(core),
                     (unsigned long long)ar_core_pinned_keys(core),
                     (unsigned long long)ar_core_used_memory(core),
                     (unsigned long long)ar_core_maxmemory(core),
                     ar_core_has_evict_plugin(core),
                     (unsigned long long)ar_metric_plugin_reloads(core),
                     (unsigned long long)ar_core_layout_gen(core),
                     (unsigned long long)ar_core_hot_keys(core),
                     (unsigned long long)ar_core_cold_keys(core));
    if (n < 0)
      return reply_err(c, "ERR info");
    return reply_bulk(c, buf, (size_t)n);
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
  return reply_err(c, "ERR unknown command");
}


static int flush_writes(ArCore* core, ArConn* c) {
  while (c->woff < c->wlen) {
    ssize_t n =
        write(c->fd, c->wbuf + c->woff, c->wlen - c->woff);
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
    if (dispatch(core, c, argv, argc) < 0) {
      c->should_close = 1;
      break;
    }
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
    ssize_t n = read(c->fd, c->rbuf + c->rlen, c->rcap - c->rlen);
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
  return process_reads(core, c);
}

static int accept_clients(ArCore* core) {
  for (;;) {
    struct sockaddr_in addr;
    socklen_t alen = sizeof(addr);
    int fd = accept(core->listen_fd, (struct sockaddr*)&addr, &alen);
    if (fd < 0) {
      if (errno == EAGAIN || errno == EWOULDBLOCK)
        return 0;
      return -1;
    }
    set_nonblock(fd);
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    ArConn* c = conn_alloc(core, fd);
    if (!c) {
      close(fd);
      continue;
    }
    struct epoll_event ev;
    ev.events = EPOLLIN | EPOLLET;
    ev.data.ptr = c;
    if (epoll_ctl(core->epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {
      conn_close(core, c);
      continue;
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
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK); /* 127.0.0.1 only */

  if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
    close(fd);
    return 0;
  }
  if (listen(fd, 512) < 0) {
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
  ev.data.ptr = NULL; /* listen marker */
  if (epoll_ctl(ep, EPOLL_CTL_ADD, fd, &ev) < 0) {
    close(ep);
    close(fd);
    return 0;
  }

  core->listen_fd = fd;
  core->epfd = ep;
  core->quit = 0;
  fprintf(stderr, "aura-redis-ffi listening on 127.0.0.1:%d (loopback only; C epoll)\n",
          port);
  fflush(stderr);
  return 1;
}

static int serve_once(ArCore* core, int timeout_ms) {
  struct epoll_event events[AR_MAX_EVENTS];
  int n = epoll_wait(core->epfd, events, AR_MAX_EVENTS, timeout_ms);
  if (n < 0) {
    if (errno == EINTR)
      return 0;
    return -1;
  }
  for (int i = 0; i < n; ++i) {
    ArConn* c = (ArConn*)events[i].data.ptr;
    if (!c) {
      accept_clients(core);
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

int ar_core_serve_ms(ArCore* core, int ms) {
  if (!core || core->listen_fd < 0)
    return -1;
  if (core->quit)
    return 1;
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
  return 0;
}
