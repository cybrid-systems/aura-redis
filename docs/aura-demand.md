# Aura demand map for aura-redis

**Status:** authoritative (Aura differentiation track)  
**Tip baseline:** ~`c1e7abb` (main)  
**Scope:** What a Redis-*compatible* cache uniquely needs from **Aura** — not generic Redis feature parity.  
**Companions:** [`aura-redis-match.md`](aura-redis-match.md) · [`aura-pain-scenarios.md`](aura-pain-scenarios.md) · [`aura-native-control.md`](aura-native-control.md) · [`mutation-gains.md`](mutation-gains.md) · [`runtime-mutation-explore.md`](runtime-mutation-explore.md) · [`high-roi-iterations.md`](high-roi-iterations.md) · [`mvp-plan.md`](mvp-plan.md) · [`architecture.md`](architecture.md) · [`workloads.md`](workloads.md) · [`production-plan.md`](production-plan.md) · [`perf-eval.md`](perf-eval.md)

**See also matching matrix:** [`aura-redis-match.md`](aura-redis-match.md) — comprehensive Aura capability catalog ↔ Redis production pain matrix (STRONG/MEDIUM/WEAK/NONE + anti-matches). Broader than this demand-mining doc; use it when judging fit honesty.

**Scenario deep-dive:** [`aura-pain-scenarios.md`](aura-pain-scenarios.md) — per-pain ops narrative + how Aura solves (Layer A native → B stdlib → C kernels), verify exits tied to A1–An.

**Invariant:** Redis-specific work stays in **this** repo. Aura compiler/runtime opts must stay **generic** (language-wide). Do not propose Redis-shaped Aura core patches unless framed as generic primitives Redis happens to use. `AURA_REDIS_DENY_PLUGIN=1` for Aura-native demos — `PLUGIN` / `.so` is escape hatch, not moat.

---

## 1. Thesis

Redis-compatible cache (RESP, maxmemory kernels, string/typed KV) is **table stakes**. Aura-redis value is a **closed-loop control plane** — `policy_agent.aura` under sandbox — that **mutates live `choose-fn` code** (`std/hot-strategy` + `mutate:*`) against INFO / workload signals, then applies named C kernels via RESP `EVICT` / `LAYOUT` / `PIN` / `POLICY`. The citeable win is **multi-phase regret / hit quality** (`phase_marathon`: adaptive **100%** cum / **0** regret vs fixed LRU/LFU each collapsing on a different phase — see [`perf-eval.md`](perf-eval.md)), **not** memtier ops/s. Vanilla Redis offers static `CONFIG` + manual ops; RedisGears / modules / sidecar Python can script rules but cannot **sandbox-bounded live code mutation + heal + fitness evolve** of the policy itself. That closed loop is the product moat.

---

## 2. Capability map

