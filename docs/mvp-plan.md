# aura-redis MVP plan (M0–M5)

**Status:** M0 freeze — executable demo track (collapses explore iters 10–14).  
**Companion:** [`iteration-plan.md`](iteration-plan.md) · [`runtime-mutation-explore.md`](runtime-mutation-explore.md) · [`aura-native-control.md`](aura-native-control.md) · [`workloads.md`](workloads.md)

---

## MVP goal (one sentence)

Under Meta/Twitter-shaped Zipf + phase shifts, **Aura-mutated policy** (multi-signal choose → joint `EVICT`+`LAYOUT`, optional pin) **beats static LRU on hit%**, with a **~2-minute demo** anyone can run (`scripts/demo-mvp.sh`).

Product rule: **Aura mutates policy under sandbox; C only runs named kernels.** `AURA_REDIS_DENY_PLUGIN=1`.

---

## Phases

| Phase | Theme | Exit |
|-------|--------|------|
| **M0** | Plan freeze (this doc) | Doc landed; iteration-plan points here ✅ |
| **M1** | Multi-signal INFO + choose-fn API | INFO exposes `keys` / `evicted` / samples; choose returns `evict` or `evict\|layout` (+ optional `pin`); bodies: normal / aggressive / conservative |
| **M2** | Joint EVICT+LAYOUT | One Aura/Python tick applies both; C `AURA_REDIS_LAYOUT_ADAPTIVE` **off** when agent owns layout |
| **M3** | Industry harness | ≥ `zipf_hotkey` + keep/extend oscillate; regret vs best-fixed table in bench output |
| **M4** | Hot-key PIN + `EVICT samples N` | Explicit PIN set skipped by eviction; samples tunable (default 16); auto-pin when policy says `pin` |
| **M5** | Distinctive demo MVP | `scripts/demo-mvp.sh` (~2 min): LRU lose → adaptive win → WS shift → optional invert/heal; README “Demo MVP” |

---

### M1 — Multi-signal choose

**INFO (C)** adds / keeps at least: `gets`, `sets`, `hits`, `misses`, `evicted`, `keys`, `layout`, `evict`, `samples`, `pinned`, `used_memory`, `maxmemory`.

**choose-fn contract** (Aura body string / Python mirror):

```text
(lambda (dgets dsets dhits dmisses [devicted nkeys]) …)
  → ""                         # no change
  → "lfu" | "lru" | "noop"     # eviction only (backward compat)
  → "lfu|hot_cold"             # joint EVICT + LAYOUT
  → "lru|flat"
  → "lfu|hot_cold|pin"         # optional pin hint (M4)
```

| Body | Bias |
|------|------|
| `choose_normal.aura` | Balanced: write-heavy→`lfu\|hot_cold`; read-heavy+hit≥60%→`lru\|flat` |
| `choose_aggressive.aura` | Faster flips, lower min-ops, pin sooner on miss spikes |
| `choose_conservative.aura` | Higher min-ops / hit threshold; layout stays `flat` unless pressure |

### M2 — Joint apply

Agent parses `|` fields → `EVICT <name>` then `LAYOUT <name>` when present. When running with Aura/Python adaptive controller, **do not** enable pure-C `AURA_REDIS_LAYOUT_ADAPTIVE` (policy owns layout).

### M3 — `zipf_hotkey`

Zipf α≈0.99 over keyspace, small hot set, cold flood past `maxmemory`, GET hot set. Compare `lru` / `lfu` / `adaptive` (+ layout when joint). Extend `oscillate`. Document in `workloads.md`. Print regret vs best-fixed.

### M4 — PIN + samples

- RESP: `PIN key` / `UNPIN key` / `PIN` (list pinned keys)
- Eviction skips pinned entries
- `EVICT samples <n>` (default 16)
- Policy/agent may auto-pin top-N on miss spike + write flood when choice includes `pin`

### M5 — Demo MVP

Story arc in `scripts/demo-mvp.sh` (+ optional `tests/test_mvp.py`):

1. C server, `DENY_PLUGIN`, small `maxmemory`
2. Phase A Meta-like Zipf / `hot_protect` under **static LRU** → catastrophic hit%
3. Same load under **Aura adaptive** (Docker `policy_agent` if reliable; else Python mirror of `choose_*.aura`, announced) → swap `lru→lfu` (+ layout), hit% ≈100%
4. Phase B working-set shift → adaptive toward `lru` / stays optimal vs stuck LFU
5. Optional: invert policy → behavior flips; heal restores
6. Final table + one-liner: *Aura mutates policy under sandbox; C only runs kernels*

---

## Post-MVP backlog

Point to [`runtime-mutation-explore.md`](runtime-mutation-explore.md) **iters 15+**:

TTL-aware / expire-first · Soft-goal choose · hot_cold threshold mutation · `std/evolve` · W-TinyLFU-ish / GDSF · Per-prefix policy · ZSET/gaming (if data-type roadmap opens)

---

## How to run the MVP demo

```bash
./scripts/build-native.sh
./scripts/demo-mvp.sh          # ~2 min, exit 0 = LRU lose / adaptive win
python3 scripts/bench_dynamic_evict.py --workloads zipf_hotkey,oscillate
```

## Headline metric (why not single-phase or memtier)

**Primary scoreboard = multi-phase cumulative useful-GET hit% + regret vs per-phase oracle**
(`phase_marathon` / strengthened oscillate), not single-phase hit% and not memtier ops/s.

- Single-phase adaptive only *ties* the best fixed kernel — that understates the moat.
- Memtier throughput without `maxmemory` pressure does not show adaptive wins (and must not be claimed as such).
- Across zipf/hot → ws_shift → hot again, fixed LRU and fixed LFU each collapse on a different phase; adaptive stays near the oracle and **beats both** on cumulative hit% / useful GETs.

See `docs/workloads.md`, `docs/perf-eval.md`, `python3 scripts/bench_regret.py`.
