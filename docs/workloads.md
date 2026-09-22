# Dynamic eviction workloads

These workloads are designed so **fixed LRU is not always optimal**. Aura-adaptive
eviction (`lru` ↔ `lfu` via RESP `EVICT`, driven by Aura `policy_agent` or a
Python mirror of `choose_normal`) tracks the better kernel across phases.

Data plane: host `aura_redis_server` (C). Control plane: Aura policy mutation under
sandbox (`AURA_REDIS_DENY_PLUGIN=1`); `PLUGIN` is not the feature under test.

## Workloads

| Name | Favors | What happens |
|------|--------|----------------|
| `hot_protect` | **LFU** | Adaptive flips to LFU (write prelude) → insert+boost a small hot set → flood unique cold keys past `maxmemory` → GET the hot set. LRU has already aged the hot keys out; LFU keeps high-frequency keys. |
| `ws_shift` | **LRU** | Boost set A, then stop using it and drive set B under pressure. LFU clings to A and thrashes B; LRU follows the new working set. |
| `oscillate` | **adaptive** | `hot_protect` → `FLUSHDB` → read-heavy bridge (→ LRU) → `ws_shift` on one server lifetime. Adaptive should land near best-of `{lru,lfu}` on each phase. |

Policy rules (same as `src/redis/policy/choose_normal.aura`):

- window ops ≥ 80
- write-heavy (`sets > gets*2`) → `lfu`
- read-heavy (`gets > sets*5`) and hit% ≥ 60 → `lru`

## Reproduce

```bash
./scripts/build-native.sh
python3 scripts/bench_dynamic_evict.py
# optional Aura-native controller (Docker ghcr.io/cybrid-systems/dev):
python3 scripts/bench_dynamic_evict.py --aura-agent --workloads hot_protect,ws_shift
```

Defaults: `--maxmemory 120000`, port `26730`, Python adaptive controller.

## Expected shape (illustrative)

On a typical host run (see `docs/perf-log.md` for a dated capture):

| workload | LRU hit% | LFU hit% | adaptive hit% |
|----------|----------|----------|---------------|
| hot_protect | ~0% | ~100% | ~100% (swap lru→lfu) |
| ws_shift | ~100% | ~35% | ~100% (stays/bridges to lru) |
| oscillate | ~98% overall (0% then 100%) | ~36% | **~100%** both phases |

Caveats: approximate sampling eviction (16 samples); LFU freq only increments under the LFU kernel — the harness boosts the hot set **after** adaptive flips to LFU. Absolute ops/s is not the goal; hit ratio / useful target GETs is.
