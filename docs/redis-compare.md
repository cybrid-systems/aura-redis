# aura-redis vs Redis — comprehensive comparison

**Date:** 2026-09-23 09:57:16 CST (Asia/Shanghai)  
**Tip:** `abfd3f8`  
**Host:** Linux x86_64, 8 CPUs (KVM)  
**Redis baseline:** `redis:7-alpine` (Docker `--network host`)  
**Aura data plane:** `native/build/aura_redis_server` (`AURA_REDIS_DENY_PLUGIN=1`)  
**Loadgen throughput:** `redislabs/memtier_benchmark`  
**Loadgen hit-quality:** `scripts/bench_hit_vs_redis.py` + authoritative `scripts/bench_regret.py`

Two independent scoreboards — **never collapse** ops/s and regret into one number:

1. **Throughput (ops/s)** — memtier, no `maxmemory` pressure
2. **Hit quality (useful-GET hit%)** — small maxmemory; Redis = fixed `allkeys-lru` / `allkeys-lfu` only

---

## Scoreboard 1 — Throughput (ops/s)

Frozen matrix: **1c×1t**, SET:GET=**1:10**, 32B, key 1..10000 **R:R**, `-n 20000`.

| Engine | pipeline | Totals ops/s | vs Redis |
|--------|----------|--------------|----------|
| redis:7-alpine | 1 | 42739.15 | 1.00 |
| aura EVICT=lru | 1 | 45527.60 | **1.065** |
| aura EVICT=lfu | 1 | 46623.20 | **1.091** |
| aura EVICT=slru | 1 | 45543.04 | **1.066** |
| redis:7-alpine | 16 | 398446.06 | 1.00 |
| aura EVICT=lru | 16 | 444099.03 | **1.115** |
| aura EVICT=lfu | 16 | 430061.28 | **1.079** |
| aura EVICT=slru | 16 | 361925.44 | **0.908** |

**Headline p=1:** aura-lru / redis = **1.065×**; p=16 = **1.115×**.

### Expanded matrix

| Variant | redis ops/s | aura-lru | aura-lfu | aura-slru | lru÷redis |
|---------|-------------|----------|----------|-----------|-----------|
| n=100000 p=1 | 42072.58 | 43837.13 | 43977.46 | 45438.00 | **1.042** |
| n=100000 p=16 | 362669.68 | 421393.29 | 411702.22 | 391447.65 | **1.162** |
| n=20000 p=8 | 247555.39 | 235067.35 | 265869.06 | 274134.08 | **0.950** |
| n=20000 p=1 4c×4t | 158258.84 | 166937.68 | 168580.49 | 163197.11 | **1.055** |

Throughput is **not** the adaptive/mutation story — cite hit-quality for that.

---

## Scoreboard 2 — Hit quality

### 2A) Authoritative Aura marathon (`bench_regret.py phase_marathon`)

Same harness as [`perf-eval.md`](perf-eval.md): `maxmemory=120000`, Aura `policy_agent` adaptive.

| Engine | Policy | Cum useful-GET hit% | Useful | Notes |
|--------|--------|---------------------|--------|-------|
| aura | **adaptive** | **100.0%** | 1956 | near-oracle; +18.2pp vs LRU, +63.9pp vs LFU |
| aura | lru | 81.8% | 1600 | loses zipf + hot_protect |
| aura | lfu | 36.1% | 706 | loses ws_shift |

**Cite:** adaptive **100%** / 1956 useful vs LRU **81.8%** / LFU **36.1%**.

### 2B) Side-by-side vs Redis fixed policy (`bench_hit_vs_redis.py`)

Redis has **no** live EVICT/PIN/LAYOUT. Baseline = `allkeys-lru` / `allkeys-lfu` with headroom ≈150KiB above Redis idle `used_memory` (~1MB). Aura adaptive on this *short* harness often tracks LRU (agent needs longer phases) — use **2A** for adaptive cites; use **2B** for fixed-kernel vs Redis tables.

| Workload | Engine | Policy | Hit% | Useful GET hits | Misses | Notes |
|----------|--------|--------|------|-----------------|--------|-------|
| hot_protect | aura | adaptive | 0.0 | 0 | 16 | — |
| hot_protect | aura | lfu | 100.0 | 16 | 0 | — |
| hot_protect | aura | lru | 0.0 | 0 | 16 | — |
| hot_protect | aura | slru | 100.0 | 16 | 0 | — |
| hot_protect | redis | lfu | 100.0 | 16 | 0 | maxmemory-policy=allkeys-lfu |
| hot_protect | redis | lru | 31.2 | 5 | 11 | maxmemory-policy=allkeys-lru |
| phase_marathon | aura | adaptive | 40.8 | 31 | 45 | — |
| phase_marathon | aura | lfu | 100.0 | 76 | 0 | — |
| phase_marathon | aura | lru | 40.8 | 31 | 45 | — |
| phase_marathon | aura | slru | 100.0 | 76 | 0 | — |
| phase_marathon | redis | lfu | 100.0 | 76 | 0 | maxmemory-policy=allkeys-lfu |
| phase_marathon | redis | lru | 72.4 | 55 | 21 | maxmemory-policy=allkeys-lru |
| ws_shift | aura | adaptive | 77.5 | 31 | 9 | — |
| ws_shift | aura | lfu | 100.0 | 40 | 0 | — |
| ws_shift | aura | lru | 77.5 | 31 | 9 | — |
| ws_shift | aura | slru | 100.0 | 40 | 0 | — |
| ws_shift | redis | lfu | 100.0 | 40 | 0 | maxmemory-policy=allkeys-lfu |
| ws_shift | redis | lru | 70.0 | 28 | 12 | maxmemory-policy=allkeys-lru |
| zipf | aura | adaptive | 0.0 | 0 | 20 | — |
| zipf | aura | lfu | 100.0 | 20 | 0 | — |
| zipf | aura | lru | 0.0 | 0 | 20 | — |
| zipf | aura | slru | 100.0 | 20 | 0 | — |
| zipf | redis | lfu | 80.0 | 16 | 4 | maxmemory-policy=allkeys-lfu |
| zipf | redis | lru | 20.0 | 4 | 16 | maxmemory-policy=allkeys-lru |

Redis = fixed allkeys-lru/lfu only; Aura adaptive is Aura-only.

**Redis hot_protect:** allkeys-lru **31.2%** vs allkeys-lfu **100%** — same qualitative LRU-lose as aura LRU **0%** / LFU **100%**. Aura `slru`/`tinylfu` match LFU retention.

---

## How to re-run

```bash
export AURA_REDIS_DENY_PLUGIN=1
./scripts/build-native.sh
./scripts/bench-vs-redis.sh                 # dual scoreboard → docs/redis-compare.md
AURA_REDIS_REDIS_HEADROOM=150000 \
  python3 scripts/bench_hit_vs_redis.py     # hit-quality only
python3 scripts/bench_regret.py phase_marathon
./scripts/memtier-cmp.sh
./scripts/ci-prod.sh                        # includes ci-strong
./scripts/ci-bench.sh                       # regret packs (not in ci-prod wall)
```

CI: unit/integration always via `ci-prod` → `ci-strong`. Long regret stays out of the wall-time gate.

