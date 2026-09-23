# aura-redis perf log

## 2026-09-23 09:59:12 CST — tip `ce55c70` (bench-vs-redis)

Frozen memtier: **1c×1t**, SET:GET=**1:10**, 32B, key 1..10000 **R:R**.

| Engine | pipeline | Totals ops/s | vs Redis |
|--------|----------|--------------|----------|
| redis:7-alpine | 1 | 40412.37 | 1.00 |
| aura-redis C EVICT=lru | 1 | 42546.85 | **1.053** |
| aura-redis C EVICT=lfu | 1 | 45521.07 | **1.126** |
| aura-redis C EVICT=slru | 1 | 46340.49 | **1.147** |
| redis:7-alpine | 16 | 409165.30 | 1.00 |
| aura-redis C EVICT=lru | 16 | 475963.83 | **1.163** |
| aura-redis C EVICT=lfu | 16 | 303817.47 | **0.743** |
| aura-redis C EVICT=slru | 16 | 422101.22 | **1.032** |

Full dual scoreboard: [`redis-compare.md`](redis-compare.md).

---

## 2026-09-23 09:57:16 CST — tip `abfd3f8` (bench-vs-redis dual scoreboard)

Frozen memtier: **1c×1t**, SET:GET=**1:10**, 32B, key 1..10000 **R:R**, `-n 20000`.  
Redis: `redis:7-alpine`. DENY_PLUGIN=1.

| Engine | pipeline | Totals ops/s | vs Redis |
|--------|----------|--------------|----------|
| redis:7-alpine | 1 | 42739.15 | 1.00 |
| aura-redis C EVICT=lru | 1 | 45527.60 | **1.065** |
| aura-redis C EVICT=lfu | 1 | 46623.20 | **1.091** |
| aura-redis C EVICT=slru | 1 | 45543.04 | **1.066** |
| redis:7-alpine | 16 | 398446.06 | 1.00 |
| aura-redis C EVICT=lru | 16 | 444099.03 | **1.115** |
| aura-redis C EVICT=lfu | 16 | 430061.28 | **1.079** |
| aura-redis C EVICT=slru | 16 | 361925.44 | **0.908** |

**Expanded (lru vs redis):** n=100k p=1 **1.042×**; n=100k p=16 **1.162×**; p=8 **0.950×**; 4c×4t **1.055×**.

Hit-quality headline (Aura `policy_agent`): `phase_marathon` adaptive **100%** / 1956 vs LRU **81.8%** / LFU **36.1%** (+18.2pp / +63.9pp).  
Redis side-by-side (fixed policy): see [`redis-compare.md`](redis-compare.md).

Reproduce: `./scripts/bench-vs-redis.sh` · `python3 scripts/bench_regret.py phase_marathon`.

---

## 2026-09-23 06:55:00 CST (Asia/Shanghai) — tip `b64fd21`

**SHA:** `b64fd2171257eb50cae7fc248b69f196f4b0d7d2` (post P2 TLS)  
**Host:** Linux x86_64, 8 CPUs, Intel Xeon (KVM); Release `aura_redis_server` (OpenSSL TLS linked).

Frozen memtier: **1c×1t**, SET:GET=**1:10**, 32B, key 1..10000 **R:R**, `-n 20000`.  
Redis: `redis:7-alpine` (Docker host net). Adaptive throughput row = Py controller (not Aura story).

| Engine | pipeline | Totals ops/s | vs Redis |
|--------|----------|--------------|----------|
| redis:7-alpine | 1 | 42345.61 | 1.00 |
| aura-redis C EVICT=lru | 1 | 47773.97 | **1.128** |
| aura-redis C EVICT=lfu | 1 | 45254.19 | **1.069** |
| aura-redis C adaptive (Py) | 1 | 46341.78 | **1.094** |
| redis:7-alpine | 16 | 413787.40 | 1.00 |
| aura-redis C EVICT=lru | 16 | 460553.59 | **1.113** |
| aura-redis C EVICT=lfu | 16 | 420203.38 | **1.015** |
| aura-redis C adaptive (Py) | 16 | 456225.19 | **1.103** |

**Expanded (lru vs redis):**

| Variant | redis ops/s | aura-lru ops/s | vs Redis |
|---------|-------------|----------------|----------|
| n=100000 p=1 | 41459.49 | 44615.93 | **1.076** |
| n=100000 p=16 | 391785.05 | 447623.57 | **1.142** |
| n=20000 p=8 | 256383.96 | 287079.97 | **1.120** |
| n=20000 p=1 4c×4t | 151140.16 | 168461.66 | **1.115** |

**Gates:** p=1 lru ratio ≥ **1.077** — cleared (**1.128**). Full matrix + hit-rate: **`docs/perf-eval.md`**.

Hit-quality headline (Aura `policy_agent`, same SHA): `phase_marathon` adaptive **100%** / 1956 vs LRU **81.8%** / LFU **42.9%** (+18.2pp / +57.1pp; regret=0). Industry pack PASS. Mutation: `poison_heal` +100pp; `ttl_wave`/`flash_churn`/`prefix_mix` PASS; `mutation_gain`/`evolve_gain` stretch asserts FAIL (Δ=0 vs frozen).

Reproduce: `./scripts/build-native.sh` then memtier matrix + `python3 scripts/bench_regret.py` / `bench_industry.py`.

---

## Prior: 2026-09-22 21:22:30 CST — tip `d1d1db2` / eval `9a968aa`

Recorded: 2026-09-22 21:22:30 CST (Asia/Shanghai)  
**SHA:** `d1d1db2` (docs refresh after e2e at this tip); full eval table also cited at `9a968aa`.

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

Hit-rate headline (same era): LRU **0%** vs adaptive **100%** on `hot_protect` / demo-mvp Phase A; `zipf_hotkey` LRU **0%** vs adaptive **97.6%** (later pin-fix → 100%).

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