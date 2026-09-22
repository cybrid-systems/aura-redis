# High-ROI iterations (M6–M12)

Ordered by **impact × Aura-mutation story × effort**. Aura `policy_agent` stays
DEFAULT. Do not grow aura-grok for Redis-shaped prims.

| ID | Theme | Status | Exit |
|----|-------|--------|------|
| **M6** | Fitness-driven hot-strategy swap/heal + mutation_gain vs frozen | **DONE** | +14.8pp mutation_gain; +100pp poison_heal; log proof swap/heal |
| **M7** | Threshold string mutation (min-ops / ratios in choose-fn body) | **DONE** (with M6 follow-on) | `fitness-threshold-mutate min-ops=40→28` via hot-strategy:swap! of rebuilt body |
| **M8** | `diurnal_shift` harness (quiet→peak→flash→cool) | **DONE** (with M6) | used as mutation_gain workload; regret vs fixed |
| **M9** | TTL-aware kernel + EXPIRE (or stub) | pending | Aura chooses `ttl_aware` on ttl_wave; stub EXPIRE+lazy expire if full TTL hard |
| **M10** | Soft-goal choose (hit% s.t. evict CPU) | pending | refuse expensive kernel over budget |
| **M11** | `std/evolve` offline/slow online on body text | pending | evolve thresholds; heal on fitness drop |
| **M12** | Per-prefix policy namespace sketch | pending | two prefixes, two choose policies |

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
