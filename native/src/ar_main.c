/* Optional standalone entry for smoke/bench without Aura (host).
 * Production story remains Aura control plane via server_ffi.aura. */
#include "ar_core.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char** argv) {
  int port = 6379;
  const char* evict = "noop";
  const char* plugin = NULL;
  const char* layout = NULL;
  const char* requirepass = NULL;
  const char* bind_addr = NULL;
  int protected_mode = -1; /* -1 = default (on) */
  int maxclients = -1;
  int timeout_sec = -1;
  int tcp_backlog = -1;
  uint64_t maxmem = 0;
  const char* rdb_dir = NULL;
  const char* rdb_filename = NULL;
  const char* config_path = NULL;
  int config_path_set = 0; /* 1 if --config / env provided (incl empty=disable) */
  int tls_port = 0;
  int tls_yes = 0;
  const char* tls_cert = NULL;
  const char* tls_key = NULL;
  const char* tls_ca = NULL;
  for (int i = 1; i < argc; ++i) {
    if (strcmp(argv[i], "--port") == 0 && i + 1 < argc)
      port = atoi(argv[++i]);
    else if (strcmp(argv[i], "--evict") == 0 && i + 1 < argc)
      evict = argv[++i];
    else if (strcmp(argv[i], "--maxmemory") == 0 && i + 1 < argc)
      maxmem = strtoull(argv[++i], NULL, 10);
    else if (strcmp(argv[i], "--plugin") == 0 && i + 1 < argc)
      plugin = argv[++i];
    else if (strcmp(argv[i], "--layout") == 0 && i + 1 < argc)
      layout = argv[++i];
    else if (strcmp(argv[i], "--requirepass") == 0 && i + 1 < argc)
      requirepass = argv[++i];
    else if (strcmp(argv[i], "--bind") == 0 && i + 1 < argc)
      bind_addr = argv[++i];
    else if (strcmp(argv[i], "--protected-mode") == 0 && i + 1 < argc) {
      const char* v = argv[++i];
      if (strcmp(v, "no") == 0 || strcmp(v, "0") == 0 || strcmp(v, "false") == 0)
        protected_mode = 0;
      else
        protected_mode = 1;
    } else if (strcmp(argv[i], "--maxclients") == 0 && i + 1 < argc)
      maxclients = atoi(argv[++i]);
    else if (strcmp(argv[i], "--timeout") == 0 && i + 1 < argc)
      timeout_sec = atoi(argv[++i]);
    else if (strcmp(argv[i], "--tcp-backlog") == 0 && i + 1 < argc)
      tcp_backlog = atoi(argv[++i]);
    else if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
      config_path = argv[++i];
      config_path_set = 1;
    } else if (strcmp(argv[i], "--dir") == 0 && i + 1 < argc)
      rdb_dir = argv[++i];
    else if (strcmp(argv[i], "--dbfilename") == 0 && i + 1 < argc)
      rdb_filename = argv[++i];
    else if (strcmp(argv[i], "--tls-port") == 0 && i + 1 < argc)
      tls_port = atoi(argv[++i]);
    else if (strcmp(argv[i], "--tls") == 0 && i + 1 < argc) {
      const char* v = argv[++i];
      if (strcmp(v, "yes") == 0 || strcmp(v, "1") == 0 || strcmp(v, "on") == 0)
        tls_yes = 1;
      else
        tls_yes = 0;
    } else if (strcmp(argv[i], "--tls-cert-file") == 0 && i + 1 < argc)
      tls_cert = argv[++i];
    else if (strcmp(argv[i], "--tls-key-file") == 0 && i + 1 < argc)
      tls_key = argv[++i];
    else if (strcmp(argv[i], "--tls-ca-file") == 0 && i + 1 < argc)
      tls_ca = argv[++i];
  }
  /* Env fallbacks (Iteration 8 layout + existing eviction) */
  if (!layout || !layout[0]) {
    const char* el = getenv("AURA_REDIS_LAYOUT");
    if (el && el[0])
      layout = el;
  }
  {
    const char* ee = getenv("AURA_REDIS_EVICT");
    if (ee && ee[0] && strcmp(evict, "noop") == 0)
      evict = ee;
  }
  {
    const char* ep = getenv("AURA_REDIS_EVICT_SO");
    if (ep && ep[0] && !plugin)
      plugin = ep;
  }
  {
    const char* em = getenv("AURA_REDIS_MAXMEMORY");
    if (em && em[0] && maxmem == 0)
      maxmem = strtoull(em, NULL, 10);
  }
  if (!requirepass || !requirepass[0]) {
    const char* rp = getenv("AURA_REDIS_REQUIREPASS");
    if (rp && rp[0])
      requirepass = rp;
  }
  if (!bind_addr || !bind_addr[0]) {
    const char* ba = getenv("AURA_REDIS_BIND");
    if (ba && ba[0])
      bind_addr = ba;
  }
  if (protected_mode < 0) {
    const char* pm = getenv("AURA_REDIS_PROTECTED_MODE");
    if (pm && pm[0]) {
      if (strcmp(pm, "no") == 0 || strcmp(pm, "0") == 0 ||
          strcmp(pm, "false") == 0 || strcmp(pm, "off") == 0)
        protected_mode = 0;
      else
        protected_mode = 1;
    } else {
      protected_mode = 1;
    }
  }
  if (maxclients < 0) {
    const char* e = getenv("AURA_REDIS_MAXCLIENTS");
    if (e && e[0]) maxclients = atoi(e);
  }
  if (timeout_sec < 0) {
    const char* e = getenv("AURA_REDIS_TIMEOUT");
    if (e && e[0]) timeout_sec = atoi(e);
  }
  if (tcp_backlog < 0) {
    const char* e = getenv("AURA_REDIS_TCP_BACKLOG");
    if (e && e[0]) tcp_backlog = atoi(e);
  }
  if (!config_path_set) {
    const char* e = getenv("AURA_REDIS_CONFIG");
    if (e) { /* present — empty disables default aura-redis.conf */
      config_path = e;
      config_path_set = 1;
    }
  }
  if (!rdb_dir || !rdb_dir[0]) {
    const char* e = getenv("AURA_REDIS_DIR");
    if (e && e[0]) rdb_dir = e;
  }
  if (!rdb_filename || !rdb_filename[0]) {
    const char* e = getenv("AURA_REDIS_DBFILENAME");
    if (e && e[0]) rdb_filename = e;
  }
  if (tls_port <= 0) {
    const char* e = getenv("AURA_REDIS_TLS_PORT");
    if (e && e[0]) tls_port = atoi(e);
  }
  if (!tls_yes) {
    const char* e = getenv("AURA_REDIS_TLS");
    if (e && (strcmp(e, "yes") == 0 || strcmp(e, "1") == 0 || strcmp(e, "on") == 0))
      tls_yes = 1;
  }
  if (!tls_cert || !tls_cert[0]) {
    const char* e = getenv("AURA_REDIS_TLS_CERT_FILE");
    if (e && e[0]) tls_cert = e;
  }
  if (!tls_key || !tls_key[0]) {
    const char* e = getenv("AURA_REDIS_TLS_KEY_FILE");
    if (e && e[0]) tls_key = e;
  }
  if (!tls_ca || !tls_ca[0]) {
    const char* e = getenv("AURA_REDIS_TLS_CA_FILE");
    if (e && e[0]) tls_ca = e;
  }
  if (tls_yes && tls_port <= 0)
    tls_port = 6380; /* Redis-ish default when --tls yes without --tls-port */

  ArCore* core = ar_core_create();
  if (!core) {
    fprintf(stderr, "ar_main: create failed\n");
    return 1;
  }
  /* Config file: defaults → file → CLI/env (CLI wins). Empty path disables. */
  if (config_path_set) {
    if (!ar_core_set_config_file(core, config_path)) {
      fprintf(stderr, "ar_main: bad --config path\n");
      ar_core_destroy(core);
      return 1;
    }
  }
  if (!ar_config_load(core)) {
    fprintf(stderr, "ar_main: config load failed (%s)\n",
            ar_core_config_file(core));
    ar_core_destroy(core);
    return 1;
  }
  if (bind_addr && bind_addr[0]) {
    if (!ar_core_set_bind(core, bind_addr)) {
      fprintf(stderr, "ar_main: bad --bind %s\n", bind_addr);
      ar_core_destroy(core);
      return 1;
    }
  }
  ar_core_set_protected_mode(core, protected_mode);
  if (requirepass && requirepass[0])
    ar_core_set_requirepass(core, requirepass);
  if (maxclients > 0)
    ar_core_set_maxclients(core, maxclients);
  if (timeout_sec >= 0)
    ar_core_set_timeout(core, timeout_sec);
  if (tcp_backlog > 0)
    ar_core_set_tcp_backlog(core, tcp_backlog);
  if (layout && layout[0]) {
    if (!ar_core_set_layout(core, layout)) {
      fprintf(stderr, "ar_main: bad layout %s (want flat|hot_cold)\n", layout);
      ar_core_destroy(core);
      return 1;
    }
    fprintf(stderr, "ar_main: layout=%s\n", ar_core_layout_name(core));
  }
  if (plugin && plugin[0])
    ar_core_load_evict_plugin(core, plugin);
  else
    ar_core_set_evict_by_name(core, evict);
  if (maxmem)
    ar_core_set_maxmemory(core, maxmem);
  if (rdb_dir && rdb_dir[0]) {
    if (!ar_core_set_rdb_dir(core, rdb_dir)) {
      fprintf(stderr, "ar_main: bad --dir %s\n", rdb_dir);
      ar_core_destroy(core);
      return 1;
    }
  }
  if (rdb_filename && rdb_filename[0]) {
    if (!ar_core_set_rdb_filename(core, rdb_filename)) {
      fprintf(stderr, "ar_main: bad --dbfilename %s\n", rdb_filename);
      ar_core_destroy(core);
      return 1;
    }
  }
  if (!ar_rdb_load(core)) {
    fprintf(stderr, "ar_main: aura-rdb load failed (dir=%s file=%s)\n",
            ar_core_rdb_dir(core), ar_core_rdb_filename(core));
    ar_core_destroy(core);
    return 1;
  }
  ar_core_install_signal_handlers(core);
  if (tls_cert && tls_cert[0]) {
    if (!ar_core_set_tls_cert_file(core, tls_cert)) {
      fprintf(stderr, "ar_main: bad --tls-cert-file\n");
      ar_core_destroy(core);
      return 1;
    }
  }
  if (tls_key && tls_key[0]) {
    if (!ar_core_set_tls_key_file(core, tls_key)) {
      fprintf(stderr, "ar_main: bad --tls-key-file\n");
      ar_core_destroy(core);
      return 1;
    }
  }
  if (tls_ca && tls_ca[0]) {
    if (!ar_core_set_tls_ca_file(core, tls_ca)) {
      fprintf(stderr, "ar_main: bad --tls-ca-file\n");
      ar_core_destroy(core);
      return 1;
    }
  }
  if (ar_core_listen(core, port) != 1) {
    fprintf(stderr, "ar_main: listen %d failed\n", port);
    ar_core_destroy(core);
    return 1;
  }
  if (tls_port > 0 || tls_yes) {
    if (!tls_cert || !tls_cert[0] || !tls_key || !tls_key[0]) {
      fprintf(stderr,
              "ar_main: TLS requires --tls-cert-file and --tls-key-file "
              "(and OpenSSL build)\n");
      ar_core_destroy(core);
      return 1;
    }
    if (ar_core_listen_tls(core, tls_port) != 1) {
      fprintf(stderr, "ar_main: tls listen %d failed\n", tls_port);
      ar_core_destroy(core);
      return 1;
    }
  }
  int rc = ar_core_serve_forever(core);
  ar_core_destroy(core);
  return rc == 0 ? 0 : 1;
}
