# High-ROI iterations (M6–M12)

Ordered by **impact × Aura-mutation story × effort**. Aura `policy_agent` stays
DEFAULT. Do not grow aura-grok for Redis-shaped prims.

| ID | Theme | Status | Exit |
|----|-------|--------|------|
| **M6** | Fitness-driven hot-strategy swap/heal + mutation_gain vs frozen | **DONE** | +14.8pp mutation_gain; +100pp poison_heal; log proof swap/heal |
| **M7** | Threshold string mutation (min-ops / ratios in choose-fn body) | **DONE** (with M6 follow-on) | `fitness-threshold-mutate min-ops=40→28` via hot-strategy:swap! of rebuilt body |
| **M8** | `diurnal_shift` harness (quiet→peak→flash→cool) | **DONE** (with M6) | used as mutation_gain workload; regret vs fixed |
| **M9** | TTL-aware kernel + SET EX / EXPIRE / TTL | **DONE** | `ttl_aware` beats LRU on `ttl_wave`; Aura `EVICT … → ttl_aware` |
| **M10** | Soft-goal choose (hit% s.t. evict CPU) | pending | refuse expensive kernel over budget |
| **M11** | `std/evolve` offline/slow online on body text | pending | evolve thresholds; heal on fitness drop |
| **M12** | Per-prefix policy namespace sketch | pending | two prefixes, two choose policies |

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
