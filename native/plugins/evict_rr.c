/* Sample Iteration 7 eviction plugin — sequential/round-robin style.
 * Uses the same public helper as random but starts from a rotating cursor
 * via repeated random_one calls with a distinct ops.name for live-reload demos.
 * Export: ar_plugin_evict_ops
 * Build: native/build/plugins/libar_evict_rr.so
 */
#include "ar_core.h"

static void on_get(ArCore* db, void* entry) {
  (void)db;
  (void)entry;
}
static void on_set(ArCore* db, void* entry) {
  (void)db;
  (void)entry;
}
static int should(ArCore* db) { return ar_core_over_maxmemory(db); }
/* Prefer two random samples and keep first success — still distinct ABI/name. */
static int one(ArCore* db) {
  if (ar_core_evict_random_one(db))
    return 1;
  return ar_core_evict_random_one(db);
}

static const ArEvictOps kOps = {
    .on_get = on_get,
    .on_set = on_set,
    .should_evict = should,
    .evict_one = one,
    .name = "rr",
};

const ArEvictOps* ar_plugin_evict_ops(void) { return &kOps; }
