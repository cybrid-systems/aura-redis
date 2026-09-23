# Strong-narrative iteration plan (native-first)

**Status:** authoritative execution plan for the citeable Aura moat  
**Date:** 2026-09-23 CST  
**Tip baseline:** post A12/A10 on main (see changelog)  
**Scope:** Guard-bounded live `choose-fn` mutation under sandbox + C dumb kernels + multi-phase regret evidence — **not** Redis P3 parity.  
**Companions:** [`aura-demand.md`](aura-demand.md) · [`aura-pain-scenarios.md`](aura-pain-scenarios.md) · [`aura-redis-match.md`](aura-redis-match.md) · [`aura-native-control.md`](aura-native-control.md) · [`mutation-gains.md`](mutation-gains.md) · [`perf-eval.md`](perf-eval.md) · [`production-plan.md`](production-plan.md) · [`architecture.md`](architecture.md) · [`high-roi-iterations.md`](high-roi-iterations.md)

**Invariant:** Redis-only work in this repo. Do not modify `/workspace/aura-grok`. Aura compiler/runtime changes must stay generic — prefer fixing `policy_agent` / harness / C INFO hooks here. Prefer **原生** for Layer A native (sandbox / Mutate effect / Guard / TypedMutationAudit / provenance / WAL). Stdlib (`hot-strategy`) is surface on A.

---

## Thesis

**Strong narrative** = Guard-bounded live `choose-fn` mutation under sandbox + C dumb kernels + multi-phase regret evidence.

| Layer | Role in the narrative |
|-------|------------------------|
| **A 原生 (native)** | `sandbox` / `capability` Effect::Mutate·Network / `MutationBoundaryGuard` + `mutate:rebind` / `TypedMutationAudit` / `ast:snapshot\|restore` / provenance / workspace / dirty→JIT invalidate — trust & audit **first-class** |
| **B stdlib** | `std/hot-strategy` register!/swap!/heal!/version — ergonomic surface over A |
| **C kernels** | Aura **selects** names only: `lru` / `lfu` / `ttl_aware` / `slru` / `tinylfu` / `LAYOUT` / `PIN` |

Citeable wins today: `phase_marathon` adaptive **100%** / regret_hits=0; `poison_heal` Δ=+100pp. Open stretch: `mutation_gain` / `evolve_gain` Δ=0 in recent [`perf-eval.md`](perf-eval.md) (historical mutate Δ=+14.8pp in [`mutation-gains.md`](mutation-gains.md)).

IDs map to [`aura-demand.md`](aura-demand.md) **A1–A16**. Wave labels **SN*** order execution; native trust ahead of explore.

---

## Wave ordering (native-aware)

### Wave SN0 — Evidence restore (prove mutation still attributable)

Restore mutate−frozen / evolve−frozen deltas so the moat is measurable again. Harness + agent only; no Aura core.

#### SN1 / A1 — Close `mutation_gain`

| Field | Value |
|-------|--------|
| **Goal** | Mutate path beats frozen under harder diurnal/flash; fitness-swap logged |
| **Native hooks** | Indirect: `mutate:rebind` via hot-strategy under Guard; prove via swap log + hit Δ (JIT invalidate unused) |
| **Exit criteria** | `python3 scripts/bench_regret.py mutation_gain` → mutate−frozen ≥ **+8pp** + `fitness-swap` in log; do not break `phase_marathon` / `poison_heal` |
| **Depends-on** | — |
| **Estimate** | 1–2d |
| **Status** | **DONE** (2026-09-23): mutate 100% vs frozen 72.1%, **Δ=+27.9pp**; fitness-swap + inline EVICT/PIN |
| **Root cause (fixed)** | threshold-mutate-first blocked aggressive swap; `mutate:rebind` re-eval stalled ticks before PIN; cool diluted flash |

#### SN2 / A2 — Close `evolve_gain`

| Field | Value |
|-------|--------|
| **Goal** | Evolve ≥ +8pp vs frozen bad thresholds; ≥2 evolve gens logged |
| **Native hooks** | Same rebind/Guard path; multi-gen keep/revert in agent |
| **Exit criteria** | `python3 scripts/bench_regret.py evolve_gain` → evolve−frozen ≥ **+8pp** + ≥2 evolve gens in log; keep A1 green |
| **Depends-on** | Prefer after A1 (same harness family); can parallel if careful |
| **Estimate** | 1–2d |
| **Status** | **DONE** (2026-09-23): evolve 61.4% vs frozen 4.3%, **Δ=+57.1pp**; ≥2 gens + inline EVICT/PIN |

---

### Wave SN1 — Native trust surface (强叙事可信)

