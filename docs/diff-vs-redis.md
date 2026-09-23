# Aura differentiation vs Redis (A17 / A18 / A19 + adaptive moat)

**Date:** 2026-09-23 13:10:53 CST  
**Tip:** `28b461d` (`28b461da8136e12afc8af0529b98303a93c4d139`)  
**Discipline:** `AURA_REDIS_DENY_PLUGIN=1`; dual scoreboard (hit-quality ≠ memtier).  
**Redis baseline:** `redis:7-alpine` fixed `allkeys-lru` / `allkeys-lfu` only.  
**Aura control plane:** `policy_agent.aura` (Soft sandbox OK for this experiment).

> Adaptive hit-quality SSOT = `python3 scripts/bench_regret.py phase_marathon`.
> Short `bench_hit_vs_redis.py` adaptive rows remain non-citeable; fixed-kernel vs Redis OK.
> Never claim memtier adaptive wins — E5 is C dataplane parity only.

---

## E1 — Multi-phase regret (established adaptive moat)

Authoritative Aura marathon (`bench_regret.py phase_marathon`, `maxmemory=120000`).

| Engine | Policy | Cum useful-GET hit% | Useful | Regret | Notes |
|--------|--------|---------------------|--------|--------|-------|
| aura | **adaptive** | **100.0%** | 1956 | -110 | policy_agent live EVICT/PIN |
| aura | **lru** | **81.8%** | 1600 | 246 | static kernel |
| aura | **lfu** | **36.1%** | 706 | 1140 | static kernel |

**Cite:** adaptive vs aura LRU **+18.2pp**, vs aura LFU **+63.9pp**.

### E1b — Redis fixed-policy side-by-side (static kernels only)

| Workload | Engine | Policy | Hit% | Useful hits | Misses | Notes |
|----------|--------|--------|------|-------------|--------|-------|
| hot_protect | aura | lfu | 100.0 | 16 | 0 | — |
| hot_protect | aura | lru | 0.0 | 0 | 16 | — |
| hot_protect | aura | slru | 100.0 | 16 | 0 | — |
| hot_protect | redis | lfu | 100.0 | 16 | 0 | maxmemory-policy=allkeys-lfu |
| hot_protect | redis | lru | 6.2 | 1 | 15 | maxmemory-policy=allkeys-lru |
| phase_marathon | aura | lfu | 100.0 | 76 | 0 | — |
| phase_marathon | aura | lru | 40.8 | 31 | 45 | — |
| phase_marathon | aura | slru | 100.0 | 76 | 0 | — |
| phase_marathon | redis | lfu | 100.0 | 76 | 0 | maxmemory-policy=allkeys-lfu |
| phase_marathon | redis | lru | 69.7 | 53 | 23 | maxmemory-policy=allkeys-lru |
| ws_shift | aura | lfu | 100.0 | 40 | 0 | — |
| ws_shift | aura | lru | 77.5 | 31 | 9 | — |
| ws_shift | aura | slru | 100.0 | 40 | 0 | — |
| ws_shift | redis | lfu | 100.0 | 40 | 0 | maxmemory-policy=allkeys-lfu |
| ws_shift | redis | lru | 57.5 | 23 | 17 | maxmemory-policy=allkeys-lru |
| zipf | aura | lfu | 100.0 | 20 | 0 | — |
| zipf | aura | lru | 0.0 | 0 | 20 | — |
| zipf | aura | slru | 100.0 | 20 | 0 | — |
| zipf | redis | lfu | 65.0 | 13 | 7 | maxmemory-policy=allkeys-lfu |
| zipf | redis | lru | 15.0 | 3 | 17 | maxmemory-policy=allkeys-lru |

Redis cannot switch kernels mid-flight; cite §E1 adaptive for the moat, §E1b for fixed-kernel parity/contrast only.

---

## E2 — Poison-key flood (A19)

Unique-SET poison + light poison GET noise under tight maxmemory; scoreboard = **keep\*** hit% (not ops/s).

