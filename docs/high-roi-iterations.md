# High-ROI iterations (M6–M12)

Ordered by **impact × Aura-mutation story × effort**. Aura `policy_agent` stays
DEFAULT. Do not grow aura-grok for Redis-shaped prims.

| ID | Theme | Status | Exit |
|----|-------|--------|------|
| **M6** | Fitness-driven hot-strategy swap/heal + mutation_gain vs frozen | **DONE** | +14.8pp mutation_gain; +100pp poison_heal; log proof swap/heal |
| **M7** | Threshold string mutation (min-ops / ratios in choose-fn body) | **DONE** (with M6 follow-on) | `fitness-threshold-mutate min-ops=40→28` via hot-strategy:swap! of rebuilt body |
| **M8** | `diurnal_shift` harness (quiet→peak→flash→cool) | **DONE** (with M6) | used as mutation_gain workload; regret vs fixed |
| **M9** | TTL-aware kernel + SET EX / EXPIRE / TTL | **DONE** | `ttl_aware` beats LRU on `ttl_wave`; Aura `EVICT … → ttl_aware` |
| **M10** | Soft-goal choose (hit% s.t. evict CPU) | **DONE** | refuse `lfu`/+pin when `erate≥20`; `flash_churn` soft vs LFU/nosoft |
| **M11** | Slow online evolve loop (thresholds × gens) | **DONE** | multi-gen keep/revert; evolve vs frozen on `evolve_gain` |
| **M12** | Per-prefix policy namespace | **DONE** | POLICY a:/b:; prefix vs global +73.2pp on `prefix_mix` |

## How to run M12

```bash
./scripts/build-native.sh
python3 scripts/bench_regret.py prefix_mix
# or:
python3 scripts/bench_dynamic_evict.py --workloads prefix_mix \
  --policies lru,lfu,adaptive,adaptive_prefix
```

RESP `POLICY <prefix> <profile>` stores hints; INFO `policy_hints` (e.g.
`a:=session;b:=zipf`). Aura `policy_agent` pins those prefixes and overrides
choose toward `lfu|flat|pin` / `ttl_aware|flat|pin` (DENY_PLUGIN).

Measured (2026-09-22 CST, Aura `policy_agent`, `prefix_mix`, maxmemory=120k):

| policy | hit% | useful | a: hit% | b: hit% | notes |
|--------|------|--------|---------|---------|-------|
| lru | 0.0% | 0 | 0% | 0% | |
| lfu | 100% | 56 | 100% | 100% | fixed oracle |
| **adaptive** (global) | **26.8%** | **15** | 14% | 39% | pins hot/z only |
| **adaptive_prefix** | **100%** | **56** | **100%** | **100%** | PIN a: + b: |

**Prefix-attributable Δ = +73.2pp** useful hit% vs global adaptive. Log:
`policy_agent: prefix-policy hints=a:=session;b:=zipf → lfu|flat|pin` +
`PIN prefix-policy a:/b:`.

## How to run M11


```bash
./scripts/build-native.sh
python3 scripts/bench_regret.py evolve_gain
# or:
python3 scripts/bench_dynamic_evict.py --workloads evolve_gain \
  --policies lru,lfu,adaptive_evolve_frozen,adaptive_evolve
```

`std/evolve` in pinned Aura is intend-analytics strategy evolution (not Redis
fitness) — M11 implements a **slow online evolve loop** in `policy_agent`:

1. Seed **bad** thresholds (`min-ops=900`, `miss-pin=75`) → choose rarely fires
2. Each generation: window fitness (hit% EWMA) → mutate thresholds down →
   `hot-strategy:swap!` trial body → **keep** if fitness ≥ champion else **revert**
3. Log proof: `policy_agent: evolve gen=N fitness=F action=keep|revert|baseline|propose`

Distinct from M7 one-shot `fitness-threshold-mutate` (single step, no loop).

