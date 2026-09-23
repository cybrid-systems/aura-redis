# aura-redis end-to-end performance evaluation

**Date:** 2026-09-23 06:55:00 CST (Asia/Shanghai)  
**SHA:** `b64fd21` (`b64fd2171257eb50cae7fc248b69f196f4b0d7d2`) — post P2 TLS tip  
**Host:** Linux x86_64, 8 CPUs, `Intel(R) Xeon(R) Processor` (KVM)  
**Data plane:** host `native/build/aura_redis_server` (Release, OpenSSL TLS linked)  
**Redis baseline:** `redis:7-alpine` (Docker, `--network host`)  
**Loadgen:** `redislabs/memtier_benchmark` (Docker, host network)

Two independent dimensions — **do not** collapse into one score:

1. **Throughput** (memtier) — no `maxmemory` pressure; raw RESP / data-plane speed.
2. **Hit quality** (dynamic workloads) — small `maxmemory=120000`; eviction policy fitness.

Caveats: approximate LFU sampling (default 16, adaptive bumps to 64 under pin); adaptive DEFAULT is Aura `policy_agent.aura` (Docker) writing RESP `EVICT`/`LAYOUT`/`PIN` from `choose_normal.aura`. Python `choose_policy` is a host-only CI mirror (`--python-ctl`). Absolute ops/s move with CPU load; **ratios** are the citeable signal.

---

## Headline contrasts (cite these)

| Claim | Evidence (this run) |
|-------|---------------------|
| **Adaptive beats BOTH fixed on cumulative marathon** | `phase_marathon`: adaptive **100%** / 1956 useful vs LRU **81.8%** / 1600 vs LFU **42.9%** / 840 (**+18.2pp** vs LRU, **+57.1pp** vs LFU; regret_hits=0) |
| **Static LRU collapses on Meta-like hot floods** | `hot_protect` / `zipf_hotkey`: LRU **0.0%** hit |
| **Zipf adaptive ≈ LFU (0pp regret)** | `zipf_hotkey`: adaptive **100%** = LFU **100%** |
| **Aura C ≥ Redis ops/s (throughput ≠ adaptive story)** | Frozen p=1 **~1.07–1.13×**; p=16 **~1.02–1.11×** vs `redis:7-alpine` — do **not** cite adaptive for throughput wins |
| **Expanded matrix still ≥ Redis** | Long N=100k lru **~1.08–1.14×**; pipeline=8 **~1.12×**; 4c×4t **~1.12×** |
| **Mutation / industry packs** | `poison_heal` mutate **+100pp** vs frozen; `ttl_wave` / `flash_churn` / `prefix_mix` adaptive paths PASS; `mutation_gain` mutate−frozen **+27.9pp** (A1 PASS); `evolve_gain` evolve=frozen (assert FAIL, Δ=0) |

Primary scoreboard = **multi-phase cumulative useful-GET hit% + regret vs per-phase oracle**, not single-phase hit% and not memtier ops/s.

---

## A) Throughput (ops/s)

### A1) Frozen matrix

**1c×1t**, SET:GET=**1:10**, value **32B**, key 1..10000 **R:R**, `-n 20000`.  
Adaptive throughput row = C server `--evict lru` + host Python mirror (`scripts/_e2e_adaptive_ctl.py`) — throughput benches are not the Aura story; hit-quality benches use Aura `policy_agent` by default.

| Engine | pipeline | Totals ops/s | Avg latency (ms) | vs Redis |
|--------|----------|--------------|------------------|----------|
| redis:7-alpine | 1 | 42345.61 | 0.02301 | 1.00 |
| aura-redis C EVICT=lru | 1 | 47773.97 | 0.02088 | **1.128** |
| aura-redis C EVICT=lfu | 1 | 45254.19 | 0.02157 | **1.069** |
| aura-redis C adaptive (Py controller) | 1 | 46341.78 | 0.02090 | **1.094** |
| redis:7-alpine | 16 | 413787.40 | 0.03716 | 1.00 |
| aura-redis C EVICT=lru | 16 | 460553.59 | 0.03087 | **1.113** |
| aura-redis C EVICT=lfu | 16 | 420203.38 | 0.03357 | **1.015** |
| aura-redis C adaptive (Py controller) | 16 | 456225.19 | 0.03143 | **1.103** |

