# aura-redis end-to-end performance evaluation

**Date:** 2026-09-22 21:37:44 CST (Asia/Shanghai)  
**SHA:** `9a968aa` (`9a968aa80d61e8786e0bf4c2f551e86ce3ef923b`)  
**Host:** Linux x86_64, 8 CPUs, `Intel(R) Xeon(R) Processor`  
**Data plane:** host `native/build/aura_redis_server` (Release)  
**Redis baseline:** `redis:7-alpine` (Docker, `--network host`)  
**Loadgen:** `redislabs/memtier_benchmark` (Docker, host network)

Two independent dimensions — **do not** collapse into one score:

1. **Throughput** (memtier) — no `maxmemory` pressure; raw RESP / data-plane speed.
2. **Hit quality** (dynamic workloads) — small `maxmemory=120000`; eviction policy fitness.

Caveats: approximate LFU sampling (default 16, adaptive bumps to 64 under pin); adaptive defaults to a Python RESP `EVICT`/`LAYOUT`/`PIN` controller mirroring `choose_normal` (Aura `policy_agent` verified in §C). Absolute ops/s move with CPU load; **ratios** are the citeable signal.

---

## Headline contrasts (cite these)

| Claim | Evidence (this run) |
|-------|---------------------|
| **Adaptive beats BOTH fixed on cumulative marathon** | `phase_marathon`: adaptive **100%** / 1956 useful vs LRU **81.8%** / 1600 vs LFU **45.8%** / 896 (**+18.2pp** vs LRU, **+54.2pp** vs LFU; regret_hits=0) |
| **Static LRU collapses on Meta-like hot floods** | `hot_protect` / `zipf_hotkey`: LRU **0.0%** hit |
| **Zipf adaptive ≈ LFU (0pp regret)** | `zipf_hotkey`: adaptive **100%** = LFU **100%** (re-SET + PIN hot head before cold flood; samples=64) |
| **Aura C ≥ Redis ops/s (throughput ≠ adaptive story)** | p=1 **~1.08–1.15×**; p=16 **~1.27–1.36×** vs `redis:7-alpine` — do **not** cite adaptive for throughput wins |
| **Demo MVP** | `./scripts/demo-mvp.sh`: live Phase A LRU lose + **headline phase_marathon** cumulative/regret PASS |

Primary scoreboard = **multi-phase cumulative useful-GET hit% + regret vs per-phase oracle**, not single-phase hit% and not memtier ops/s.

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

- Without memory pressure, LRU / LFU / adaptive should be close; p=1 differences are noise + LFU bookkeeping + controller INFO polls.
- Adaptive at p=16 is slightly below LRU here (controller ticks / shared host noise) but still **~1.27×** Redis.
- **Do not claim adaptive throughput wins** — that is not the Aura story.
- Lisp `server.aura` remains a functional demo only.


---

## B) Hit rate under dynamic load (HEADLINE)

Harness: `python3 scripts/bench_regret.py` (wraps `bench_dynamic_evict.py`).  
`maxmemory=120000`, seed=42. Primary metric: **cumulative useful target GET hit%** / regret vs per-phase oracle.

### B1) `phase_marathon` (cite this)

One server lifetime: `zipf_hotkey` → bridge/UNPIN → `ws_shift` → bridge → `hot_protect`.

| policy | cum hit% | useful GETs | regret_hits vs oracle | per-phase |
|--------|----------|-------------|------------------------|-----------|
| lru | **81.8%** | 1600 | 356 | zipf=0%, ws=100%, hot=0% |
| lfu | **45.8%** | 896 | 1060 | zipf=100%, ws=33.8%, hot=100% |
| adaptive | **100.0%** | **1956** | **0** | zipf=100%, ws=100%, hot=100%; swaps both ways + PIN |

`adaptive vs fixed: +18.2pp vs LRU, +54.2pp vs LFU`.

### B2) Appendix — single-phase / oscillate

| workload | lru | lfu | adaptive | notes |
|----------|-----|-----|----------|-------|
| hot_protect | **0.0%** | 100% | **100%** | swap lru→lfu |
| ws_shift | 100% | 33.4% | **100%** | adaptive matches LRU |
| oscillate | 97.6% | 34.8% | **100%** / 1640 useful | near-oracle; small margin vs LRU (hot phase only 40 GETs) |
| zipf_hotkey | **0.0%** | 100% | **100%** (0pp regret) | re-SET + PIN full hot head |

Also: `./scripts/demo-mvp.sh` prints live Phase A/B then re-runs `phase_marathon` as the PASS gate.

See `docs/workloads.md`.

---

## C) Aura policy_agent path (optional)

`--aura-agent` (Docker `policy_agent.aura` → RESP `EVICT`/`LAYOUT`/`PIN`, `AURA_REDIS_DENY_PLUGIN=1`) on `hot_protect` + `ws_shift` previously confirmed adaptive near-oracle. Agent now PIN/UNPIN prefix sets + samples=64 on pin path (flat layout during protect).

---

## Interpretation

- **Gap vs Redis (raw speed):** On this host/matrix the C data plane is **~1.08–1.35×** Redis ops/s (p=1 and p=16). Treat as “same class / slightly faster”; cite ratios, not absolute ops/s.
- **When adaptive wins hard (HEADLINE):** `phase_marathon` — fixed LRU dies on zipf/hot; fixed LFU dies on ws_shift; adaptive tracks the better kernel (+ PIN) → **100% cumulative / 0 regret**, clearly above both fixed.
- **Zipf single-phase:** adaptive matches LFU at **100%** once hot keys are re-SET then PIN’d before the cold flood (earlier ~2–5pp regret was pinning already-evicted keys).
- **When fixed LRU is fine:** `ws_shift`-like recency changes — LRU already 100%; adaptive matches; LFU loses (~33%).
- **Do not mix scores:** Hit-ratio benches use tiny `maxmemory`; throughput benches do not. Do not claim adaptive throughput wins.

---

## Reproduce

```bash
./scripts/build-native.sh
./scripts/bench-e2e.sh                    # A + B + optional C → docs/perf-eval.md
# pieces:
./scripts/memtier-cmp.sh                  # Redis vs default C (also refreshes docs/perf-log.md)
python3 scripts/bench_regret.py           # HEADLINE phase_marathon + zipf + oscillate
python3 scripts/bench_dynamic_evict.py --workloads phase_marathon,zipf_hotkey,hot_protect,ws_shift,oscillate
python3 scripts/bench_industry.py
./scripts/demo-mvp.sh
python3 scripts/bench_dynamic_evict.py --aura-agent --workloads hot_protect,ws_shift
# env knobs: MEMTIER_N, REDIS_PORT, AURA_PORT, SKIP_LISP=1, SKIP_AURA_AGENT=1
```

Related: `docs/perf-log.md` (historical memtier gate), `docs/workloads.md` (dynamic eviction design), `docs/mvp-plan.md` (why regret is the headline).
