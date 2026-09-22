# Runtime mutation exploration / 运行时变异探索

**Status:** design exploration (2026-09-22) — **A–C in progress** (see [`mutation-gains.md`](mutation-gains.md) / M6 fitness swap+heal landed)  
**Scope:** aura-redis only — Aura mutates **policy**; C executes **dumb kernels**.  
**Do not:** grow aura-grok / Aura core for Redis-shaped prims.  
**Companion:** [`aura-native-control.md`](aura-native-control.md) · [`workloads.md`](workloads.md) · [`architecture.md`](architecture.md)

**Goal / 目标:** under real big-tech cache workload *shapes*, adaptive policy
(hit% / regret vs oracle) ≥ best fixed kernel on each phase — by mutating
`choose-fn` (and later thresholds / joint EVICT+LAYOUT), not by `dlopen`.

---

## 0. Already mutable today / 今日已可变

| Axis | What exists | Aura / C split |
|------|-------------|----------------|
| **EVICT** | `noop` / `lru` / `lfu` (+ `PLUGIN` escape hatch) | Aura chooses name via RESP `EVICT`; C swaps vtable between commands |
| **LAYOUT** | `flat` / `hot_cold` (+ C `ar_core_adapt_layout` GET-heavy rule) | RESP `LAYOUT`; optional env adaptive |
| **choose-fn** | `hot-strategy:register!/swap!/heal!` + body strings in `src/redis/policy/` | Pure Aura mutate; heal on bad body |
| **Signals** | `INFO` → `gets/sets/hits/misses/evict/layout/…` window deltas | Agent parses INFO; choose-fn today takes `(dgets dsets dhits dmisses)` |
| **Sample size** | Fixed **16** in C (`sample_pick_lru` / `sample_pick_lfu`) | **Not** yet a RESP knob |
| **Sandbox** | `AURA_REDIS_DENY_PLUGIN=1` | Forces Aura-native path; PLUGIN denied |

**Product rule:** Exploration = mutate choose-fn / evolve body text from INFO.
C stays named kernels. `DENY_PLUGIN` stays on for the moat story.

```text
  INFO deltas ──► choose-fn (Aura code string, hot-swappable)
                        │
                        ▼
                  EVICT <name> / LAYOUT <name>   (C kernel pointer)
```

---

## 1. Big-tech workload archetypes / 大厂负载原型

Cite **real published patterns** (blogs, papers, eng talks) — not fake DOIs.
Map each → pain → preferred mutation axis.