Env: `AURA_REDIS_EVOLVE=1`, `AURA_REDIS_THRESH_MIN_OPS`, `AURA_REDIS_THRESH_MISS_PIN`,
`AURA_REDIS_EVOLVE_MAX_GENS`, `AURA_REDIS_EVOLVE_WINDOW`.

Measured (2026-09-22 CST, Aura `policy_agent`, `evolve_gain`, maxmemory=120k):

| policy | hit% | useful GETs | notes |
|--------|------|-------------|-------|
| lru | 11.4% | 16 | static |
| lfu | 100.0% | 140 | oracle |
| **adaptive_evolve_frozen** | **10.0%** | **14** | bad min-ops=900 frozen |
| **adaptive_evolve** | **42.9%** | **60** | multi-gen keep; r3–r4 = 100% |

**Evolve-attributable Δ = +32.9pp** (evolve − frozen). Log proof: `evolve gen=N
fitness=F action=baseline|propose|keep` (≥2 gens). Late rounds match LFU after
thresholds drop (900→760→620→…).

## How to run M10


```bash
./scripts/build-native.sh
python3 scripts/bench_regret.py flash_churn
# or:
python3 scripts/bench_dynamic_evict.py --workloads flash_churn \
  --policies lfu,lru,adaptive_nosoft,adaptive_soft
```

Soft-goal rule (documented in `choose_normal.aura` / `choose_aggressive.aura`):

- `evict_rate_pct = (devicted * 100) / ops` (INFO `evicted` delta / window ops)
- budget **20** (aggressive **18**, conservative **25**): refuse expensive `lfu`|+`pin`
- prefer `ttl_aware|flat|soft` if TTL pressure, else `lru|flat|soft`
- A/B: `adaptive_soft` (= `normal`) vs `adaptive_nosoft` (pre-soft body, no `|soft`)

Measured (2026-09-22 CST, Aura `policy_agent`, `flash_churn`, maxmemory≈90k):

| policy | hit% | useful GETs | evicted | notes |
|--------|------|-------------|---------|-------|
| lfu | 12.5% | 50 | 578 | session thrash |
| lru | 0.0% | 0 | 578 | keep looks oldest |
| adaptive_nosoft | **100%** | **400** | 873 | TTL rule → ttl_aware (no soft log) |
| **adaptive_soft** | **100%** | **400** | 873 | soft-goal refuse erate≈50 → ttl_aware |

Log proof: `policy_agent: soft-goal refuse expensive (erate=50 …)` then
`EVICT lfu → ttl_aware`. Soft vs LFU: **+87.5pp** useful hit%.

## How to run M9

```bash
./scripts/build-native.sh
python3 scripts/bench_regret.py ttl_wave
# or:
python3 scripts/bench_dynamic_evict.py --workloads ttl_wave --policies lru,lfu,ttl_aware,adaptive
```

Signals (INFO → choose-fn): `expired` delta, `keys_with_ttl`, `avg_ttl_ms`.

Measured (2026-09-22 CST, Aura `policy_agent` frozen-seed `normal` on ttl_wave):

| policy | hit% | useful GETs | notes |
|--------|------|-------------|-------|
| lru | **0.0%** | 0 | sessions look newest |
| lfu | 37.5% | 180 | sessions GET-boosted |
| **ttl_aware** | **100%** | **480** | expire-soon-first |
| **adaptive** | **100%** | **480** | `EVICT lru → ttl_aware` |

Log proof: `policy_agent: EVICT lru → ttl_aware (… avg_ttl=3967 keys_ttl=120)`.

C path: `SET key val EX sec`, `EXPIRE`, `TTL`, lazy expire on GET, `EVICT ttl_aware`.

## How to run M6

```bash
./scripts/demo-mutation-gain.sh
python3 scripts/bench_regret.py mutation_gain
python3 scripts/bench_regret.py poison_heal
```

## Pointers

- [`mutation-gains.md`](mutation-gains.md)
- [`runtime-mutation-explore.md`](runtime-mutation-explore.md)
- [`mvp-plan.md`](mvp-plan.md)