| Aura capability | Redis-facing use | Status | Evidence |
|-----------------|------------------|--------|----------|
| **Sandbox + deny-plugin** | Force Aura-native adaptation; no silent `.so` path | **SHIPPED** | `AURA_REDIS_DENY_PLUGIN=1`; `scripts/sandbox-policy-profile.sh`; `tests/test_aura_native.py` |
| **hot-strategy `register!` / `swap!`** | Rebind `choose-fn` body strings under load | **SHIPPED** | `src/redis/policy_agent.aura`; `src/redis/policy/choose_*.aura`; `tests/test_hot_strategy_policy.aura` |
| **hot-strategy `heal!` + mutate snapshot** | Last-good recovery from broken/inverted policy | **SHIPPED** | `poison_heal` Δ=+100pp; demo invert→heal; `mutate:safety-snapshot` / `mutate:boundary-safe?` |
| **Fitness-driven choose / profile swap** | Miss spike / EWMA drop → `conservative→aggressive` etc. | **SHIPPED** (stretch flaky) | [`mutation-gains.md`](mutation-gains.md) historical +14.8pp; **recent [`perf-eval.md`](perf-eval.md) `mutation_gain` Δ=0** → open demand |
| **Threshold mutate (body-string rebuild)** | Lower `min-ops` / `miss-pin` via `swap!` of rebuilt lambda | **SHIPPED** | M7 `fitness-threshold-mutate min-ops=40→28` logs |
| **Evolve loop (multi-gen keep/revert)** | Bad seed thresholds → propose/trial/keep or revert | **SHIPPED** (stretch flaky) | M11; historical evolve Δ=+32.9pp; **recent `evolve_gain` Δ=0** → open demand |
| **Per-prefix `POLICY`** | Multi-tenant hint → pin/override choose | **PARTIAL** | M12 `prefix_mix` +73pp; hints only — not isolated choose-fn / budgets |
| **Layout migrate (`flat` ↔ `hot_cold`)** | Joint EVICT+LAYOUT from choose string | **SHIPPED** | Agent owns layout; C `LAYOUT`; leave `AURA_REDIS_LAYOUT_ADAPTIVE` **off** when agent runs |
| **PIN / protect** | Skip hot keys under flood | **SHIPPED** | `PIN` / `UNPIN`; auto-pin on `\|pin`; `EVICT samples` bump |
| **TTL-aware kernel + signals** | Expire-soon-first under TTL waves | **SHIPPED** | C `ttl_aware`; INFO `keys_with_ttl` / `avg_ttl_ms` / `expired`; `ttl_wave` ~100% |
| **Soft-goal (hit% s.t. evict CPU)** | Refuse `lfu`/+pin when `erate` over budget | **SHIPPED** | M10 `flash_churn`; `\|soft` log; +87pp vs LFU |
| **Diurnal / regime harness** | quiet→peak→flash→cool | **SHIPPED** | `diurnal_shift` / `mutation_gain` workload |
| **Poison recovery** | Inverted/broken seed → heal | **SHIPPED** | `poison_heal` PASS (+100pp) |
| **Multi-phase regret oracle** | Cumulative useful-GET hit% + regret vs per-phase best fixed | **SHIPPED** | `phase_marathon` / `scripts/bench_regret.py` — **headline scoreboard** |
| **Policy HA reconnect** | Backoff, re-apply last EVICT/LAYOUT, policy-pin, heartbeat | **SHIPPED** | P1.9; fail-safe = C keeps last kernel |
| **In-process Aura server as data plane** | Pure Lisp RESP path | **GAP** (intentional) | ~70–110× slower than C (`architecture.md`); C data plane is product |
| **FFI + closure co-location** | hot-strategy in same process as `(c-func)` soup | **GAP** (Aura rev) | Closures inert after many `c-func` → **split process** required (`aura-native-control.md`) |
| **`mutation_gain` non-zero under harder oracle** | Mutate ≫ frozen on diurnal flash | **GAP** | `perf-eval.md`: mutate=frozen 85.2%, Δ=0 (assert FAIL) |
| **`evolve_gain` non-zero** | Evolve ≫ frozen bad thresholds | **GAP** | `perf-eval.md`: evolve=frozen 10%, Δ=0 (need ≥8pp) |
| **Typed (HASH/LIST/ZSET) in policy** | Type-aware eviction/layout pressure | **GAP** | P3.16 types in C; choose-fn / INFO have **no** type-mix signals |
| **Multi-tenant policy isolation** | Per-prefix choose-fn / pin budget / sandbox | **GAP** | `POLICY` is hint-level override, shared global choose-fn |
| **Mutation audit trail** | Ops-visible why/when body changed | **GAP** | Logs exist ad-hoc; no durable audit ring / RESP query |
| **Canary / rollback of choose-fn** | Trial apply before commit; timed rollback | **GAP** | `heal!` is last-good only; no canary window / A/B |
| **A/B shadow traffic** | Dual policy on sampled GETs | **GAP** | Not in data plane; needs C sample hook or dual agent |
| **Explainability of EVICT/LAYOUT flips** | Structured reason codes for ops | **PARTIAL** | Agent logs signals inline; no `INFO policy_explain` / stable schema |
| **Policy version pin across replica failover** | Same choose generation on promote | **GAP** | P2.14 replica exists; `hot-strategy:version` is agent-local, not replicated |
| **Controller cost vs hit-quality** | Auto-freeze when gain < overhead | **GAP** | Manual `AURA_REDIS_FROZEN=1` / `AURA_REDIS_FITNESS_MUTATE=0` only |
| **`hot_cold` threshold via Aura** | Promote/demote knobs mutated live | **GAP** | C soft-cap ~nkeys/4; not RESP-tunable from agent |
| **W-TinyLFU / SLRU named kernels** | Better zipf admission | **GAP** | Explore backlog; Aura would only *select* the name |

