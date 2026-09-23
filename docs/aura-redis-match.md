# Aura capability ↔ Redis production pain matching

**Status:** authoritative matching companion (broader than demand mining)  
**Date:** 2026-09-23 CST  
**Scope:** Match **Aura-differentiating** language/runtime **native** + stdlib capabilities to **industry-realistic** Redis operator/app pains. Honest WEAK / NONE / anti-matches required.  
**Companions:** [`aura-demand.md`](aura-demand.md) · [`aura-native-control.md`](aura-native-control.md) · [`mutation-gains.md`](mutation-gains.md) · [`architecture.md`](architecture.md) · [`workloads.md`](workloads.md) · [`production-plan.md`](production-plan.md) · [`perf-eval.md`](perf-eval.md)

**Read sources (this pass, read-only):**  
- **Native:** `/workspace/aura-grok/docs/generated/{primitives,modules,primitives-registry}.md`, `docs/agent-orchestration-status.md`, `src/core/{sandbox,capability_model,mutation,workspace_isolation,provenance_tracker,resource_quota,security_event,mutation_audit_wal}.*`, `src/compiler/{typed_mutation_audit,lowering_linear_types,dirty_propagation,aot_hot_update_health,aura_jit}.*`, `src/serve/{fiber,scheduler}.*`, `src/orch/*` (+ pinned `.deps/aura` mirrors).  
- **Stdlib:** `docs/stdlib/*`, `lib/std/{hot-strategy,mutate,evolve,agent,swarm,fss,capability,ffi,heal,atomic-swap,hot-update*}.aura`.  
- **aura-redis:** `policy_agent.aura` + existing docs. **No** edits to `/workspace/aura-grok`.

**Matching rule:** Pains that need **trusted live policy change** map first to **sandbox + capability/effect + typed-mutate + provenance + MutationBoundaryGuard + hot-strategy** — not “Lua-like scripting.” Pure data-plane pains (bigkey scan, Cluster, ops/s) stay WEAK/NONE.

---

## 1. Purpose

**Thesis:** Claim Aura only where **VM-enforced** live code change (effect-gated Mutate, typed/safe mutation with provenance, snapshot/heal, incremental dirty→JIT/AOT invalidate) clearly beats vanilla `CONFIG SET maxmemory-policy`, Redis modules / `PLUGIN.so`, Lua / RedisGears reload, or a Python sidecar that only flips knobs. Stdlib (`hot-strategy`, `std/mutate` helpers) is the **library surface on top of** that native story — not the differentiator by itself. Mark stretch matches **WEAK**; say **NONE** for Cluster / ops/s / monitoring-stack territory. Forced “Aura can script X too” marketing is an anti-goal.

---

## 2. Aura capability catalog

Two layers. **Layer A = language/runtime/compiler native (原声/原生).** **Layer B = stdlib** sitting on A. For each: **Aura-native?** · **Used by aura-redis?** · pointer.

### 2.A Language & runtime native (non-stdlib-first)

#### A.1 Sandbox / capability / effect (VM)

| Capability | What it uniquely enables | Aura-native? | Used? | Pointer |
|------------|--------------------------|--------------|-------|---------|
| **`aura.core.sandbox`** `SandboxMode` {Off, Restricted, Strict}; atomic SSOT `set_mode` | Process-wide sandbox authority; links capability registry, workspace isolation, provenance policy (FailOnStale under Strict); sweeps Soft-era cross_grants on enter Restricted/Strict | yes | **partial** (demos often `AURA_SANDBOX=off`; `DENY_PLUGIN` on) | `src/core/sandbox.hh`; primitives `security:set-sandbox-mode!` |
| **`aura.core.capability_model`** `Effect` bits (Mutate, Network, Ffi, MacroSelfEvo, …) + grants | Effect checks on mutate/FFI/network; grant epoch bound to Mutation epoch + fiber; multi-tenant retain windows | yes | **partial** (agent needs Network; no Ffi in agent; grants not prod-wired) | `src/core/capability_model.hh`; `security:grant-effect!` / `grant-capability!` / `check-effect` |
| **`with-capability` / `check-capability` / `capability-stack`** | Env-bound capability special forms | yes | **no** | primitives; `lib/std/capability.aura` thin wrap |
| **`security:set-effect-sandbox-mode!` / `set-tenant-principal!` / `grant-cross-tenant!` / `check-tenant-isolation`** | Effect sandbox + tenant principal / isolation | yes | **no** | primitives security section |
| **`aura.core.workspace_isolation`** | Tenant principals; cross-tenant deny; isolation audit | yes | **no** | `src/core/workspace_isolation.*` |
| **Unified `SecurityEvent` ring + WAL** | Single “last deny why?” across effect / isolation / hygiene / allow | yes | **no** | `src/core/security_event.hh`; `query:security-audit*` |
| **`AURA_REDIS_DENY_PLUGIN`** (repo invariant) | Product: adaptation ≠ dlopen | repo policy on Aura | **yes** | `aura-native-control.md` |

