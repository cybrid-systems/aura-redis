# aura-redis perf log

Recorded: 2026-09-22 21:22:30 CST (Asia/Shanghai)  
**SHA:** `d1d1db2` (docs refresh after e2e at this tip)

Frozen memtier: **1c×1t**, SET:GET=**1:10**, 32B, key 1..10000 **R:R**, `-n 20000`.  
Data plane: host `aura_redis_server` (Release). Redis: `redis:7-alpine` (Docker host net).

| Engine | pipeline | Totals ops/s | vs Redis |
|--------|----------|--------------|----------|
| redis:7-alpine | 1 | 42056.57 | 1.00 |
| aura-redis C EVICT=lru | 1 | 45282.37 | **1.077** |
| aura-redis C EVICT=lfu | 1 | 45527.39 | **1.083** |
| aura-redis C adaptive (Py) | 1 | 48245.09 | **1.147** |
| redis:7-alpine | 16 | 377287.30 | 1.00 |
| aura-redis C EVICT=lru | 16 | 511312.80 | **1.355** |
| aura-redis C EVICT=lfu | 16 | 508595.26 | **1.348** |
| aura-redis C adaptive (Py) | 16 | 477760.26 | **1.266** |

**Gates:** p=1 ratio ≥ **1.077** (lru) — cleared (≥0.80 iter4). Full matrix + hit-rate: **`docs/perf-eval.md`**.

Hit-rate headline (same SHA): LRU **0%** vs adaptive **100%** on `hot_protect` / demo-mvp Phase A; `zipf_hotkey` LRU **0%** vs adaptive **97.6%**.

Reproduce: `./scripts/bench-e2e.sh` or `./scripts/memtier-cmp.sh`.


### Dynamic eviction + zipf (2026-09-22 Asia/Shanghai)

Harness: `python3 scripts/bench_dynamic_evict.py --workloads hot_protect,ws_shift,oscillate,zipf_hotkey`.

| workload | lru | lfu | adaptive |
|----------|-----|-----|----------|
| hot_protect | 0.0% | 100% | **100%** |
| ws_shift | 100% | 33.4% | **100%** |
| oscillate | 97.6% | 34.8% | **100%** |
| zipf_hotkey | 0.0% | 100% | **97.6%** |

Demo: `./scripts/demo-mvp.sh` Phase A LRU 0% → adaptive 100%. Industry pack: `python3 scripts/bench_industry.py` PASS.

### Iteration 4 levers (headroom / if regresses)

Already applied in v1 C path: epoll ET, TCP_NODELAY, pipelined parse-many-per-read, buffered writes, binary-safe dict with rehash, static `+OK`/`+PONG`/`$-1`, zero-copy GET reply from entry pointer.

Next if needed: `writev` multi-reply, arena/slab values, dict load-factor tuning, io_uring, multi-thread shard.

### Iteration 8 — layout (2026-09-22 Asia/Shanghai)

Added `flat` / `hot_cold` layouts with quiescent `ar_core_set_layout` migrate. Not a memtier gate change; correctness demo: `tests/test_layout.py` (migrate under concurrent SET/GET; promote/demote counters). Soft hot cap ≈ nkeys/4.

### Iteration 7 stretch — plugin live-reload (2026-09-22 Asia/Shanghai)

`PLUGIN` / `ar_core_load_evict_plugin` swaps eviction `.so` under concurrent SET/GET without closing the listen fd. Evidence: `tests/test_plugin_reload.py` (control socket survives; new clients connect; worker errors=0; `reloads>=4`). Not a memtier gate change.