Statuses: **SHIPPED** = demo/bench green on main; **PARTIAL** = works but shallow vs demand; **GAP** = missing or recent stretch Δ=0.

---

## 3. Demand mining

Only demands where **Aura live mutation / sandbox / hot-strategy / fitness** is the differentiator vs vanilla Redis, Redis+sidecar Python, RedisGears, or modules.

### A. Workload regime shifts (zipf ↔ scan, diurnal, flash sale, poison)

- **Who feels it:** SRE / cache platform; app owners under 大促 / feed reshuffle / abuse scans.
- **Why Redis alone fails:** `maxmemory-policy` is a static knob; changing it is ops ticket + restart risk; no code-level policy that *reacts and heals*.
- **Aura-shaped solution:** Fitness `hot-strategy:swap!` of choose-fn profiles + threshold mutate + `heal!` on poison; joint `EVICT`+`LAYOUT`+`PIN` in one tick.
- **Measurable exit:** `phase_marathon` regret_hits=0 (already); **restore** `mutation_gain` mutate−frozen ≥ +8pp under tightened diurnal; keep `poison_heal` as CI gate.
- **Priority (Aura track):** **P0**.

### B. Multi-tenant / prefix isolation

- **Who feels it:** Multi-tenant cache / Redis-as-a-Service; noisy-neighbor tenants.
- **Why Redis alone fails:** Shared `maxmemory-policy`; ACLs don’t isolate eviction strategy; modules don’t give per-prefix *mutable code*.
- **Aura-shaped solution:** Deepen `POLICY`: per-prefix choose body (or parameter namespace) under sandbox; pin budgets; optional per-prefix fitness — still one agent, isolated strategy names via `hot-strategy` multi-register.
- **Measurable exit:** Extend `prefix_mix` with *conflicting* optima (tenant A wants `lfu`+pin, B wants `ttl_aware`) where global adaptive loses and prefix-isolated mutate wins ≥ +20pp on worse tenant.
- **Priority:** **P1**.

### C. Safety of self-modifying policy

- **Who feels it:** Sec / SRE approving “live code change” in prod.
- **Why Redis alone fails:** N/A (they don’t mutate policy code); modules = process-risk reload.
- **Aura-shaped solution:** Already: `mutate:boundary-safe?`, `mutate:safety-snapshot`, `heal!`, `DENY_PLUGIN`, no `effect:ffi` in agent. **Demand:** durable mutation audit; canary choose-fn (trial N ticks then commit or heal); optional dual-register shadow.
- **Measurable exit:** `tests/test_policy_canary.py` — bad trial auto-heals within T ticks; audit of last K mutations queryable; Restricted sandbox + `network` grant path documented (`sandbox-policy-profile.sh`).
- **Priority:** **P0** (audit + canary) for production trust.

### D. Closing `mutation_gain` / `evolve_gain` to non-zero

- **Who feels it:** Product / docs (claim “mutation-attributable gain”); CI stretch gates.
- **Why Redis alone fails:** No mutate-vs-frozen story at all.
- **Aura-shaped solution:** Harder oracles + longer windows so frozen conservative *cannot* luck into flash LFU; evolve must keep ≥1 generation improvement; optionally mutate signal *weights*, not only thresholds (`build-threshold-body` / evolve body in `policy_agent.aura`).
- **Measurable exit:** `python3 scripts/bench_regret.py mutation_gain` assert mutate−frozen ≥ +8pp; `evolve_gain` ≥ +8pp; both green in CI (or explicit stretch job). Treat current Δ=0 as **open demand**, not “mutation useless” (`phase_marathon` + `poison_heal` remain citeable).
- **Priority:** **P0**.

### E. Observability — why EVICT/LAYOUT flipped

- **Who feels it:** Ops on-call; “why did we leave LFU?”.
- **Why Redis alone fails:** CONFIG changes are human; no policy rationale stream.
- **Aura-shaped solution:** Structured `policy_explain` (reason enum + signals + profile + `hot-strategy:version`) on each apply; export via INFO section / heartbeat file.
- **Measurable exit:** Parseable explain lines in `phase_marathon` log; test asserts reason ∈ {miss_spike, soft_goal, ttl_pressure, prefix_policy, fitness_swap, evolve_keep, …}.
- **Priority:** **P1**.

### F. Typed data (HASH/LIST/ZSET) eviction/layout pressure