| # | Archetype | Real pattern (cite class) | Pain / 痛点 | Preferred mutation |
|---|-----------|---------------------------|-------------|-------------------|
| 1 | **Meta / Facebook feed & social graph** | Memcached → TAO graph cache; Zipf GET-heavy; hot keys dominate (Atikoglu et al. Facebook Memcached workload; Nishtala et al. Scaling Memcache at Facebook; Bronson et al. TAO) | Hot-key retention under admission floods; write storms on status/like; cold fill after deploy | **LFU / W-TinyLFU-ish + hot-key pin**; joint LAYOUT→`hot_cold`; miss_rate trend in choose-fn |
| 2 | **Twitter / X timeline & rate-limit** | Timeline fanout + Redis rate limits; bursty; working-set shifts; abuse scans (Twitter eng: Redis for timelines/rate-limit; “scan”/KEYS-class cold floods in ops lore) | LRU ok on WS shift; LFU clings to old hot; cold flood evicts useful set | **Aggressive LRU on scan_attack**; ws_oscillate-aware choose; expire/evict under pressure |
| 3 | **Alibaba / ByteDance / 中国电商** | Diurnal peaks; 大促/秒杀 write spikes; bigkey/hotkey; multi-tenant Redis (Aliyun Redis / Tair eng blogs; Double-11 capacity talks; ByteDance Redis practice posts) | Flash-sale write→LFU; night quiet→LRU; bigkey blows maxmemory; noisy neighbor | **Diurnal choose + soft watermark**; per-prefix policy later (P2); TTL-aware eviction |
| 4 | **Netflix EVCache / CDN edge** | EVCache (Memcached-based) almost-all GET; regional; TTL waves causing expiry storms (Netflix Tech Blog: EVCache; caching @ edge) | Expiry floods → miss storms; layout thrash if promote too eager | **TTL-wave aware**: expire-first under pressure; LAYOUT stay `flat` unless WS concentrated; conservative choose |
| 5 | **Uber / Airbnb session & geo** | Short-TTL sessions; write-heavier; high churn (Uber Redis/Docstore talks; Airbnb caching posts) | LRU thrash on churn; need TTL-aware / GDSF-ish; layout promote wasteful | **TTL-aware / expire-first**; session_churn harness; soft goals on eviction CPU |
| 6 | **Gaming / leaderboard (Tencent-style)** | ZSET rank updates, frequent score bumps (Tencent / NetEase game Redis talks; classic Redis leaderboard pattern) | **aura-redis has no ZSET yet** — mark **future kernel** | Future: ZSET + rank-update-aware eviction; until then skip as exit criterion |
| 7 | **Feature-store / ML online store** | Dense GET-by-id; embedding-sized values (tens–hundreds of KB); layout locality matters (Uber Michelangelo / Feast / RedisJSON+vector blog patterns) | Large values → memory cliff; flat hash miss locality; compression tradeoff | **LAYOUT hot_cold** + (P2) value compression toggle; choose on keyspace growth + value-size proxy |
| 8 | **Multi-tenant Redis-as-a-Service** | AWS ElastiCache / Aliyun / Memorystore: noisy neighbor, isolation knobs (AWS ElastiCache best practices; Aliyun Redis multi-tenant posts) | One tenant’s scan/hotkey starves others | **Per-prefix / per-tenant policy namespaces** (P2); hot-key pin per tenant; fairness later (P3) |

### Archetype → harness (preview)

| Archetype | Bench name (see §4) |
|-----------|---------------------|
| Meta Zipf | `zipf_hotkey` |
| Twitter burst / scan | `scan_attack`, extend `ws_oscillate_3phase` |
| China e-comm | `diurnal_shift` |
| Netflix | `ttl_wave` |
| Uber/Airbnb | `session_churn` |
| Feature store | (extend zipf with large values + layout) |
| Multi-tenant | (prefix-tagged mix of above) |

---

## 2. Mutation axes / 变异轴

Prioritize by **impact × Aura-native story × implementability**.

### P0 — extend choose-fn / multi-objective policy（主要 Aura）

| Item | Why | Sketch |
|------|-----|--------|
| **Multi-signal choose-fn** | Today only write/read + hit%; Meta/Twitter need miss trend, eviction pressure, concentration | Extend INFO + agent: `miss_rate` EWMA, `eviction_rate`, p99 proxy (EWMA latency if exported), `keyspace` growth, **top-k access share** (need C counter or sample) |
| **Evolve thresholds / variants** | Fixed 60% hit / 5× read is brittle across diurnal | `choose_aggressive` / `choose_conservative` body strings; `std/evolve` or offline evolve → swap; heal fallback |
| **Joint EVICT+LAYOUT choose** | Layout adapt is C-rule today; Aura should own both | choose-fn returns `(evict . layout)` or two RESP applies per tick |
| **Soft goals** | Flash-sale: max hit% s.t. eviction CPU budget | choose may refuse `lfu` if `evicted/sec` over budget; stay `lru`/`noop` |

**Exit criteria (P0):** on `oscillate` + new `zipf_hotkey` / `diurnal_shift`, multi-signal adaptive ≥ best fixed per phase; heal still recovers from broken body.

### P1 — new C kernels + RESP knobs（小 C 面，Aura 选型）

