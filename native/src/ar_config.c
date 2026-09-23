/* Durable CONFIG file: load on boot, rewrite on CONFIG SET / REWRITE.
 * Format: one "key value" per line (# comments / blank skipped). Redis-ish,
 * not a full Redis CONFIG REWRITE of redis.conf. */
#include "ar_internal.h"
#include "ar_core.h"

#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int ar_core_set_config_file(ArCore* core, const char* path) {
  if (!core)
    return 0;
  if (!path || !path[0]) {
    core->config_path[0] = '\0';
    return 1;
  }
  size_t n = strlen(path);
  if (n >= sizeof(core->config_path))
    return 0;
  memcpy(core->config_path, path, n + 1);
  return 1;
}

const char* ar_core_config_file(ArCore* core) {
  return core ? core->config_path : "";
}

static int key_eq(const char* a, const char* lit) {
  size_t n = strlen(lit);
  size_t i = 0;
  for (; a[i] && i < n; ++i) {
    char ca = a[i];
    char cb = lit[i];
    if (ca >= 'A' && ca <= 'Z')
      ca = (char)(ca - 'A' + 'a');
    if (cb >= 'A' && cb <= 'Z')
      cb = (char)(cb - 'A' + 'a');
    if (ca != cb)
      return 0;
  }
  return a[i] == '\0' && lit[i] == '\0';
}

static int apply_kv(ArCore* core, const char* key, const char* val) {
  if (key_eq(key, "maxmemory")) {
    ar_core_set_maxmemory(core, strtoull(val, NULL, 10));
    return 1;
  }
  if (key_eq(key, "requirepass")) {
    return ar_core_set_requirepass(core, val) ? 1 : 0;
  }
  if (key_eq(key, "protected-mode")) {
    int on = !(strcmp(val, "no") == 0 || strcmp(val, "0") == 0 ||
               strcmp(val, "false") == 0 || strcmp(val, "off") == 0);
    ar_core_set_protected_mode(core, on);
    return 1;
  }
  if (key_eq(key, "evict-samples") || key_eq(key, "samples")) {
    return ar_core_set_evict_samples(core, atoi(val)) ? 1 : 0;
  }
  if (key_eq(key, "maxclients")) {
    return ar_core_set_maxclients(core, atoi(val)) ? 1 : 0;
  }
  if (key_eq(key, "timeout")) {
    return ar_core_set_timeout(core, atoi(val)) ? 1 : 0;
  }
  if (key_eq(key, "tcp-backlog")) {
    return ar_core_set_tcp_backlog(core, atoi(val)) ? 1 : 0;
  }
  if (key_eq(key, "slowlog-log-slower-than")) {
    return ar_core_set_slowlog_slower_than(core, atoi(val)) ? 1 : 0;
  }
  if (key_eq(key, "dir")) {
    return ar_core_set_rdb_dir(core, val) ? 1 : 0;
  }
  if (key_eq(key, "dbfilename")) {
    return ar_core_set_rdb_filename(core, val) ? 1 : 0;
  }
  if (key_eq(key, "bind")) {
    return ar_core_set_bind(core, val) ? 1 : 0;
  }
  if (key_eq(key, "shadow-policy")) {
    return ar_core_set_shadow_policy(core, val) ? 1 : 0;
  }
  if (key_eq(key, "shadow-sample-pct")) {
    return ar_core_set_shadow_sample_pct(core, atoi(val)) ? 1 : 0;
  }
  if (key_eq(key, "hot-soft-cap-pct")) {
    return ar_core_set_hot_soft_cap_pct(core, atoi(val)) ? 1 : 0;
  }
  if (key_eq(key, "hot-soft-cap-min")) {
    return ar_core_set_hot_soft_cap_min(core, atoi(val)) ? 1 : 0;
  }
  if (key_eq(key, "hot-promote-on-get")) {
    int on = !(strcmp(val, "no") == 0 || strcmp(val, "0") == 0 ||
               strcmp(val, "false") == 0 || strcmp(val, "off") == 0);
    return ar_core_set_hot_promote_on_get(core, on) ? 1 : 0;
  }
  /* Unknown keys: ignore (forward-compat). */
  return 1;
}