- **Who feels it:** Apps with big HASH/ZSET (feature store, leaderboard, session blobs).
- **Why Redis alone fails:** Policy still string-centric; type mix doesn’t drive eviction choice — and Redis still won’t *mutate code* when types shift.
- **Aura-shaped solution:** C exports type memory shares / bigkey signals in INFO; Aura choose may prefer layout/pin/ttl rules under typed pressure — **kernels stay C**; policy stays Aura. (Gaming ZSET-rank kernel remains future named C ops.)
- **Measurable exit:** New `typed_pressure` harness (large HASH flood + hot string set); adaptive with type signals ≥ fixed on protected set.
- **Priority:** **P2** (types exist P3.16; policy leverage missing).

### G. Replica / TLS / prod deploy — policy version across failover

- **Who feels it:** Deploy / HA owners.
- **Why Redis alone fails:** Replica copies data not “which mutable policy generation”.
- **Aura-shaped solution:** Pin `hot-strategy:version` + profile hash into heartbeat / optional note; on promote, agent re-seeds from pin or stays on last C kernel until agent catches up (fail-safe already: C keeps last `EVICT`).
- **Measurable exit:** Extend `tests/test_prod_policy_ha.py`: kill primary agent, promote replica, new agent resumes same profile version or documents empty→INFO bootstrap.
- **Priority:** **P1**.

### H. Cost — controller overhead vs hit-quality gain

- **Who feels it:** Platform cost owners; dense multi-tenant nodes.
- **Why Redis alone fails:** No controller; our agent INFO-polls every `AURA_REDIS_POLICY_MS`.
- **Aura-shaped solution:** Aura policy that **mutates itself into a freeze** when EWMA gain < threshold (meta-policy): `swap!` → conservative + fitness-off, or lengthen tick — still Aura, still heal-able.
- **Measurable exit:** Bench: under stable single-regime load, auto-freeze reduces INFO/apply rate ≥ 5× with hit% within 2pp of always-on.
- **Priority:** **P2**.

### I. What should NOT be Aura

- **Dumb kernels** (`lru` / `lfu` / `ttl_aware` / future SLRU): stay C `ArEvictOps`.
- **RESP parse, epoll, dict, typed containers:** stay C (`aura_redis_server`).
- **Throughput race with Redis:** not the Aura story (`perf-eval` already ~1.0–1.14× Redis on memtier — hygiene, not moat).
- **Redis Cluster / slot migration:** out of scope (production anti-goal).
- **Aura core Redis prims:** forbidden; generic Fiber / mutate / sandbox improvements only if language-wide.

---

## Contrast: what others already do

| Approach | Can adapt eviction? | Live **code** mutation under sandbox? | Heal / fitness evolve of policy? |
|----------|---------------------|----------------------------------------|----------------------------------|
| Vanilla Redis `CONFIG` | Manual / static | No | No |
| Redis + sidecar Python | Yes (RESP / CONFIG) | Process restart / deploy | Ad-hoc scripts; no AST heal |
| RedisGears / functions | Event scripts | Reload unit; not hot-strategy denseness | Limited |
| Redis modules / `PLUGIN.so` | Yes | dlopen risk; our escape hatch | No Aura heal story |
| **Aura-redis `policy_agent`** | Yes via RESP | **Yes** (`hot-strategy` + mutate) | **Yes** (fitness / evolve / heal) |

Keep backlog items only in the bottom row’s unique column.

---

## 4. Ranked backlog (Aura differentiation track)

Priorities here are **P0–P2 for Aura differentiation**, independent of Redis compatibility P0–P3 (those are **DONE** on [`production-plan.md`](production-plan.md)).

