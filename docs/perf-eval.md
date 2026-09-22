# aura-redis end-to-end performance evaluation

**Date:** 2026-09-22 20:49:45 CST (Asia/Shanghai)  
**SHA at measurement:** `e060051` (docs/scripts landed in follow-up commit on `main`)  
**Host:** Linux x86_64, 8 CPUs, `Intel(R) Xeon(R) Processor`  
**Data plane:** host `native/build/aura_redis_server` (Release)  
**Redis baseline:** `redis:7-alpine` (Docker, `--network host`)  
**Loadgen:** `redislabs/memtier_benchmark` (Docker, host network)

Two independent dimensions — **do not** collapse into one score:

1. **Throughput** (memtier) — no `maxmemory` pressure; raw RESP / data-plane speed.
2. **Hit quality** (dynamic workloads) — small `maxmemory=120000`; eviction policy fitness.

Caveats: approximate LFU sampling (16 samples); adaptive defaults to a Python RESP `EVICT` controller mirroring `choose_normal` (optional Aura `policy_agent` in §C).

---

## A) Throughput (ops/s)

Frozen matrix: **1c×1t**, SET:GET=**1:10**, value **32B**, key 1..10000 **R:R**, `-n 20000`.  
Adaptive row = C server `--evict lru` + Python controller (`scripts/_e2e_adaptive_ctl.py`, same rules as `choose_normal`).

| Engine | pipeline | Totals ops/s | Avg latency (ms) | vs Redis |
|--------|----------|--------------|------------------|----------|
| redis:7-alpine | 1 | 38154.98 | 0.02679 | 1.00 |
| aura-redis C EVICT=lru | 1 | 46159.42 | 0.02091 | **1.210** |
| aura-redis C EVICT=lfu | 1 | 44648.74 | 0.02171 | **1.170** |
| aura-redis C adaptive (Py controller) | 1 | 43935.28 | 0.02157 | **1.151** |
| redis:7-alpine | 16 | 378931.41 | 0.03980 | 1.00 |
| aura-redis C EVICT=lru | 16 | 512491.99 | 0.02938 | **1.352** |
| aura-redis C EVICT=lfu | 16 | 416302.40 | 0.03446 | **1.099** |
| aura-redis C adaptive (Py controller) | 16 | 514033.10 | 0.02914 | **1.357** |
| Aura Lisp `server.aura` (short, n=500) | 1 | 13.68 | 73.09 | **~0.00036** (≈2790× slower than Redis) |

Notes:

- Without memory pressure, LRU / LFU / adaptive should be close; p=1 differences are noise + LFU counter updates + controller `INFO` polls.
- LFU at pipeline=16 pays more for per-access frequency bookkeeping (~19% below LRU here).
- Adaptive throughput matches LRU (controller is out-of-band; no maxmemory flips under this matrix).
- Lisp engine remains a functional demo only — C / FFI data plane is the speed path.

---

## B) Hit rate under dynamic load

Harness: `python3 scripts/bench_dynamic_evict.py` (maxmemory=120000, Python adaptive controller).  
Primary metric: hit% / useful target GETs (not ops/s).

| workload | policy | overall hit% | useful GETs | phase detail |
|----------|--------|--------------|-------------|--------------|
| hot_protect | lru | 0.0% | 0 | hot_protect=0.0%(0) |
| hot_protect | lfu | 100.0% | 40 | hot_protect=100.0%(40) |
| hot_protect | adaptive | 100.0% | 40 | hot_protect=100.0%(40); swap `lru→lfu` |
| ws_shift | lru | 100.0% | 1600 | ws_shift=100.0%(1600) |
| ws_shift | lfu | 34.4% | 551 | ws_shift=34.4%(551) |
| ws_shift | adaptive | 100.0% | 1600 | ws_shift=100.0%(1600) |
| oscillate | lru | 97.6% | 1600 | hot=0.0%(0), shift=100.0%(1600) |
| oscillate | lfu | 35.8% | 587 | hot=100.0%(40), shift=34.2%(547) |
| oscillate | adaptive | **100.0%** | **1640** | hot=100%(40), shift=100%(1600); swaps `lru→lfu`, `lfu→lru` |

See `docs/workloads.md` for workload design.

---

## C) Aura policy_agent path (optional)

`--aura-agent` (Docker `policy_agent.aura` → RESP `EVICT`, `AURA_REDIS_DENY_PLUGIN=1`) on `hot_protect` + `ws_shift`:

| workload | lru | lfu | adaptive (Aura agent) |
|----------|-----|-----|------------------------|
| hot_protect | 0.0% | 100.0% | **100.0%** (swap `lru → lfu`) |
| ws_shift | 100.0% | 34.4% | **100.0%** |

Adaptation still wins with the Aura-native controller. Wall-clock per adaptive run was ~1.2–1.3s vs ~0.5–0.7s for the Python controller (Docker agent startup / tick), not a data-plane regression.

---

## Interpretation

- **Gap vs Redis (raw speed):** On this host/matrix the C data plane is **~1.15–1.35×** Redis ops/s (p=1 and p=16). Treat as “same class”; absolute numbers move with CPU load. Lisp is ~**three orders of magnitude** slower.
- **When adaptive wins:** `hot_protect` and multi-phase `oscillate` — fixed LRU ages out hot keys under cold floods; adaptive flips to LFU then back to LRU for working-set shifts. Useful GETs on oscillate: adaptive **1640** vs LRU **1600** vs LFU **587**.
- **When fixed LRU is fine:** Stable read-heavy sets / `ws_shift`-like recency changes without a prior frequency trap — LRU already at 100% hit; adaptive matches, LFU loses (~34%).
- **Do not mix scores:** Hit-ratio benches use tiny `maxmemory`; throughput benches do not. Eviction sampling and controller ticks are orthogonal to the memtier gate.

---

## Reproduce

```bash
./scripts/build-native.sh
./scripts/bench-e2e.sh                    # A + B + optional C → docs/perf-eval.md
# pieces:
./scripts/memtier-cmp.sh                  # Redis vs default C (also refreshes docs/perf-log.md)
python3 scripts/bench_dynamic_evict.py
python3 scripts/bench_dynamic_evict.py --aura-agent --workloads hot_protect,ws_shift
# env knobs: MEMTIER_N, REDIS_PORT, AURA_PORT, SKIP_LISP=1, SKIP_AURA_AGENT=1
```

Related: `docs/perf-log.md` (historical memtier gate), `docs/workloads.md` (dynamic eviction design).