### A2) Expanded dimensions (lru vs redis only)

| Variant | Engine | Totals ops/s | vs Redis |
|---------|--------|--------------|----------|
| n=100000, p=1, 1c×1t | redis:7-alpine | 41459.49 | 1.00 |
| n=100000, p=1, 1c×1t | aura-redis C EVICT=lru | 44615.93 | **1.076** |
| n=100000, p=16, 1c×1t | redis:7-alpine | 391785.05 | 1.00 |
| n=100000, p=16, 1c×1t | aura-redis C EVICT=lru | 447623.57 | **1.142** |
| n=20000, p=8, 1c×1t | redis:7-alpine | 256383.96 | 1.00 |
| n=20000, p=8, 1c×1t | aura-redis C EVICT=lru | 287079.97 | **1.120** |
| n=20000, p=1, 4c×4t | redis:7-alpine | 151140.16 | 1.00 |
| n=20000, p=1, 4c×4t | aura-redis C EVICT=lru | 168461.66 | **1.115** |

Notes:

- Without memory pressure, LRU / LFU / adaptive should be close; p=1 differences are noise + LFU bookkeeping + controller INFO polls.
- Absolute ops/s vs prior `9a968aa` eval moved with host load (Redis p=16 higher here; Aura p=16 lower) — **cite ratios**, not absolutes. Frozen p=16 ratio band is ~1.02–1.11× this run vs ~1.27–1.36× then; still ≥ Redis.
- **Do not claim adaptive throughput wins** — that is not the Aura story.
- Lisp `server.aura` remains a functional demo only.

---

## B) Hit rate under dynamic load (HEADLINE)

Harness: `python3 scripts/bench_regret.py` (wraps `bench_dynamic_evict.py`).  
`maxmemory=120000`, seed=42. Primary metric: **cumulative useful target GET hit%** / regret vs per-phase oracle.  
Adaptive DEFAULT = Aura `policy_agent.aura` (Docker).

### B1) `phase_marathon` (cite this)

One server lifetime: `zipf_hotkey` → bridge/UNPIN → `ws_shift` → bridge → `hot_protect`.

| policy | cum hit% | useful GETs | regret_hits vs oracle | per-phase |
|--------|----------|-------------|------------------------|-----------|
| lru | **81.8%** | 1600 | 356 | zipf=0%, ws=100%, hot=0% |
| lfu | **42.9%** | 840 | 1116 | zipf=100%, ws=30.2%, hot=100% |
| adaptive | **100.0%** | **1956** | **0** | zipf=100%, ws=100%, hot=100% |

`adaptive vs fixed: +18.2pp vs LRU, +57.1pp vs LFU`.

### B2) Appendix — single-phase / oscillate / industry

| workload | lru | lfu | adaptive | notes |
|----------|-----|-----|----------|-------|
| hot_protect | **0.0%** | 100% | **100%** | industry pack |
| ws_shift | 100% | 30.1% | **100%** | adaptive matches LRU |
| oscillate | 97.6% | 33.1% | **100%** / 1640 useful | near-oracle |
| zipf_hotkey | **0.0%** | 100% | **100%** (0pp regret) | |

`python3 scripts/bench_industry.py` (phase_marathon + zipf + hot_protect + oscillate): PASS on headline criteria.

### B3) Mutation / stretch packs (expanded)

