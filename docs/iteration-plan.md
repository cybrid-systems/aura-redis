# aura-redis iteration plan

Ordered iterations. Each ends with: **smoke green**, **commit + push `main`** (CI wait optional per project preference), short note in commit body.

Aura core changes: **out of scope** unless separately approved as generic; this plan is **aura-redis only**.

---

## Iteration 0 — Design freeze ✅ (this doc)

- [x] `docs/architecture.md`
- [x] `docs/iteration-plan.md`
- Deliverable: design agreed in-repo.

---

## Iteration 1 — Native core skeleton + FFI hello ✅

**Goal:** Build `libaura_redis_core.so`; Aura loads it via `std/ffi` and calls `ar_core_create` / `ar_ping` / in-process `ar_set`/`ar_get`.

**Exit:** Aura process SET/GET through C ≥ 10× pure-Aura `store-set-string!` microbench (order-of-magnitude check).

---

## Iteration 2 — TCP + RESP in C + memtier baseline ✅

**Goal:** C epoll loop + RESP2 for PING/GET/SET/DEL/EXISTS; `server_ffi.aura` serve_forever; memtier vs Redis scripted.

**Exit:** memtier p=1 Totals reported; target **≥20% Redis** (stretch); must beat Lisp engine by ≫10×.

**Result:** p=1 ratio **1.125** vs Redis; ≈334× Lisp. See `docs/perf-log.md`.

---

## Iteration 3 — Pipeline + command stretch toward gate ✅

**Goal:** Efficient pipelining; MGET/MSET; INCR; FLUSHDB; KEYS optional.

**Exit:** memtier p=1 **≥50% Redis**; p=16 not regressing vs p=1.

**Result:** Pipeline parse-many + MGET/MSET/INCR/DECR/FLUSHDB landed with iter2; p=1 **112%**, p=16 **130%** Redis.

---

## Iteration 4 — Chase 80% gate ✅

**Goal:** Buffer sizing, parse/encode tight loops, dict load factor, syscall batching (`writev`), reduce copies.

**Exit:** **`scripts/memtier-cmp.sh` ratio ≥ 0.80** on frozen matrix (primary gate).

**Result:** Gate cleared at **1.125** without writev; remaining levers documented in `docs/perf-log.md`.

---

## Iteration 5 — Eviction vtable: LRU + LFU + maxmemory ✅

**Goal:** `ar_core_set_evict_by_name`; maxmemory; approximate LRU/LFU; metrics.

**Exit:** Under maxmemory, eviction keeps RSS bound; smoke + a small eviction test.

**Result:** `lru`/`lfu` + `ar_core_set_maxmemory` + `tests/test_eviction.py`; env `AURA_REDIS_MAXMEMORY` / `AURA_REDIS_EVICT`.

---

## Iteration 6 — Aura adaptive supervisor (the moat) ✅

**Goal:** `adaptive.aura` polls metrics; swaps LRU↔LFU (or adaptive) via FFI; optional `hot-strategy` for threshold lambdas.

**Exit:** Demo script: load pattern A → strategy X; pattern B → strategy Y; logged swaps; correctness preserved.

**Result:** `AURA_REDIS_ADAPTIVE=1` serve_ms pump in `server_ffi.aura` (policy inlined; mirrors `adaptive.aura` / `adaptive_body.aura` — Aura FFI+closure quirks); rules write-heavy→lfu / read-heavy+hit≥60%→lru; `scripts/demo-adaptive.sh` / `tests/test_adaptive.py --spawn` shows lru→lfu then lfu→lru.

---

## Iteration 7 — Hot-update strategy plugins (optional stretch) ✅ (v1 dlopen)

**Goal:** Strategy as reloadable `.so` via `std/hot-update` / `aot:reload` **or** dlopen of policy packs from aura-redis, without dropping listen socket.

**Exit:** Reload new eviction impl while server runs; one memtier soak.

**Result (v1):** `ar_core_load_evict_plugin` + sample `native/plugins/evict_random.c` → `libar_evict_random.so`; env `AURA_REDIS_EVICT_SO` / `--plugin`; `tests/test_evict_plugin.py`.

**Result (stretch — live reload):** RESP `PLUGIN` / `PLUGIN <so>` swaps eviction `.so` mid-`serve_*` without dropping the listen socket; second sample `libar_evict_rr.so`; `ar_metric_plugin_reloads`; `tests/test_plugin_reload.py` + `scripts/demo-plugin-reload.sh`. **`std/hot-update` / `aot:reload` deferred** — Aura AOT reload targets func_table modules, not `ArEvictOps` C plugins (see `docs/architecture.md` §4.4).

---

## Iteration 8 — Layout evolution ✅

**Goal:** Second layout or hot/cold tier; Aura triggers migrate on signal.

**Exit:** Documented API + one migrate demo.

**Result:** Layouts `flat` (alias `flat_hash`) and `hot_cold`; `ar_core_set_layout` / `ar_core_layout_name` with generation + `layout_busy` quiescent migrate; RESP `LAYOUT [name]`; env `AURA_REDIS_LAYOUT` / `AURA_REDIS_LAYOUT_ADAPTIVE` + `ar_core_adapt_layout` (GET-heavy → hot_cold); promote-on-GET / demote under hot soft-cap; `tests/test_layout.py`.

---

## Iteration 9 — Aura-native mutate / hot-strategy control plane ✅

**Goal:** Maximize sandbox + mutation + hot-strategy; demote PLUGIN/.so as moat.

**Exit:** RESP `EVICT`/`INFO`; `policy_agent.aura` + policy bodies; demo shows choose-fn swap changes eviction under two loads; heal smoked; docs.

**Result:** Split C `aura_redis_server` + Aura `policy_agent` (RESP apply — avoids FFI+closure inertness); `hot-strategy:register!/swap!/heal!` + `mutate:safety-snapshot`; `AURA_REDIS_DENY_PLUGIN`; `docs/aura-native-control.md`; `scripts/demo-aura-native.sh` / `tests/test_aura_native.py` / `tests/test_hot_strategy_policy.aura`.

---

## Parallel track (not blocking gates)

- Keep pure `AURA_REDIS_ENGINE=aura` for reference tests.
- README: architecture summary + how to bench.
- Generic Aura wishlist (string-ref / hash probe / tcp buffers / AOT `set!`) filed only as **separate** aura issues if/when asked—**not** required for M3.

---

## Current position

**Iterations 0–9 done.** Aura-native path is the product story; PLUGIN remains escape hatch (DENY_PLUGIN under sandbox profile).

---

## Future iterations (10+) — adaptive load mutation

Design exploration (archetypes, P0–P3 axes, harness names, numbered backlog with exit criteria):

→ [`runtime-mutation-explore.md`](runtime-mutation-explore.md)

Summary of planned next steps:

| Iter | Theme |
|------|--------|
| 10 | Multi-signal INFO + choose-fn API |
| 11 | Joint EVICT+LAYOUT policy |
| 12 | Industry harness pack (`zipf_hotkey`, `diurnal_shift`, …) |
| 13 | RESP eviction tunables (`samples N`, soft watermark) |
| 14 | Hot-key pin set |
| 15 | New kernel: SLRU or TTL-aware |
| 16 | Soft-goal choose (hit% vs evict CPU) |
| 17 | hot_cold threshold mutation via Aura |
| 18 | Optional `std/evolve` thresholds |
| 19 | W-TinyLFU-ish / GDSF kernel |
| 20 | Per-prefix policy namespace sketch |
| 21 | ZSET / gaming — only if data-type roadmap opens |
