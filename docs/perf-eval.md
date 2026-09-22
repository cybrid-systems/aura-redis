# aura-redis end-to-end performance evaluation

**Date:** 2026-09-22 21:22:30 CST (Asia/Shanghai)  
**SHA:** `d1d1db2` (`d1d1db21e1536acfd886d3e5b947f4bc6ebed8c1`)  
**Host:** Linux x86_64, 8 CPUs, `Intel(R) Xeon(R) Processor`  
**Data plane:** host `native/build/aura_redis_server` (Release)  
**Redis baseline:** `redis:7-alpine` (Docker, `--network host`)  
**Loadgen:** `redislabs/memtier_benchmark` (Docker, host network)

Two independent dimensions — **do not** collapse into one score:

1. **Throughput** (memtier) — no `maxmemory` pressure; raw RESP / data-plane speed.
2. **Hit quality** (dynamic workloads) — small `maxmemory=120000`; eviction policy fitness.

Caveats: approximate LFU sampling (16 samples); adaptive defaults to a Python RESP `EVICT`/`LAYOUT` controller mirroring `choose_normal` (Aura `policy_agent` verified in §C). Absolute ops/s move with CPU load; **ratios** are the citeable signal.

---

## Headline contrasts (cite these)

| Claim | Evidence (this run) |
|-------|---------------------|
| **Static LRU collapses on Meta-like hot floods** | `hot_protect` / `zipf_hotkey`: LRU **0.0%** hit |
| **Adaptive near-oracle on those floods** | `hot_protect` adaptive **100%**; `zipf_hotkey` adaptive **97.6%** (LFU 100%) |
| **Adaptive also wins multi-phase** | `oscillate`: adaptive **100%** / 1640 useful vs LRU 97.6% / 1600 vs LFU 34.8% / 570 |
| **Aura C ≥ Redis ops/s** | p=1 **~1.08–1.15×**; p=16 **~1.27–1.36×** vs `redis:7-alpine` |
| **Demo MVP one-liner** | `./scripts/demo-mvp.sh`: Phase A LRU **0%** → adaptive **100%** |

---

## A) Throughput (ops/s)

Frozen matrix: **1c×1t**, SET:GET=**1:10**, value **32B**, key 1..10000 **R:R**, `-n 20000`.  
Adaptive row = C server `--evict lru` + Python controller (`scripts/_e2e_adaptive_ctl.py`, same rules as `choose_normal`).

| Engine | pipeline | Totals ops/s | Avg latency (ms) | vs Redis |
|--------|----------|--------------|------------------|----------|
| redis:7-alpine | 1 | 42056.57 | 0.02344 | 1.00 |
| aura-redis C EVICT=lru | 1 | 45282.37 | 0.02130 | **1.077** |
| aura-redis C EVICT=lfu | 1 | 45527.39 | 0.02112 | **1.083** |
| aura-redis C adaptive (Py controller) | 1 | 48245.09 | 0.02035 | **1.147** |
| redis:7-alpine | 16 | 377287.30 | 0.04012 | 1.00 |
| aura-redis C EVICT=lru | 16 | 511312.80 | 0.02941 | **1.355** |
| aura-redis C EVICT=lfu | 16 | 508595.26 | 0.02924 | **1.348** |
| aura-redis C adaptive (Py controller) | 16 | 477760.26 | 0.03006 | **1.266** |

Notes:

- Without memory pressure, LRU / LFU / adaptive should be close; p=1 differences are noise + LFU bookkeeping + controller `INFO` polls.
- Adaptive at p=16 is slightly below LRU here (controller ticks / shared host noise) but still **~1.27×** Redis.
- Lisp `server.aura` remains a functional demo only (~three orders of magnitude slower on prior short runs) — C / FFI is the speed path.

---

## B) Hit rate under dynamic load

Harness: `python3 scripts/bench_dynamic_evict.py --workloads hot_protect,ws_shift,oscillate,zipf_hotkey` (maxmemory=120000, seed=42).  
Primary metric: hit% / useful target GETs (not ops/s).