Make ops trust live code change: durable audit, canary, prod-shaped sandbox.

#### SN3 / A3 — Mutation audit + explain (native-leaning)

| Field | Value |
|-------|--------|
| **Goal** | Durable ring / schema `{ts, op, from, to, reason, version}`; prefer native provenance / TypedMutationAudit / audit WAL where feasible; else structured heartbeat + INFO flat keys |
| **Native hooks** | Prefer `query:last-mutation-provenance` / TypedMutationAudit trail / `mutation_audit_wal` from agent process; fallback heartbeat file + INFO |
| **Exit criteria** | `tests/test_policy_audit.py` PASS; explain reasons on `phase_marathon` path ∈ {miss_spike, soft_goal, ttl_pressure, prefix_policy, fitness_swap, evolve_keep, …} |
| **Depends-on** | — (can follow A1/A2) |
| **Estimate** | 1–2d |
| **Status** | **DONE** (2026-09-23): durable audit ring + heartbeat last_audit_*/last_explain; `tests/test_policy_audit.py` PASS

#### SN4 / A4 — Canary choose-fn

| Field | Value |
|-------|--------|
| **Goal** | Trial body N ticks → commit or `heal!` |
| **Native hooks** | `ast:snapshot` / restore; Guard; optional workspace snapshot |
| **Exit criteria** | `tests/test_policy_canary.py` — bad trial auto-heals within T ticks |
| **Depends-on** | A3 |
| **Estimate** | 2–3d |
| **Status** | **DONE** (2026-09-23): trial N ticks → commit or `heal!`; audit canary_start/commit/heal; bad→heal + good→commit green |

#### SN5 / A11 — Restricted sandbox + Network grant

| Field | Value |
|-------|--------|
| **Goal** | Prod-shaped Restricted sandbox + `effect:network` grant path; document when `AURA_SANDBOX=off` still required |
| **Native hooks** | `SandboxMode` Restricted; `grant-effect!(Network)` / Mutate; no Ffi |
| **Exit criteria** | Doc + demo path without `AURA_SANDBOX=off` when TA available; `scripts/sandbox-policy-profile.sh` updated |
| **Depends-on** | sandbox profile |
| **Estimate** | 1d |
| **Status** | **PARTIAL** (2026-09-23): profile `off|restricted` + `smoke-sandbox-profile.sh`; live agent still needs `AURA_SANDBOX=off` — Restricted `grant-effect!` returns #f without Tenant Admin on this Aura pin |

---

### Wave SN2 — Isolation & continuity

#### SN6 / A5 — Prefix isolation deepen

| Field | Value |
|-------|--------|
| **Goal** | Per-prefix choose body or param bag via multi `register!` |
| **Native hooks** | Future: workspace_isolation / tenant principal; today multi hot-strategy names |
| **Exit criteria** | Conflicting-optima `prefix_mix_v2` ≥ +20pp on victim tenant |
| **Depends-on** | M12 |
| **Estimate** | 2–3d |
| **Status** | **DONE** (2026-09-23): worse-tenant **Δ=+96.4pp** (100% vs 3.6%); bags + deep compose + `pfx-bag-0/1` register |

#### SN7 / A6 — Policy version across failover

| Field | Value |
|-------|--------|
| **Goal** | Pin `hot-strategy:version` + profile across replica promote |
| **Native hooks** | Process-local provenance/version; heartbeat / note pin (原生 does not auto-replicate workspace) |
| **Exit criteria** | Extend `tests/test_prod_policy_ha.py` + replica |
| **Depends-on** | P2.14, P1.9 |
| **Estimate** | 1–2d |
| **Status** | **DONE** (2026-09-23): durable `AURA_REDIS_POLICY_PIN` (profile/version/hash); resume logs `from_version`; fail-safe EVICT retained; `test_policy_version_pin_across_restart` PASS |

#### SN8 / A8 — Auto-freeze meta-policy

| Field | Value |
|-------|--------|
| **Goal** | Mutate into freeze when EWMA gain < overhead |
| **Native hooks** | Same rebind path; meta choose body |
| **Exit criteria** | Stable-load INFO/apply rate ↓ ≥5×; hit% within 2pp of always-on |
| **Depends-on** | A1 |
| **Estimate** | 1–2d |
| **Status** | **DONE** (2026-09-23): auto_freeze/unfreeze; polls/sec ×6.7 drop; `tests/test_policy_autofreeze.py` PASS; A14-lite heartbeat `applies_sec`/`polls_sec` |

#### SN9 / A14 — Overhead dashboard

