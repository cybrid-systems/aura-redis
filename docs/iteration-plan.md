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

**Work:**

1. `native/CMakeLists.txt` + `ar_core.h` / `ar_core.c` (or `.cpp`)
2. In-memory dict (simple open-addressing or chained); string values only
3. Eviction vtable stub (`noop` only)
4. `scripts/build-native.sh` (container-friendly)
5. `src/redis/ffi_boot.aura` — c-load + bind symbols
6. `tests/test_ffi_core.py` or small Aura script: SET/GET roundtrip via FFI (no TCP yet)

**Exit:** Aura process SET/GET through C ≥ 10× pure-Aura `store-set-string!` microbench (order-of-magnitude check).

---

## Iteration 2 — TCP + RESP in C + memtier baseline

**Goal:** C epoll (or poll) loop + RESP2 for PING/GET/SET/DEL/EXISTS; `server_ffi.aura` serve_forever; memtier vs Redis scripted.

**Exit:** memtier p=1 Totals reported; target **≥20% Redis** (stretch); must beat Lisp engine by ≫10×.

---

## Iteration 3 — Pipeline + command stretch toward gate

**Goal:** Efficient pipelining; MGET/MSET; INCR; FLUSHDB; KEYS optional.

**Exit:** memtier p=1 **≥50% Redis**; p=16 not regressing vs p=1.

---

## Iteration 4 — Chase 80% gate

**Goal:** Buffer sizing, parse/encode tight loops, dict load factor, syscall batching (`writev`), reduce copies.

**Exit:** **`scripts/memtier-cmp.sh` ratio ≥ 0.80** on frozen matrix (primary gate).

---

## Iteration 5 — Eviction vtable: LRU + LFU + maxmemory

**Goal:** `ar_core_set_evict_by_name`; maxmemory; approximate LRU/LFU; metrics.

**Exit:** Under maxmemory, eviction keeps RSS bound; smoke + a small eviction test.

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

**Iteration 1 done** (`libaura_redis_core.so` + FFI smoke). **Next: Iteration 2** (TCP + RESP + memtier).