#### A.2 Mutation core / TypedMutation / provenance / linear

| Capability | What it uniquely enables | Aura-native? | Used? | Pointer |
|------------|--------------------------|--------------|-------|---------|
| **`(mutate :op)` / mutate prims** (`mutate:rebind`, atomic batch, …) under **`MutationBoundaryGuard`** | Structural FlatAST edits with Guard acquire, rollback, epoch bump — not free-form eval | yes | **yes** (via hot-strategy → rebind) | `evaluator_mutation_boundary.cpp`; `docs/generated/primitives.md` |
| **`typed-mutate-atomic`** | Atomic typed mutate path | yes | **no** | primitives |
| **`TypedMutationAudit`** (type + **linear** + **provenance** invariants; Full default in prod) | Post-mutate hard-gate; trail + optional typed-summary WAL | yes | **no** (agent doesn’t query trail) | `src/compiler/typed_mutation_audit.h` |
| **`mutate:set-agent-fingerprint` / `mutate:validate-reflected`** | Agent identity + reflected validate | yes | **no** | primitives Mutate section |
| **`with-pinned` / `pin-stable-refs` / `unpin-stable-refs`** | Pin StableNodeRefs across mutate | yes | **no** | primitives |
| **Provenance** `query:last-mutation-provenance`, `query:provenance-of*`, `query:node-provenance`, `reflect:provenance-blame` | Attribute / blame edits; FailOnStale under Strict | yes | **no** | primitives Query; `provenance_tracker.hh` |
| **`ast:snapshot` / `ast:restore` / `ast:diff` / ownership validate** | Structural last-good + post-restore checks | yes | **yes** (heal path) | primitives Ast |
| **`workspace:*`** (snapshot, rollback-to/latest, memory-limit, concurrent-mutation-policy, merge-3way, can-write?) | Workspace-level safety & memory | yes | **partial** (workspace used; APIs mostly unused) | primitives Workspace |
| **Mutation audit WAL** (`AuditWalRecord` + reason tail) | Durable mutate forensic trail | yes | **no** | `mutation_audit_wal.hh`; `security:set-audit-persist-dir!` |
| **Macro hygiene / `MacroIntroduced` / `Effect::MacroSelfEvo`** | Hygiene-protected nodes; Strict refuse silent restamp; macro mutate needs MacroSelfEvo grant | yes | **no** (choose-fn not macro-introduced) | provenance_tracker; capability_model #3542 |
| **Linear types** `define-linear`; IR Move/Borrow/MutBorrow/Drop (`lowering_linear_types`) | Ownership in lowered IR; TypedMutation audits linear invariants | yes | **no** | `lowering_linear_types.ixx`; primitives |

#### A.3 Hot-update / JIT / AOT / incremental pipeline

| Capability | What it uniquely enables | Aura-native? | Used? | Pointer |
|------------|--------------------------|--------------|-------|---------|
| **Dirty propagation + IR dirty bridges** | BFS cascade after mutate; type∪IR cone; Production/Full full-cone | yes | **indirect** (rebind dirties; agent doesn’t observe) | `dirty_propagation.ixx` |
| **JIT invalidate / compile epoch** (`query:jit-stats-hash` hotswap-invalidate, `compile:epoch`) | Prove rebind caused recompile work | yes | **no** (denseness proof unused in redis) | hot-strategy.md contract; JIT |
| **`aot:reload` / region mask / module version** | Native `.so` hot-patch + multi-agent region isolation | yes | **no** (**anti-moat** vs DENY_PLUGIN) | primitives; `aot_hot_update_health.hh` |
| **`hot-swap:fn`** | AOT/plugin swap surface (≠ pure-Aura rebind) | yes | **no** | primitives |
| **`compile:relower-strategy` / `compile:snapshot`** | Strategy relower / compile snapshot | yes | **no** | primitives Compile |