| Item | Why | Sketch |
|------|-----|--------|
| **Eviction family** | Industry standard beyond LRU/LFU | Named kernels: `slru`, `w_tinylfu` (approx), `ttl_aware`, `gdsf` — each `ArEvictOps`; Aura only picks name |
| **RESP tunables** | Sample=16 is hard-coded; Meta hotkeys need more samples | `CONFIG`-like or admin: `EVICT samples N`, `LFU-LOG`, hot_cold **promotion threshold**, `maxmemory` **soft watermark** |
| **Hot-key pin set** | Classic Meta/Twitter hotspot mitigation | Don’t evict N hottest (tracked by sample or explicit `PIN` set); Aura enables when top-k share > threshold |
| **TTL expiry urgency vs LRU** | Netflix TTL waves; Uber short TTL | Under memory pressure: expire-soon-first before LRU victim |

**Exit criteria (P1):** ≥2 new named kernels selectable via `EVICT`; samples N live-tunable; one pin-set demo under `zipf_hotkey` beats plain LFU on protected hot set.

### P2 — structure / layout deeper

| Item | Why |
|------|-----|
| **hot_cold promote/demote thresholds** | Aura sets (not only C `/4` soft cap) |
| **Shard / segment count; rehash aggressiveness** | Feature-store + large keyspace |
| **Value compression toggle** | Embedding-sized values |
| **Per-tenant / per-prefix policy namespaces** | Multi-tenant SaaS isolation |

### P3 — later / heavier

| Item | Note |
|------|------|
| TinyLFU + admission; ARC; segmented LRU with Aura-evolved segment sizes | Heavier C; still name-selectable |
| Adaptive pipeline / client fairness | Needs scheduling hooks |
| Persistence/AOF off for pure cache; replica read preference | **Out of scope** until cluster/replica exists |
| ZSET / leaderboard kernel | Gaming archetype; future data type |

---

## 3. Aura-native story / Aura 原生叙事（强调）

```text
  ✅ Policy = Aura code strings under sandbox (hot-strategy swap/heal)
  ✅ C = dumb named kernels (lru/lfu/…/future slru)
  ✅ Apply = RESP EVICT / LAYOUT / INFO (no ffi in policy agent)
  ✅ DENY_PLUGIN = on for product demos
  ❌ Moat ≠ dlopen / PLUGIN.so
  ❌ Exploration ≠ “write a smarter .so”
```

Optional later: `std/evolve` mutates choose-fn **body text** from INFO analytics
(thresholds, signal weights) — still Aura strings, still heal-able.

---

## 4. Workload harness ideas / 下一轮压测脚本

Mirror industry traces; extend `scripts/bench_dynamic_evict.py` / `docs/workloads.md`.

| Script | Shape | Favors / success |
|--------|-------|------------------|
| `zipf_hotkey` | Zipf α≈0.99, Meta-like; hot set + cold flood | LFU / pin / TinyLFU-ish ≥ LRU |
| `diurnal_shift` | Quiet→peak→flash-sale write spike→cool | Adaptive tracks phase; regret low vs oracle schedule |
| `ttl_wave` | Netflix-like mass EXPIRE wave then GET storm | TTL-aware / expire-first; miss spike contained |
| `session_churn` | Uber-like short TTL, write-heavy sessions | TTL-aware ≥ LRU; layout not thrashing |
| `scan_attack` | Cold flood / KEYS-like abuse | Choose flips to protect WS (LRU or pin) |
| `ws_oscillate_3phase` | Extend existing `oscillate` to 3+ phases | Adaptive ≥ best fixed **each** phase |

**Success metric / 成功指标:**

- **Adaptive ≥ best fixed** on each phase (hit% on target key set).
- **Regret vs oracle** (oracle = hindsight best kernel schedule) → minimize cumulative regret.
- Secondary: eviction CPU / ops under soft budget (P0 soft goals).

---

## 5. Proposed iteration backlog / 后续迭代 backlog

After Iterations 0–9 (done). Each: smoke green, commit+push main, exit criteria.