| Engine | Policy | keep* hit% | Hits | Misses | unique_sets | Notes |
|--------|--------|------------|------|--------|-------------|-------|
| redis | lru | 0.0 | 0 | 20 | 0 | maxmemory-policy=allkeys-lru |
| redis | lfu | 0.0 | 0 | 20 | 0 | maxmemory-policy=allkeys-lfu |
| aura | lru | 0.0 | 0 | 20 | 820 | EVICT=lru |
| aura | lfu | 100.0 | 20 | 0 | 820 | EVICT=lfu |
| aura | adaptive_poison_off | 0.0 | 0 | 20 | 820 | policy_agent poison_defense=OFF seed=aggressive fitness=off defended=n/a(off) explain_mid=4 reason=unknown |
| aura | adaptive_poison_on | 100.0 | 20 | 0 | 900 | policy_agent poison_defense=ON seed=normal fitness=off defended=yes explain_mid=6 reason=unique_set_storm |

**Hit-quality under poison flood:** aura static LFU **100.0%** vs redis allkeys-lru **0.0%** / allkeys-lfu **0.0%**.
**A19 control-plane:** adaptive_poison_on keep*=**100.0%** vs redis LRU 0.0% (aura static LFU 100.0%) with `defended=yes` + `INFO explain_*` mid join (Redis cannot mutate choose-fn).
A19 defensive body is `lfu|flat|soft` (refuse pin; LFU-oriented keep* defense). Cite adaptive_poison_on keep* vs Redis 0% (and ≈ aura static LFU when landed); cite storm→mutate+explain for the control-plane moat.

Redis has no `unique_sets` storm detector and cannot mutate choose-fn to `lfu|flat|soft` (refuse pin) mid-process.

---

## E3 — Shadow→Canary promote (A18)

**What Redis cannot do:** in-process policy *code* generation / choose-fn canary. Redis only `CONFIG SET maxmemory-policy` (requires ops change; no shadow dry-run → trial body → commit/heal). Aura A18 winner selection is **score-gated** (`winner_score` from hit_ewma + sampled shadow miss% + diverges; default AUTOPROMOTE OFF).

| Signal | Value |
|--------|-------|
| AUTOPROMOTE (experiment) | ON (prod default **OFF**) |
| boot saw `shadow-autopromote on` | True |
| saw autopromote/canary | True |
| EVICT before → after | `lru` → `lfu` |
| server restart required | **False** |

Sample agent lines (sanitized):

```
policy_agent: fitness-mutate=on seed=normal
policy_agent: pin wrote path=/work/.ar-diff-canary-hb-26681.pin profile=normal version=5 hash=600000
policy_agent: heartbeat wrote path=/work/.ar-diff-canary-hb-26681 bytes=890
policy_agent: heartbeat wrote path=/work/.ar-diff-canary-hb-26681 bytes=890
policy_agent: shadow-ab champ=lru alt=lfu|flat|pin (dry-run no EVICT loser)
policy_agent: shadow-ab autopromote score=105 gate=40 samples=0 diverges=1 ewma=0 -> canary aggressive
policy_agent: audit ts=228195129 op=shadow-autopromote from=champ to=aggressive reason=shadow_winner_score version=5 mid=2 evict=- layout=-
policy_agent: audit-file wrote path=/work/.ar-diff-canary-audit-26681.log
policy_agent: heartbeat wrote path=/work/.ar-diff-canary-hb-26681 bytes=1023
policy_agent: canary-start normal→aggressive reason=shadow_autopromote result=#t
policy_agent: audit ts=228195183 op=canary-start from=normal to=aggressive reason=canary_start version=5 mid=4 evict=- layout=-
policy_agent: audit-file wrote path=/work/.ar-diff-canary-audit-26681.log
policy_agent: heartbeat wrote path=/work/.ar-diff-canary-hb-26681 bytes=1004
policy_agent: pin wrote path=/work/.ar-diff-canary-hb-26681.pin profile=aggressive version=5 hash=1000000
policy_agent: canary-tick n=1/4 ewma=0 avg=0 drop=0 to=aggressive
policy_agent: EVICT (post-swap) → lfu
policy_agent: heartbeat wrote path=/work/.ar-diff-canary-hb-26681 bytes=1012
policy_agent: pin wrote path=/work/.ar-diff-canary-hb-26681.pin profile=aggressive version=5 hash=1003040
policy_agent: canary-tick n=2/4 ewma=23 avg=77 drop=0 to=aggressive
policy_agent: heartbeat wrote path=/work/.ar-diff-canary-hb-26681 bytes=1013
```

Redis contrast:

```
{
  "kind": "redis_contrast",
  "config_maxmemory_policy": [
    "maxmemory-policy",
    "allkeys-lru"
  ],
  "has_policy_explain": false,
  "has_shadow_canary": false,
  "has_in_process_codegen": false,
  "note": "Redis exposes CONFIG GET maxmemory-policy only; no mid join / canary / live choose-fn."
}
```

