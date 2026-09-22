# Dynamic eviction workloads

These workloads are designed so **fixed LRU is not always optimal**. Aura-adaptive
eviction (`lru` ↔ `lfu` via RESP `EVICT`, driven by Aura `policy_agent.aura` —
the product control plane; Python `choose_policy` is a host-only CI mirror)
tracks the better kernel across phases.

Data plane: host `aura_redis_server` (C). Control plane: Aura policy mutation under
sandbox (`AURA_REDIS_DENY_PLUGIN=1`); `PLUGIN` is not the feature under test.

## Workloads

| Name | Favors | What happens |
|------|--------|----------------|
| `hot_protect` | **LFU** | Adaptive flips to LFU (write prelude) → insert+boost a small hot set → flood unique cold keys past `maxmemory` → GET the hot set. LRU has already aged the hot keys out; LFU keeps high-frequency keys. |
| `ws_shift` | **LRU** | Boost set A, then stop using it and drive set B under pressure. LFU clings to A and thrashes B; LRU follows the new working set. |
| `phase_marathon` | **adaptive (headline)** | One lifetime: `zipf_hotkey` → bridge → `ws_shift` → bridge → `hot_protect`. Primary scoreboard = **cumulative useful-GET hit%** + **regret vs per-phase oracle** (best fixed kernel each phase). Fixed LRU dies on zipf/hot; fixed LFU dies on ws_shift; adaptive tracks both (+ PIN). |
| `oscillate` | **adaptive** | `hot_protect` → `FLUSHDB` → read-heavy bridge (→ LRU) → `ws_shift` on one server lifetime. Adaptive should land near best-of `{lru,lfu}` on each phase. |
| `zipf_hotkey` | **LFU / pin / adaptive** | Meta-like Zipf α≈0.99 over keyspace; boost tiny hot set; cold flood past `maxmemory`; GET hot head (+ Zipf probes). Static LRU collapses; LFU/adaptive (+ optional `PIN`) retain. |

Policy rules (same as `src/redis/policy/choose_normal.aura`, joint form):

- window ops ≥ 40
- write-heavy (`sets > gets*2`) → `lfu|hot_cold` (optional `|pin` on miss spike)
- read-heavy (`gets > sets*5`) and hit% ≥ 60 → `lru|flat`
- Extra signals: `devicted`, `keys`, agent hit% EWMA

Headline regret harness: `python3 scripts/bench_regret.py` (defaults `phase_marathon,...`).

Industry wrapper: `python3 scripts/bench_industry.py` (defaults `zipf_hotkey,hot_protect,oscillate`).
Bench prints a **regret vs best-fixed** table.

## Reproduce

```bash
./scripts/build-native.sh
python3 scripts/bench_dynamic_evict.py              # Aura policy_agent DEFAULT
python3 scripts/bench_dynamic_evict.py --workloads hot_protect,ws_shift
python3 scripts/bench_dynamic_evict.py --python-ctl # host-only CI mirror
./scripts/demo-mvp.sh                               # Aura agent; fail if unavailable
```

Defaults: `--maxmemory 120000`, port `26730`, **Aura `policy_agent.aura`** adaptive
control (Docker). Python mirror via `--python-ctl` / `AURA_AGENT=0` for host-only CI.
Agent **owns layout** — do not set `AURA_REDIS_LAYOUT_ADAPTIVE`.

## Expected shape (illustrative)

On a typical host run (see `docs/perf-log.md` for a dated capture):

| workload | LRU hit% | LFU hit% | adaptive hit% |
|----------|----------|----------|---------------|
| hot_protect | ~0% | ~100% | ~100% (swap lru→lfu) |
| ws_shift | ~100% | ~35% | ~100% (stays/bridges to lru) |
| oscillate | ~98% overall (0% then 100%) | ~36% | **~100%** both phases |
| zipf_hotkey | ~0–low% | ~100% | ~100% (+ pin, 0pp regret) |
| phase_marathon | ~80% cum (fails zipf/hot) | ~45% cum (fails ws_shift) | **~100% cum, 0 regret** |

Caveats: approximate sampling eviction (16 samples); LFU freq only increments under the LFU kernel — the harness boosts the hot set **after** adaptive flips to LFU. Absolute ops/s is not the goal; hit ratio / useful target GETs is.
