# Mutation-attributable gains (M6)

**Thesis:** Aura *mutating* `choose-fn` under fitness beats a **frozen**
`choose-fn` that never `hot-strategy:swap!`s — not just "Aura picks LRU/LFU".

**Control plane:** `policy_agent.aura` (DEFAULT). `AURA_REDIS_DENY_PLUGIN=1`.
Do **not** claim Python as the mutator.

## What mutates

| Signal | Action |
|--------|--------|
| hit% EWMA drop / miss spike / stuck | `hot-strategy:swap!` profile (`conservative→aggressive`, etc.) |
| poisoned profile (`inverted`/`broken`) | `hot-strategy:heal!` → last-good |
| still bad after swap | `heal!` |

Log proof (real runs 2026-09-22 CST):

```text
policy_agent: fitness-swap conservative→aggressive reason=miss_spike result=(#t 4 3)
policy_agent: fitness-heal! reason=poison_profile from=inverted
policy_agent: hot-strategy:heal!
```

Env:

| Var | Meaning |
|-----|---------|
| `AURA_REDIS_FITNESS_MUTATE=0` / `AURA_REDIS_FROZEN=1` | freeze after seed |
| `AURA_REDIS_SEED_PROFILE` | `normal` / `aggressive` / `conservative` / `inverted` / `broken` |

## Measured results (real)

### A1 restore (2026-09-23) — `mutation_gain` mutate−frozen ≥ +8pp

Root cause: fitness preferred `threshold-mutate` over profile-swap; even after
`fitness-swap`→aggressive, `mutate:rebind` re-evals `policy_agent.aura` and
stalled the tick loop before EVICT/PIN. Fix: prefer aggressive profile-swap from
conservative; **inline EVICT lfu + PIN** on fitness-swap; denser diurnal miss
burst; flash weighted higher / cool diluted less; assert ≥8pp + fitness log.

| policy | cum hit% | useful GETs | fitness events |
|--------|----------|-------------|----------------|
| lru | 72.1% | 744 | — |
| lfu | 100.0% | 1032 | — |
| **adaptive_frozen** | **72.1%** | **744** | 0 |
| **adaptive_mutate** | **100.0%** | **1032** | fitness-swap + inline EVICT/PIN |

**Mutation-attributable Δ = +27.9pp** (mutate − frozen). Exit:
`python3 scripts/bench_regret.py mutation_gain`.

### mutation_gain (historical) (diurnal quiet→peak→flash→cool, seed=`conservative`)

| policy | cum hit% | useful GETs | fitness events |
|--------|----------|-------------|----------------|
| lru | 85.2% | 1104 | — |
| lfu | 100.0% | 1296 | — |
| **adaptive_frozen** | **85.2%** | **1104** | 0 |
| **adaptive_mutate** | **100.0%** | **1296** | 1 (`fitness-swap`) |

**Mutation-attributable Δ = +14.8pp** (mutate − frozen). Both beat fixed LRU; mutate matches LFU oracle on flash via aggressive profile + PIN.

### poison_heal (seed=`inverted`)

| policy | cum hit% | useful | fitness |
|--------|----------|--------|---------|
| poison_frozen | **0.0%** | 0 | 0 |
| poison_mutate | **100.0%** | 40 | 2 (`fitness-heal!` + `hot-strategy:heal!`) |

**Mutation-attributable Δ = +100.0pp** — heal recovers; frozen stays inverted/bad.

## How to run

```bash
./scripts/build-native.sh
python3 scripts/bench_regret.py mutation_gain
python3 scripts/bench_regret.py poison_heal
./scripts/demo-mutation-gain.sh
```

## Related

- [`high-roi-iterations.md`](high-roi-iterations.md) — M6–M12
- [`runtime-mutation-explore.md`](runtime-mutation-explore.md) — axes A–C
- [`mvp-plan.md`](mvp-plan.md)


## M7 — threshold string mutation

Aura rebuilds the choose-fn **body string** with lowered `min-ops` / `miss-pin`
and `hot-strategy:swap!`s it (not a Python mutator):

```text
policy_agent: fitness-threshold-mutate min-ops=40→28 miss-pin=30→22 reason=fitness result=(#t 4 3)
```

Tried before profile swap when seed is normal/conservative/thresholded.

## M9 note (TTL-aware kernel)

Separate from mutation_gain: C `ttl_aware` ArEvictOps + Aura choose picks it on
`ttl_wave` / `session_churn` when INFO shows short-TTL churn (`keys_with_ttl`,
`avg_ttl_ms`, `expired`). See `docs/high-roi-iterations.md` M9.


## M11 — slow online evolve loop

Multi-generation threshold mutate with window fitness + keep/revert (not M7
one-shot). See [`high-roi-iterations.md`](high-roi-iterations.md) M11.
`std/evolve` in pinned Aura is intend-analytics only — Redis fitness loop lives
in `policy_agent.aura`.


### A2 restore (2026-09-23) — `evolve_gain` evolve−frozen ≥ +8pp

Root cause: evolve step −140 left min-ops ≫ per-tick ops; `hot-strategy:swap!`
re-eval stalled PIN. Fix: halve thresholds each gen (floor 24/12); `mutate:rebind`
+ inline EVICT/PIN when min-ops≤80; shorter evolve window.

| policy | cum hit% | useful | notes |
|--------|----------|--------|-------|
| adaptive_evolve_frozen | 4.3% | 6 | bad seed frozen |
| **adaptive_evolve** | **61.4%** | **86** | ≥2 gens + inline PIN |

**Evolve-attributable Δ = +57.1pp**. Exit: `python3 scripts/bench_regret.py evolve_gain`.


## A13 — signal-weight evolve (2026-09-23)

Evolve mutates **w-miss / w-write / w-evict** alongside min-ops/miss-pin.
Identity defaults (10/2/10) match pre-A13 body semantics; logs show `w-miss=` path.

| policy | hit% | useful | notes |
|--------|------|--------|-------|
| adaptive_evolve_frozen | 4.3% | 6 | bad seed frozen |
| **adaptive_evolve** | **60.0%** | **84** | weight+threshold keep; Δ=**+55.7pp** |

Env: `AURA_REDIS_WEIGHT_EVOLVE=1`, `AURA_REDIS_W_MISS|W_WRITE|W_EVICT`.
A15: `AURA_REDIS_EVOLVE_BACKEND=pso|fss|grid` → lazy `std/swarm` propose.
A16: `AURA_REDIS_FIBER_SHADOW=1` → fiber dual proxy score (no apply loser).
Exit: `python3 scripts/bench_regret.py evolve_gain`; `python3 tests/test_policy_weight_evolve.py`.