| workload | policy | overall hit% | useful GETs | phase detail |
|----------|--------|--------------|-------------|--------------|
| hot_protect | lru | **0.0%** | 0 | hot_protect=0.0%(0) |
| hot_protect | lfu | 100.0% | 40 | hot_protect=100.0%(40) |
| hot_protect | adaptive | **100.0%** | 40 | hot_protect=100.0%(40); swap `lru→lfu` |
| ws_shift | lru | 100.0% | 1600 | ws_shift=100.0%(1600) |
| ws_shift | lfu | 33.4% | 535 | ws_shift=33.4%(535) |
| ws_shift | adaptive | **100.0%** | 1600 | ws_shift=100.0%(1600) |
| oscillate | lru | 97.6% | 1600 | hot=0.0%(0), shift=100.0%(1600) |
| oscillate | lfu | 34.8% | 570 | hot=100.0%(40), shift=33.1%(530) |
| oscillate | adaptive | **100.0%** | **1640** | hot=100%(40), shift=100%(1600); swaps both ways |
| zipf_hotkey | lru | **0.0%** | 0 | zipf_hotkey=0.0%(0) |
| zipf_hotkey | lfu | 100.0% | 127 | zipf_hotkey=100.0%(127) |
| zipf_hotkey | adaptive | **97.6%** | 124 | zipf_hotkey=97.6%(124); near-oracle (2.4pp regret) |

**Regret vs best-fixed:** adaptive ≤2.4pp on all four workloads (✓ near-oracle).

Also confirmed by:

- `./scripts/demo-mvp.sh` — Phase A static LRU **0.0%** vs Aura adaptive **100.0%**; Phase B WS adaptive **100.0%**.
- `python3 scripts/bench_industry.py` — same zipf/hot_protect/oscillate story (PASS).

See `docs/workloads.md` for workload design.

---

## C) Aura policy_agent path (optional)

`--aura-agent` (Docker `policy_agent.aura` → RESP `EVICT`, `AURA_REDIS_DENY_PLUGIN=1`) on `hot_protect` + `ws_shift`:

| workload | lru | lfu | adaptive (Aura agent) |
|----------|-----|-----|------------------------|
| hot_protect | 0.0% | 100.0% | **100.0%** (swap `lru → lfu`) |
| ws_shift | 100.0% | 33.4% | **100.0%** |

Adaptation still wins with the Aura-native controller. Wall-clock per adaptive run ~1.3s vs ~0.5–0.6s for the Python controller (Docker agent startup / tick), not a data-plane regression.

---

## Interpretation

- **Gap vs Redis (raw speed):** On this host/matrix the C data plane is **~1.08–1.35×** Redis ops/s (p=1 and p=16). Treat as “same class / slightly faster”; cite ratios, not absolute ops/s.
- **When adaptive wins hard:** `hot_protect` and `zipf_hotkey` — fixed LRU ages out the hot set under cold floods (**0%**); adaptive flips to LFU (+ layout) and holds **~98–100%**.
- **When adaptive wins across phases:** `oscillate` — useful GETs adaptive **1640** vs LRU **1600** vs LFU **570**.
- **When fixed LRU is fine:** `ws_shift`-like recency changes — LRU already 100%; adaptive matches; LFU loses (~33%).
- **Do not mix scores:** Hit-ratio benches use tiny `maxmemory`; throughput benches do not.

---

## Reproduce

```bash
./scripts/build-native.sh
./scripts/bench-e2e.sh                    # A + B + optional C → docs/perf-eval.md (+ stdout scoreboard)
# pieces:
./scripts/memtier-cmp.sh                  # Redis vs default C (also refreshes docs/perf-log.md)
python3 scripts/bench_dynamic_evict.py --workloads hot_protect,ws_shift,oscillate,zipf_hotkey
python3 scripts/bench_industry.py
./scripts/demo-mvp.sh
python3 scripts/bench_dynamic_evict.py --aura-agent --workloads hot_protect,ws_shift
# env knobs: MEMTIER_N, REDIS_PORT, AURA_PORT, SKIP_LISP=1, SKIP_AURA_AGENT=1
```

Related: `docs/perf-log.md` (historical memtier gate), `docs/workloads.md` (dynamic eviction design).