int ar_config_load(ArCore* core) {
  if (!core || !core->config_path[0])
    return 1;
  FILE* f = fopen(core->config_path, "r");
  if (!f) {
    if (errno == ENOENT)
      return 1; /* missing = empty defaults */
    fprintf(stderr, "ar_config: cannot open %s: %s\n", core->config_path,
            strerror(errno));
    return 0;
  }
  char line[512];
  int lineno = 0;
  while (fgets(line, sizeof(line), f)) {
    lineno++;
    /* strip CR/LF */
    size_t n = strlen(line);
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r'))
      line[--n] = '\0';
    /* trim leading space */
    char* p = line;
    while (*p && isspace((unsigned char)*p))
      p++;
    if (*p == '\0' || *p == '#')
      continue;
    char* key = p;
    while (*p && !isspace((unsigned char)*p))
      p++;
    if (*p) {
      *p++ = '\0';
      while (*p && isspace((unsigned char)*p))
        p++;
    }
    const char* val = p; /* may be empty */
    if (!apply_kv(core, key, val)) {
      fprintf(stderr, "ar_config: %s:%d bad %s\n", core->config_path, lineno,
              key);
      fclose(f);
      return 0;
    }
  }
  fclose(f);
  return 1;
}

int ar_config_rewrite(ArCore* core) {
  if (!core || !core->config_path[0])
    return 0;
  char tmp[560];
  int tn = snprintf(tmp, sizeof(tmp), "%s.tmp.%d", core->config_path,
                    (int)getpid());
  if (tn < 0 || (size_t)tn >= sizeof(tmp))
    return 0;
  FILE* f = fopen(tmp, "w");
  if (!f)
    return 0;
  fprintf(f, "# aura-redis.conf — written by CONFIG SET / CONFIG REWRITE\n");
  fprintf(f, "# Path override: --config / AURA_REDIS_CONFIG\n");
  fprintf(f, "maxmemory %llu\n",
          (unsigned long long)ar_core_maxmemory(core));
  fprintf(f, "requirepass %s\n",
          (core->requirepass && core->requirepass[0]) ? core->requirepass : "");
  fprintf(f, "protected-mode %s\n", core->protected_mode ? "yes" : "no");
  fprintf(f, "evict-samples %d\n", ar_core_evict_samples(core));
  fprintf(f, "maxclients %d\n", core->maxclients);
  fprintf(f, "timeout %d\n", core->timeout_sec);
  fprintf(f, "tcp-backlog %d\n", core->tcp_backlog);
  fprintf(f, "slowlog-log-slower-than %d\n", core->slowlog_slower_than_us);
  fprintf(f, "dir %s\n", ar_core_rdb_dir(core));
  fprintf(f, "dbfilename %s\n", ar_core_rdb_filename(core));
  fprintf(f, "bind %s\n",
          core->bind_addr[0] ? core->bind_addr : "127.0.0.1");
  fprintf(f, "shadow-policy %s\n", ar_core_shadow_policy(core));
  fprintf(f, "shadow-sample-pct %d\n", ar_core_shadow_sample_pct(core));
  fprintf(f, "hot-soft-cap-pct %d\n", ar_core_hot_soft_cap_pct(core));
  fprintf(f, "hot-soft-cap-min %d\n", ar_core_hot_soft_cap_min(core));
  fprintf(f, "hot-promote-on-get %s\n",
          ar_core_hot_promote_on_get(core) ? "yes" : "no");
  if (fflush(f) != 0 || fsync(fileno(f)) != 0) {
    fclose(f);
    unlink(tmp);
    return 0;
  }
  fclose(f);
  if (rename(tmp, core->config_path) != 0) {
    unlink(tmp);
    return 0;
  }
  return 1;
}