*Note:* AOT hot-update is a **runtime** hot-patch story adjacent to — and **distinct from** — pure-Aura `hot-strategy` denseness. aura-redis product path = latter; former is escape-hatch class.

#### A.4 Fiber / concurrency / orch

| Capability | What it uniquely enables | Aura-native? | Used? | Pointer |
|------------|--------------------------|--------------|-------|---------|
| **`fiber:spawn` / `join` / `yield` / `spawn-backend`** | Scheduler (serve-async) or thread-fallback concurrency | yes | **no** | primitives Fiber; `src/serve/fiber.*` |
| **Grant/effect fiber-bind; hard fiber isolation (MT)** | Per-fiber effect sessions; peer outermost revoke safety | yes | **no** | capability_model issues #2055/#3799 |
| **`aura.orch`** agent spawn + multi-fiber mailbox + BP admission/decay | Runtime agent orchestration admission under load | yes | **no** | `docs/agent-orchestration-status.md`; `src/orch/*` |
| **`agent:tick` / `agent:running?` / `agent:recover-from-error`** (native prims) | Host agent tick + closed-loop recovery | yes | **no** (custom Aura loop; heal via hot-strategy) | primitives Agent |
| **C data-plane threading** | Single-threaded epoll by design | n/a | C only | `architecture.md` |

#### A.5 GC / memory ownership (constraints for long-lived agents)

| Capability | What it uniquely enables | Aura-native? | Used? | Pointer |
|------------|--------------------------|--------------|-------|---------|
| **Evaluator GC** (root flush, pair/string compact, safepoint with serve) | Long-lived agent heaps don’t silently unbound-grow without GC | yes | **default runtime** | `evaluator_gc.cpp`; `gc_hooks.h` |
| **`resource:quota-*` / process ResourceQuota** (memory/fibers/time; per-tenant option) | Soft/hard resource bounds under self-evolve | yes | **no** | `resource_quota.*` |
| **`prim-heap-quota`** (pairs/strings/vectors soft limits) | Bound list/string growth in multi-fiber mutate loops | yes | **no** | `docs/stdlib/prim-heap-quota.md` (enforced in Evaluator) |
| **`workspace:memory-limit` / `memory-used`** | Per-workspace memory ceiling | yes | **no** | primitives |
| **Arena / lifetime pin / envframe lifetime** | Ownership pins across mutate boundaries | yes | **indirect** | `lifetime_pin`, `envframe_lifetime` |

#### A.6 Macro / hygiene / self-modifying language story

| Capability | What it uniquely enables | Aura-native? | Used? | Pointer |
|------------|--------------------------|--------------|-------|---------|
| **FlatAST SSOT after mutate** (`current-source :workspace`, authoritative unparse) | Agents always read live post-mutate program text | yes | **partial** | `workspace-source-ssot.md` |
| **MacroIntroduced marker + hygiene provenance** | Self-modifying macros don’t silently corrupt under Strict | yes | **no** | provenance_tracker #1877 |
| **`mutate:rollback-macro-introduced` + SE MacroHygieneRollbackOnStrict** | Strict sandbox rollback of macro-introduced edits | yes | **no** | security_event kind=5 |
| **EDSL `(query :op)` `(mutate :op)` `(workspace :op)` + `engine:metrics`** | Preferred agent surface (primitives.md) | yes | **partial** (hot-strategy path; not full EDSL loop) | primitives.md preamble |

#### A.7 Interop constraint (forces architecture)