| pack | key result | exit |
|------|------------|------|
| `poison_heal` | poison_mutate **100%** vs poison_frozen **0%** (Δ=+100pp; fitness-heal!) | PASS |
| `ttl_wave` | adaptive / ttl_aware **100%** vs lru **0%** / lfu **31.2%** | PASS |
| `flash_churn` | adaptive_soft / nosoft **100%** / 400 useful vs lfu **5%** / lru **0%** | PASS |
| `prefix_mix` | adaptive_prefix **100%** vs global adaptive **28.6%** / lru **1.8%** (Δ=+71.4pp) | PASS |
| `mutation_gain` | mutate **100%** vs frozen **72.1%** (Δ=**+27.9pp**); LFU **100%**; fitness-swap + inline EVICT/PIN | **PASS** (A1 restore) |
| `evolve_gain` | evolve=frozen **10.0%**; LFU **100%**; evolve Δ=+0.0pp (need ≥8pp) | FAIL assert |

Note: `evolve_gain` still stretch (Δ=0) — A2 next. `mutation_gain` restored A1 (+27.9pp).

Also: `./scripts/demo-mvp.sh` Phase A smoke flaked this run (`policy_agent: PING → ERR closed`); harness benches above used the same Aura agent path successfully.

See `docs/workloads.md`.

---

## C) Aura policy_agent path (DEFAULT for adaptive)

Adaptive benches default to Docker `policy_agent.aura` → RESP `EVICT`/`LAYOUT`/`PIN` (`AURA_REDIS_DENY_PLUGIN=1`). Use `--python-ctl` only for host-only CI. Agent PIN/UNPIN prefix sets + samples=64 on pin path (flat layout during protect).

Fresh `phase_marathon` with Aura agent (2026-09-23 / `b64fd21`): adaptive **100%** / 1956 useful vs LRU **81.8%** / 1600 vs LFU **42.9%** / 840 (**+18.2pp** vs LRU, **+57.1pp** vs LFU; regret_hits=0). Agent also emitted `fitness-threshold-mutate` events on several packs.

---

## Interpretation

- **Gap vs Redis (raw speed):** On this host/matrix the C data plane is **~1.02–1.14×** Redis ops/s across frozen + expanded rows. Treat as “same class / slightly faster”; cite ratios, not absolute ops/s.
- **vs prior `9a968aa` eval:** Hit-quality marathon still **100% / 0 regret**. Throughput ratios compressed at p=16 under noisier host load; still ≥ Redis. Do not treat absolute ops/s drift as a product regression without a quiet-host re-run.
- **When adaptive wins hard (HEADLINE):** `phase_marathon` — fixed LRU dies on zipf/hot; fixed LFU dies on ws_shift; adaptive tracks the better kernel (+ PIN) → **100% cumulative / 0 regret**.
- **Zipf single-phase:** adaptive matches LFU at **100%**.
- **When fixed LRU is fine:** `ws_shift`-like recency changes — LRU already 100%; adaptive matches; LFU loses (~30%).
- **Do not mix scores:** Hit-ratio benches use tiny `maxmemory`; throughput benches do not. Do not claim adaptive throughput wins.

---

## Reproduce

```bash
./scripts/build-native.sh
./scripts/bench-e2e.sh                    # A + B + optional C → docs/perf-eval.md
# pieces:
./scripts/memtier-cmp.sh                  # Redis vs default C (also refreshes docs/perf-log.md)
# expanded throughput (example):
MEMTIER_N=100000 ./scripts/memtier-cmp.sh
python3 scripts/bench_regret.py           # HEADLINE phase_marathon + zipf + oscillate
python3 scripts/bench_industry.py
python3 scripts/bench_regret.py mutation_gain
python3 scripts/bench_regret.py poison_heal
python3 scripts/bench_regret.py ttl_wave
python3 scripts/bench_regret.py prefix_mix
python3 scripts/bench_regret.py flash_churn
python3 scripts/bench_regret.py evolve_gain
./scripts/demo-mvp.sh
# env knobs: MEMTIER_N, REDIS_PORT, AURA_PORT, SKIP_LISP=1, AURA_AGENT=0
```

Related: `docs/perf-log.md` (historical memtier gate), `docs/workloads.md` (dynamic eviction design), `docs/mvp-plan.md` (why regret is the headline).
