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
  uint64_t maxmem = 0;
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

  ArCore* core = ar_core_create();
  if (!core) {
    fprintf(stderr, "ar_main: create failed\n");
    return 1;
  }
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
  if (ar_core_listen(core, port) != 1) {
    fprintf(stderr, "ar_main: listen %d failed\n", port);
    ar_core_destroy(core);
    return 1;
  }
  int rc = ar_core_serve_forever(core);
  ar_core_destroy(core);
  return rc == 0 ? 0 : 1;
}