| ID | Item | Scale | Exit test | Depends | Start? |
|----|------|-------|-----------|---------|--------|
| **A1** | **Close `mutation_gain` Δ>0** — longer flash phase / colder conservative seed / require fitness-swap log | 1–2d | `python3 scripts/bench_regret.py mutation_gain` mutate−frozen ≥ +8pp + fitness-swap log | — | **START NEXT** |
| **A2** | **Close `evolve_gain` Δ>0** — window/gen tuning; keep≥1 gen; assert evolve logs | 1–2d | `python3 scripts/bench_regret.py evolve_gain` ≥ +8pp + ≥2 evolve gens | — | **START NEXT** |
| **A3** | **Mutation audit + explain schema** — ring of {ts, op, from, to, reason, version}; INFO or heartbeat | 1–2d | New `tests/test_policy_audit.py`; explain reasons on `phase_marathon` | — | **START NEXT** |
| **A4** | Canary choose-fn — trial body N ticks; auto `heal!` if fitness drops | 2–3d | `tests/test_policy_canary.py` | A3 | |
| **A5** | Deep prefix isolation — per-prefix choose body or param bag via multi `register!` | 2–3d | Conflicting-optima `prefix_mix_v2` ≥ +20pp on victim tenant | M12 | |
| **A6** | Policy version pin across replica promote | 1–2d | Extend `tests/test_prod_policy_ha.py` + replica | P2.14, P1.9 | |
| **A7** | Typed pressure signals in INFO + choose | 2–3d | `typed_pressure` harness PASS | P3.16 | |
| **A8** | Auto-freeze meta-policy (cost gate) | 1–2d | Stable-load INFO rate ↓ ≥5×; hit% within 2pp | A1 | |
| **A9** | `hot_cold` promote/demote RESP knobs + Aura mutate | 1–2d | Microbench large-value locality | layout | |
| **A10** | Shadow / A/B sample path (C hook or dual agent) | 2–3d | Shadow regret report without applying loser | A4 | |
| **A11** | Restricted sandbox + `effect:network` grant (prod-shaped) | 1d | Doc + demo without `AURA_SANDBOX=off` when TA available | sandbox profile | |
| **A12** | Named kernel `slru` or approx TinyLFU (C) — Aura select only | 2–3d | zipf regret ≤ LFU | explore | |
| **A13** | Signal-weight evolve (not only min-ops) | 1–2d | `mutation_gain` under weight evolve ≥ threshold path | A1–A2 | |
| **A14** | Controller overhead dashboard (applies/sec, swap rate vs hitΔ) | 0.5–1d | Heartbeat fields + bench summary | A3 | |
| **A15** | **Swarm/FSS/PSO evolve backend** — replace/augment hand threshold walk with `std/swarm` | 2–3d | `evolve_gain` ≥ +8pp + swarm gen logs | A2 | |
| **A16** | Agent-side fiber parallel trial fitness (canary/shadow score without C hook) | 1–2d | Dual-body score in logs; no apply of loser | A4, A10 | |

**Top 3 start next:** **A1**, **A2**, **A3**.

Do **not** implement A1 in this doc-only change set unless trivially documentation.

---

## 5. Anti-goals

- Full Redis Cluster / slot migration / Cross-slot multi-key as a ship gate.
- Competing on raw ops/s as the product claim (memtier is hygiene, not moat).
- Growing Aura core (`aura-grok` / pinned Aura) for Redis-only primitives.
- Re-elevating **PLUGIN** / `.so` reload as the adaptation moat (`AURA_REDIS_DENY_PLUGIN=1` stays on for demos).
- Moving RESP parse, dict, or eviction *kernels* into Aura Lisp.
- Claiming single-phase hit% or memtier adaptive wins as the citeable story.
- Treating recent `mutation_gain` / `evolve_gain` Δ=0 as “mutation doesn’t matter” — they are **open demand signals**, while `phase_marathon` + `poison_heal` remain citeable.

---

## 6. Track relationship

```text
  Production P0–P3 (DONE)     = ship a correct single-node RESP cache/KV
  Aura differentiation (THIS) = prove sandbox + mutate + hot-strategy unique value
```

Wire-in:

- [`production-plan.md`](production-plan.md) — “Aura differentiation track” pointer.
- [`iteration-plan.md`](iteration-plan.md) — post-P3 active track → this doc.
- Execution detail for M6–M12 remains [`high-roi-iterations.md`](high-roi-iterations.md); new work uses **A1–A14** IDs above.

---

## Changelog

| Date (CST) | Notes |
|------------|-------|
| 2026-09-23 | Link [`aura-pain-scenarios.md`](aura-pain-scenarios.md) scenario deep-dive.
| 2026-09-23 | Link [`aura-redis-match.md`](aura-redis-match.md); add **A15** swarm/FSS evolve backend, **A16** fiber trial fitness from matching pass. |
| 2026-09-23 | Initial authoritative demand map from deep read of control / mutation / MVP / arch / workloads / prod / perf + `policy_agent.aura` / `policy/*.aura` / regret benches. |