| Field | Value |
|-------|--------|
| **Goal** | Controller overhead: applies/sec, swap rate vs hitΔ |
| **Native hooks** | INFO / heartbeat export (A3 schema) |
| **Exit criteria** | Heartbeat fields + bench summary |
| **Depends-on** | A3 |
| **Estimate** | 0.5–1d |
| **Status** | **DONE** (2026-09-23): heartbeat rates + A14 bench `OVERHEAD` summary (swaps vs hit%, weight/swarm/fiber notes) |

---

### Wave SN3 — Explore

| ID | Goal | Native hooks | Exit criteria | Depends-on | Estimate | Status |
|----|------|--------------|---------------|------------|----------|--------|
| **A13** | Signal-weight evolve (not only min-ops) | rebind body weights | weight path in evolve logs; `evolve_gain` ≥ +8pp | A1–A2 | 1–2d | **DONE** (+55.7pp; w-miss/w-write/w-evict mutate) |
| **A15** | Swarm/FSS/PSO evolve backend | Guard+rebind per trial; `std/swarm` surface | `evolve_gain` ≥ +8pp + swarm gen logs | A2 | 2–3d | **DONE** (`AURA_REDIS_EVOLVE_BACKEND=pso|fss|grid`; lazy `std/swarm`) |
| **A16** | Fiber parallel trial fitness | `fiber:spawn` / join (原生); score without C hook | Dual-body score in logs; no apply of loser | A4, A10 | 1–2d | **DONE** (fiber proxy dual-score; no apply loser; `FIBER_SHADOW=1`) |
| **A10** | Shadow / A/B sample | C sample hook or dual agent | Shadow regret without applying loser | A4 | 2–3d | **DONE** (C `SHADOW`/INFO sample + agent dry-run never EVICT loser; `tests/test_shadow_ab.py`) |
| **A12** | Named C kernel `slru` / TinyLFU — Aura select only | C `ArEvictOps`; Aura name pick | zipf regret ≤ LFU | explore | 2–3d | **DONE** (sample SLRU; `tinylfu` alias/approx; zipf hot-retention = LFU; `tests/test_evict_slru.py`) |

Also backlog (demand map, not SN-gated): **A7** typed pressure INFO+choose; **A9** `hot_cold` RESP knobs; **A11** Restricted sandbox remains PARTIAL.

---

## Execution order (this pass)

1. **A1** → green exit + push  
2. **A2** → green exit + push (keep A1)  
3. **A3** → audit test + explain + push  
4. **A4** → canary choose-fn + `test_policy_canary.py` + push  
5. **A11** → Restricted sandbox profile (honest PARTIAL if TA blocked) + push  
6. **A5** → `prefix_mix_v2` deepen + push  
7. **A6** → policy version pin across failover + push  
8. **A8** → auto-freeze meta-policy + push  
9. **A13** → signal-weight evolve + push  
10. **A15** → swarm/PSO/FSS evolve backend + push  
11. **A16** → fiber shadow dual-score + push  
12. **A14** → finish overhead bench summary + push  
13. **A12** → slru/tinylfu named kernel + push  
14. **A10** → shadow A/B sample path + push  

If blocked: document blocker here, push PARTIAL, continue what is possible.

---

## Anti-goals

- Redis Cluster / ops/s as the citeable story  
- Treating Δ=0 stretch packs as “mutation useless”  
- Growing Aura core for Redis-only prims  
- Re-elevating PLUGIN/.so as the adaptation moat  
- Rewriting dumb kernels in Aura Lisp  

---

## Changelog

| Date (CST) | Notes |
|------------|-------|
| 2026-09-23 | Initial native-first strong-narrative plan (SN0–SN3 / A1–A16). |
| 2026-09-23 | A1 +27.9pp, A2 +57.1pp, A3 audit ring + test green on main. |
| 2026-09-23 | A4 canary choose-fn (auto-heal / commit) + test green on main. |
| 2026-09-23 | A11 Restricted sandbox profile PARTIAL (TA blocker); smoke + docs. |
| 2026-09-23 | A5 prefix_mix_v2 worse-tenant +96.4pp; per-prefix bags + deep compose. |
| 2026-09-23 | A6 policy version pin across agent restart (resume + fail-safe). |
| 2026-09-23 | A8 auto-freeze meta-policy (×6.7 poll drop) + A14-lite heartbeat rates. |
| 2026-09-23 | SN3 explore: A13 weight evolve (+55.7pp evolve_gain), A15 PSO/`std/swarm` propose, A16 fiber-shadow, A14 overhead bench summary. |
| 2026-09-23 | A12 `slru`/`tinylfu` named C kernels (approx TinyLFU doc); zipf retention = LFU. |
| 2026-09-23 | A10 shadow sample path: C `SHADOW`+INFO + agent dual dry-run (never EVICT loser). |