| Iter | Title | Exit criteria |
|------|-------|---------------|
| **10** | Multi-signal INFO + choose-fn API | INFO exports miss EWMA / eviction_rate / keyspace; choose-fn signature extended; normal+aggressive+conservative bodies; demo on hot_protect+ws_shift no regress |
| **11** | Joint EVICT+LAYOUT policy | One Aura tick can set both; disable pure-C layout adapt when agent owns joint; test: GET-heavy→hot_cold + lru |
| **12** | Harness pack v1 | Land `zipf_hotkey`, `diurnal_shift`, `ttl_wave`, `session_churn`, `scan_attack`, `ws_oscillate_3phase`; report adaptive vs fixed table |
| **13** | RESP eviction tunables | `EVICT samples N` (default 16); optional LFU log factor; soft maxmemory watermark; docs + smoke |
| **14** | Hot-key pin set | Pin N hottest (or explicit keys); zipf_hotkey: pin+LFU beats LFU-alone on protected set |
| **15** | Kernel: SLRU or TTL-aware | At least one new named `ArEvictOps`; selectable via EVICT; wins on ttl_wave or scan_attack vs lru/lfu |
| **16** | Soft-goal choose (hit% vs evict CPU) | Policy refuses expensive kernel when eviction_rate over budget; diurnal_shift flash-sale stable |
| **17** | hot_cold threshold mutation | Aura sets promote/demote / hot soft-cap via RESP; feature-store-ish large-value microbench |
| **18** | `std/evolve` threshold loop (optional) | Evolve numeric thresholds in body string from INFO; heal if fitness drops; offline or slow online |
| **19** | W-TinyLFU-ish / GDSF kernel | Approx admission or cost-aware victim; zipf_hotkey regret ↓ vs LFU |
| **20** | Per-prefix policy namespace (sketch) | Two prefixes, two choose policies or pin budgets; noisy-neighbor demo |
| **21** | ZSET stub / gaming note | Only if data-type roadmap opens; else keep as documented future kernel |

**Stretch / parallel:** value compression toggle (P2); client fairness (P3); cluster/replica (out of scope).

---

## 6. Priority cheat-sheet / 最高价值变异（执行摘要）

For parent / user — **do these first**:

1. **Multi-signal choose-fn** (miss trend, eviction_rate, hot concentration) — Aura-only, high impact on Meta/Twitter shapes.
2. **choose_aggressive / choose_conservative + heal** — cheap variants before evolve.
3. **Joint EVICT+LAYOUT** — stop splitting policy across C rule + Aura choose.
4. **Industry harness pack** (`zipf_hotkey`, `diurnal_shift`, `ttl_wave`, …) — measure regret vs oracle.
5. **Hot-key pin + EVICT samples N** — classic hotspot mitigation; small C surface.
6. **TTL-aware / expire-first kernel** — Netflix + Uber shapes.
7. **Soft watermark + soft-goal (hit% s.t. evict CPU)** — flash-sale survival.
8. **SLRU / W-TinyLFU-ish named kernels** — next after tunables.
9. **hot_cold threshold via Aura** — feature-store / layout moat.
10. **Per-prefix namespaces** — multi-tenant SaaS story (after single-tenant adaptive is strong).

---

## 7. Non-goals / 非目标（本探索）

- Modifying **aura-grok** or adding Redis prims to Aura core.
- Making PLUGIN/.so the adaptive path.
- Full Redis command parity or cluster before adaptive regret gates.
- Fake paper citations — only well-known eng-blog / paper *classes* above.

---

## 8. Doc map

| Doc | Role |
|-----|------|
| This file | Mutation axes + archetypes + backlog |
| [`aura-native-control.md`](aura-native-control.md) | How mutate/hot-strategy works today |
| [`workloads.md`](workloads.md) | Current dynamic eviction benches |
| [`iteration-plan.md`](iteration-plan.md) | Iter 0–9 done; Iter 10+ pointers here |
| [`architecture.md`](architecture.md) | C/Aura split + vtable |