| Capability | What it uniquely enables | Aura-native? | Used? | Pointer |
|------------|--------------------------|--------------|-------|---------|
| **`std/ffi` / `c-func` under `Effect::Ffi`** | Capability-gated native calls | yes | **partial** (legacy `server_ffi`; **not** policy agent) | ffi prims |
| **FFI + closure inertness (pinned Aura rev)** | Many `c-func` → user closures inert → **split process required** for hot-strategy | constraint | **yes** (architectural) | `aura-native-control.md` |

#### A.8 Language-wide non-features (do not match as Redis wins)

Tree-walker / default non-JIT Lisp RESP path (~70–110× slower); GC/AOT/JIT as “makes Redis faster”; Redis-shaped Aura core prims (forbidden).

---

### 2.B Stdlib (library surface on Layer A)

| Capability | What it uniquely enables (on top of A) | Aura-native? | Used? | Pointer |
|------------|----------------------------------------|--------------|-------|---------|
| **`std/hot-strategy`** register!/swap!/heal!/version | Ergonomic pure-Aura strategy denseness over `mutate:rebind` + `ast:snapshot` | yes (lib) | **yes** | `lib/std/hot-strategy.aura` |
| **`std/mutate`** safety-snapshot / boundary-safe? / summary / atomic-batch-safe | Agent-facing bridge to Guard / quota / mutation-log | yes (lib) | **yes** (safety*); summary unused | `lib/std/mutate.aura` |
| **Hand fitness / threshold mutate / evolve in `policy_agent`** | Redis-specific closed loop (not `std/evolve`) | in-repo Aura | **yes** | `policy_agent.aura` |
| **`std/evolve`** | Intend-analytics strategy version bump | yes (lib) | **no** | `evolve.aura` |
| **`std/swarm` + pso/abc/ant/boids/fss`** | Population search; caller applies via swap! | yes (lib) | **no** | swarm/fss docs |
| **`std/agent` closed-loop / decision-metrics** | Generic EDSL agent loop | yes (lib) | **no** | `agent.aura` |
| **`std/heal`** | AST surgical heal from errors | yes (lib) | **no** (hot-strategy heal) | `heal.aura` |
| **`std/hot-update*`** / region / monitor | AOT `.so` reload helpers over `aot:reload` | yes (lib) | **no** (anti-moat) | hot-update*.aura |
| **`std/atomic-swap`** | Queue/commit resource↔artifact bindings | yes (lib) | **no** | atomic-swap.aura |
| **`std/orchestrator`** | Pure-Aura multi-role pipeline | yes (lib) | **no** | orchestrator.aura |
| **`std/engine-metrics`** | Thin wrappers on `engine:metrics` | yes (lib) | **no** (INFO via RESP) | engine-metrics.aura |
| **`std/ffi`** | Require-installs host FFI prims | yes (lib) | legacy only | ffi.aura |

---

## 3. Redis production pain catalog (real-world)

Concrete industry pains. **No fabricated vendor %.** Severities: **S1** outage/SLO burn · **S2** chronic waste/risk · **S3** ops toil.

### 3.1 App / cache owner

| Pain | Sev | Symptom | Usual mitigation | Limitation |
|------|-----|---------|------------------|------------|
| **Wrong `maxmemory-policy` under regime shift / 大促** | S1 | Hit% cliffs when traffic flips zipf↔scan / flash sale | Manual `CONFIG SET`; dual runbooks | Human lag; wrong next phase |
| **Hot-key** | S1–S2 | One key dominates; useful cold set evicted | Local cache, shard key, replicas | Doesn’t fix eviction of *other* useful keys |
| **Bigkey** | S2 | Slow DEL; memory cliffs | `MEMORY USAGE`, split keys | Offline/ops; policy still global |
| **TTL pileup / expire storm** | S1–S2 | Latency when many keys expire | lazyfree, stagger TTLs | LRU/LFU blind to “expire soon” |
| **Thundering herd on miss** | S1 | Stampede to origin | locks, early expire | Cache policy still static |
| **Persistence vs cache confusion** | S2 | RDB/AOF cost or surprise loss | Separate durable store | Product/process, not mutate |

### 3.2 SRE / platform

| Pain | Sev | Symptom | Usual mitigation | Limitation |
|------|-----|---------|------------------|------------|
| **Eviction storm** | S1 | `evicted_keys` spike; cascading miss | Raise memory; change policy | Static; over-provision habit |
| **Unexplained hit-rate drop** | S2 | Dashboards lack *policy reason* | CONFIG history, tribal knowledge | No structured rationale |
| **Slowlog / latency unexplained** | S2 | p99 blips; fork spikes | slowlog, latency monitor | No adaptive policy to explain |
| **`CONFIG REWRITE` / fear of live experiments** | S2 | Ops freeze knobs | Careful CONFIG; blue/green | Experiments rare → wrong policy |
| **Replica lag / failover cold policy** | S2 | Promote inherits data not policy gen | Manual re-CONFIG | Policy not first-class |
| **Cluster slot migration** | S1–S2 | Reshard stalls, CROSSSLOT | Cluster playbooks | Topology — not single-node policy |
| **Module load risk** | S1 | Bad module → crash | Avoid modules | Adaptation via modules = process risk |
| **Lua slow / RedisGears complexity** | S2 | BUSY; Gears deploy cost | lua-time-limit; avoid Gears | Scripts ≠ Guard-bounded typed mutate |
| **Connection storms / blocked clients** | S1–S2 | CLIENT LIST blowups | pools, proxies | Not eviction-mutation |
| **RDB/AOF under pressure** | S2 | Fork spikes | diskless, rewrite tune | Orthogonal |

### 3.3 Multi-tenant SaaS

| Pain | Sev | Symptom | Usual mitigation | Limitation |
|------|-----|---------|------------------|------------|
| **Noisy neighbor / shared policy** | S1–S2 | One tenant’s flood evicts another’s hot set | Separate instances; ACL; proxy quotas | ACL ≠ per-tenant *mutable strategy* |
| **Prefix/multi-db, one maxmemory-policy** | S2 | Global LRU/LFU suboptimal | More pods ($$$) | Cost ∝ isolation |
| **Over-provision RAM (static policy)** | S2 | Buy RAM so worst phase “works” | Vertical scale | Adaptation could reclaim headroom |

### 3.4 Security

| Pain | Sev | Symptom | Usual mitigation | Limitation |
|------|-----|---------|------------------|------------|
| **Open bind / weak ACL** | S1 | Exposed Redis | bind, ACL, TLS | Hygiene — not Aura-specific |
| **Who may mutate policy live?** | S2 | Trust gap for live behavior change | Restrict CONFIG; no modules | Needs effect grants + audit/canary + provenance |

---

## 4. Matching matrix (core)

Prefer **native** differentiators in the capability column. Fit: **STRONG** = sandbox-bounded live code change clearly beats CONFIG/module/sidecar; **MEDIUM** = Aura helps but C kernels/ops still dominate; **WEAK** = stretch; **NONE** = do not claim.

| Redis pain | Aura capability(ies) (native first) | Fit | How the match works | aura-redis status | Next proof |
|------------|-------------------------------------|-----|---------------------|-------------------|------------|
| Wrong policy under regime shift / 大促 | **Guard + mutate:rebind** + dirty/JIT invalidate; Layer B hot-strategy swap + fitness | **STRONG** | Live choose-fn body changes under Mutate effect; C runs named kernels only | SHIPPED `phase_marathon` 100%/0 regret; **GAP** `mutation_gain` Δ=0 | **A1** |
| Eviction storm / flash churn | Same mutate path + soft-goal choose body | **STRONG** | Policy *code* encodes evict-CPU budget | SHIPPED `flash_churn` | CI + optional A8 |
| Ops fear of live experiments / CONFIG rewrite | **Sandbox Restricted/Strict** + boundary-safe + **ast:snapshot/restore** + TypedMutationAudit + heal | **STRONG** | Trusted live experiment: Guard + snapshot/heal; C keeps last kernel if agent dies | SHIPPED poison_heal; HA reconnect | **A4** canary; **A11** Restricted |
| Poison / inverted policy | snapshot/heal + Mutate rollback; B: hot-strategy:heal! | **STRONG** | Last-good body/snap ≫ stuck bad CONFIG | SHIPPED | CI gate |
| Module / PLUGIN / Lua-Gears as adaptation | **Effect::Ffi deny** + DENY_PLUGIN + pure-Aura rebind (not aot:reload) | **STRONG** (contrast) | Moat = typed workspace mutate, not dlopen/script reload | DENY_PLUGIN demos | Never re-elevate PLUGIN/AOT reload as moat |
| “Who may mutate policy?” / ACL trust | **grant-effect!(Mutate/Network)** + tenant principal + SecurityEvent/WAL + provenance | **MEDIUM→STRONG** | VM-enforced who/what; audit join by mutation_id | PARTIAL (off sandbox) | **A11** + **A3** |
| Noisy neighbor / shared policy | **workspace_isolation** + cross-tenant deny + multi-register choose; B: POLICY hints | **MEDIUM** | Native MT isolation + per-prefix strategy bodies | PARTIAL M12 hints | **A5** |
| “Why did hit% drop?” | **mutation audit WAL** + TypedMutation trail + `query:last-mutation-provenance` + version; B: mutate:summary | **MEDIUM** | Native forensic trail → ops explain | PARTIAL logs; GAP ring | **A3** (prefer native trail export) |
| Hot-key under eviction | choose → C `PIN`/LFU (Aura selects) | **MEDIUM** | Native mutate only chooses; kernels dominate | SHIPPED auto-pin | — |
| TTL pileup | INFO signals → ttl_aware choose via rebind | **MEDIUM** | Aura picks kernel | SHIPPED `ttl_wave` | — |
| Failover cold policy | hot-strategy:version + workspace/agent reseed (native version is process-local) | **MEDIUM** | Pin policy generation across promote | GAP | **A6** |
| Cost / over-provision RAM | multi-phase hit quality + meta-policy freeze (mutate into conservative) | **MEDIUM** | Fewer “buy RAM for worst policy” | marathon citeable; freeze GAP | **A8** after A1 |
| Evolve stuck thresholds | mutate body search; **unused** swarm/FSS on native rebind | **MEDIUM** | Population search → swap! | hand evolve Δ=0 | **A2** + **A15** |
| Bigkey | — | **WEAK** | Not language mutate | out of scope | C/ops |
| Thundering herd | — | **WEAK** | App-level | no | Don’t market |
| Slowlog (non-policy) | engine:metrics (agent only) | **WEAK** | ≠ Redis latency tools | no | Anti-match APM |
| Persistence / RDB-AOF | — | **NONE** | | | |
| Connection storms | — | **NONE** | | | |
| Cluster reshard | — | **NONE** | Topology; v1 anti-goal | | |
| Match Redis ops/s | — | **NONE** | C hygiene; Lisp path slow by design | | |
| Replace monitoring stacks | SecurityEvent ≠ Datadog | **NONE** | Export into existing stacks | A3 feeds | |
| “Lua scripting in Aura stdlib” as moat | — | **NONE** | Forced match — reject | | Prefer native Guard/sandbox story |

### 4.1 Anti-matches (do not claim)

1. **Redis Cluster / slot migration / Cross-slot** — topology; v1 anti-goal.  
2. **Raw ops/s as product moat** — memtier hygiene; Lisp data plane intentionally not competitive.  
3. **Replacing monitoring / slowlog / APM** — emit policy/security explain into existing stacks.  
4. **Bigkey discovery, connection storms, RDB/AOF tuning** — C/ops.  
5. **“Aura GC/JIT makes Redis faster”** — language constraints, not Redis features.  
6. **PLUGIN.so / `aot:reload` / hot-update as the adaptation moat** — escape hatch; denseness moat is pure-Aura mutate + hot-strategy.  
7. **“Stdlib scripts like Lua/Gears”** — without sandbox+typed-mutate+provenance, that is a **WEAK** marketing claim; do not use it.

---

## 5. Synthesis: demand clusters

| Cluster | Top pains | Native levers (A) + stdlib (B) | Citeable metric | Priority vs A* |
|---------|-----------|--------------------------------|-----------------|----------------|
| **C1 Regime-adaptive eviction** | 大促 regime shift; eviction storm | A: Guard+rebind+dirty; B: hot-strategy+fitness | `phase_marathon` 100%/0 regret; `mutation_gain` ≥ +8pp | **A1**, A13 — **P0** |
| **C2 Trusted self-modifying policy** | Ops fear; poison; module risk; who may mutate | A: sandbox+effects+TypedMutation+snapshot; B: heal/canary | `poison_heal` +100pp; canary test | **A3**, **A4**, **A11** — **P0** |
| **C3 Search-based policy improve** | Stuck thresholds; evolve Δ=0 | A: rebind; B: hand evolve + **swarm/FSS** | `evolve_gain` ≥ +8pp | **A2**, **A15** |
| **C4 Multi-tenant strategy isolation** | Noisy neighbor | A: workspace_isolation+grants; B: multi-register | `prefix_mix_v2` ≥ +20pp | **A5** — **P1** |
| **C5 Policy/security explainability** | Why hit% dropped | A: audit WAL + provenance + SecurityEvent; B: summary | Parseable reasons + mid join | **A3**, A14 — **P1** |
| **C6 HA policy continuity** | Failover cold policy | A: process version limit; B: pin+reseed | HA test profile continuity | **A6** — **P1** |
| **C7 Controller cost gate** | Over-provision + poll cost | A: quota; B: meta-policy freeze | INFO rate ↓ ≥5× | **A8** — **P2** |
| **C8 Typed / structural pressure** | HASH/ZSET skew | C INFO + Aura choose (kernels C) | `typed_pressure` | **A7** — **P2** |

---

## 6. Gaps both ways

### 6.1 Native (and stdlib) unused — plausible Redis match

| Capability | Plausible match | Risk | Candidate |
|------------|-----------------|------|-----------|
| **Restricted/Strict + grant-effect Network (no Ffi)** | Prod trust for live policy | TA availability | **A11** |
| **TypedMutationAudit trail + mutation WAL + provenance queries** | Ops “why” + compliance | Schema | **A3** (native export, not only logs) |
| **workspace_isolation / grant-cross-tenant / set-tenant-principal** | True multi-tenant policy | Needs tenant model | after **A5** |
| **SecurityEvent / query:security-audit** | Unified deny/allow trail for policy ops | — | fold A3 |
| **std/swarm / FSS / PSO** on rebind | Close evolve_gain | Controller CPU | **A15** |
| **mutate:atomic-batch-safe / typed-mutate-atomic** | Multi-name prefix swap | Complexity | under **A5** |
| **fiber:spawn parallel trial fitness** | Canary/shadow without C hook | — | **A16** |
| **agent:recover-from-error / TypedMutation recover** | Richer poison than heal! | Overkill | after A4 |
| **prim-heap-quota / resource:quota / workspace:memory-limit** | Bound long-lived agent | Niche | hygiene |
| **aot:reload / hot-update** | — | Competes with DENY_PLUGIN moat | **do not elevate** |
| **Linear types in choose-fn** | Ownership-safe policy values | Stretch | **WEAK** explore |
| **aura.orch mailbox BP gates** | Multi-agent policy roles | Ops complexity | **WEAK** |

### 6.2 Redis pains unmatched (C / product / out of scope)

Cluster reshard; connection storms; RDB/AOF; bigkey discovery; ops/s parity; replacing APM; open-bind hygiene; W-TinyLFU/SLRU **implementation** (C kernel; Aura selects — **A12**).

---

## 7. Pointers / backlog delta

- Demand mining + A1–An: [`aura-demand.md`](aura-demand.md).  
- When this matrix and demand disagree on fit, **prefer honesty here** (WEAK/NONE; native-first).  
- **A15** — `std/swarm` (grid/PSO/FSS) as evolve backend → rebind.  
- **A16** — fiber parallel trial fitness for canary/shadow (agent-side).  
- **Still start:** **A1 → A2 → A3** (citeable mutate/evolve deltas + **native-backed** audit/provenance export). A15 after A2 if hand-evolve stays Δ=0.

---

## Changelog

| Date (CST) | Notes |
|------------|-------|
| 2026-09-23 | Initial matrix; expanded §2 into **Layer A native** (sandbox/capability/typed-mutate/provenance/JIT-dirty/fiber/orch/GC) + **Layer B stdlib**; matrix prefers native trusted-live-change over Lua-like scripting claims. |