---

## E4 — Provenance explain (A17)

Operator artifact after mutate/EVICT change: mid → reason → kernel join via `POLICY EXPLAIN` / `INFO explain_*` / heartbeat. Redis side = `CONFIG GET maxmemory-policy` only.

### Aura sample (sanitized)

```
POLICY EXPLAIN => mid=4|evict:unknown|evict=lfu|layout=flat
INFO explain_* => {
  "explain_mid": "4",
  "explain_reason": "unknown",
  "explain_op": "evict",
  "explain_evict": "lfu",
  "explain_layout": "flat",
  "explain": "mid=4|evict:unknown|evict=lfu|layout=flat",
  "evict": "lfu",
  "layout": "flat",
  "unique_sets": "656"
}
EVICT => lfu
--- heartbeat (tail) ---
out=flat
reconnects=0
applies=3
audit_count=3
last_audit_op=evict
last_audit_from=lru
last_audit_to=lfu
last_audit_reason=unknown
last_audit_version=5
last_explain=mid=4|evict:unknown:lru->lfu|evict=lfu|layout=flat
last_audit_mid=4
explain_pushed=3
shadow_autopromote=0
shadow_autopromote_starts=0
shadow_winner_score=0
shadow_score_gate=40
shadow_score_promotes=0
poison_active=0
poison_trips=1
canary_mode=idle
canary_ticks=0
canary_commits=0
canary_heals=0
prefix_deep=0
prefix_bags=;
prefix_bag_version=0
profile_hash=903040
from_version=0
pin_resumed=0
meta_frozen=0
tick_ms=80
info_polls=7
freeze_count=0
unfreeze_count=0
evolve_backend=hand
w_miss=10
w_write=2
w_evict=10
evolve_gen=0
evolve_keeps=0
weight_keeps=0
swarm_proposes=0
shadow_scores=0
shadow_ab_ticks=0
shadow_ab_diverges=0
shadow_ab_blocked=0
typed_pressure=0
hash_share=0
keys_hash=0
hit_ewma=0
swaps=2
applies_sec=0
polls_sec=1
```

### Redis sample

```
CONFIG GET maxmemory-policy => ['maxmemory-policy', 'allkeys-lru']
# no POLICY EXPLAIN / explain_mid / audit mid join
```

---

## E5 — Throughput hygiene (dataplane parity only)

Optional short memtier: **aura EVICT=lru** vs **redis:7-alpine**, pipeline 1/16. **Not** an adaptive win — cite ratios as C RESP parity.

_E5 skipped or memtier artifact absent — see `docs/redis-compare.md` for last citeable ratios._

---

## What Redis cannot do (A17 / A18 summary)

| Capability | Aura | Redis |
|------------|------|-------|
| Live choose-fn mutate + heal | yes (`policy_agent`) | no |
| Shadow dry-run → canary trial → commit | yes (A18, default OFF) | no |
| Provenance mid→reason→kernel explain | yes (A17) | `CONFIG GET` policy name only |
| Unique-SET storm → defensive body | yes (A19) | fixed `maxmemory-policy` |
| Multi-phase adaptive regret→0 | yes (E1) | pick one static policy |

## Residual caveats

- Soft sandbox used for agent in this experiment; Restricted/prod grant path is separate (`sandbox-policy-profile.sh`).
- Short hit harness adaptive rows remain non-citeable; marathon is SSOT.
- Redis maxmemory headroom ≈ idle `used_memory` + data budget; absolute hit% move with headroom — cite deltas / qualitative contrast.
- A18 AUTOPROMOTE stays **OFF** in production docs; enabled only for this experiment.
- Memtier absolute ops/s drift with host load; cite ratios.

## How to re-run

```bash
export AURA_REDIS_DENY_PLUGIN=1
./scripts/build-native.sh
./scripts/bench-diff-vs-redis.sh
# pieces:
python3 scripts/bench_regret.py phase_marathon
python3 scripts/bench_poison_vs_redis.py
python3 scripts/bench_explain_canary_capture.py
```

CI: document manual / non-blocking — `scripts/ci-bench.sh` runs regret packs; 
full diff compare is opt-in via `AURA_REDIS_CI_DIFF=1` (see `ci-bench.sh`).

_Artifacts dir: `/tmp/aura-diff-honesty-1923089`_
