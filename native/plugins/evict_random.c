/* Sample Iteration 7 eviction plugin — random victim under maxmemory.
 * Export: ar_plugin_evict_ops
 * Build: native/build/plugins/libar_evict_random.so
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
static int one(ArCore* db) { return ar_core_evict_random_one(db); }

static const ArEvictOps kOps = {
    .on_get = on_get,
    .on_set = on_set,
    .should_evict = should,
    .evict_one = one,
    .name = "random",
};

const ArEvictOps* ar_plugin_evict_ops(void) { return &kOps; }
