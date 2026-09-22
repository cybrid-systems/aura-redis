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

## Iteration 6 — Aura adaptive supervisor (the moat)

**Goal:** `adaptive.aura` polls metrics; swaps LRU↔LFU (or adaptive) via FFI; optional `hot-strategy` for threshold lambdas.

**Exit:** Demo script: load pattern A → strategy X; pattern B → strategy Y; logged swaps; correctness preserved.

---

## Iteration 7 — Hot-update strategy plugins (optional stretch)

**Goal:** Strategy as reloadable `.so` via `std/hot-update` / `aot:reload` **or** dlopen of policy packs from aura-redis, without dropping listen socket.

**Exit:** Reload new eviction impl while server runs; one memtier soak.

---

## Iteration 8 — Layout evolution (optional)

**Goal:** Second layout or hot/cold tier; Aura triggers migrate on signal.

**Exit:** Documented API + one migrate demo.

---

## Parallel track (not blocking gates)

- Keep pure `AURA_REDIS_ENGINE=aura` for reference tests.
- README: architecture summary + how to bench.
- Generic Aura wishlist (string-ref / hash probe / tcp buffers / AOT `set!`) filed only as **separate** aura issues if/when asked—**not** required for M3.

---

## Current position

**Iterations 0–5 done.** **Next: Iteration 6** (Aura adaptive supervisor).
