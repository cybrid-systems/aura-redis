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

### mutation_gain (diurnal quiet→peak→flash→cool, seed=`conservative`)

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
