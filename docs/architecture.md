# aura-redis architecture

**Status:** design v1 (2026-09-22)  
**Goal:** ≥80% of Redis on frozen memtier (1c×1t, SET:GET=1:10, 32B), while keeping **Aura’s runtime self-modification** as the product moat.  
**Constraint:** Aura compiler/runtime changes (if any) stay **generic**. All Redis semantics and adaptive policy live in **this repo**.

---

## 1. Problem and opportunity

### 1.1 Why the pure-Aura path is slow

Measured (vs `redis:7-alpine`, same box):

| Path | Order of magnitude |
|------|-------------------|
| Redis PING / GET | ~40k ops/s |
| Aura Lisp server PING | ~400–570 ops/s (~70–110×) |
| Aura Lisp SET (memtier) | ~10–30 ops/s |

Root causes (architectural, not missing commands):

1. Tree-walker interpretation of the accept→parse→dispatch loop (`set!` forces walker; default `aura file.aura` is not JIT/AOT).
2. RESP in Lisp on copying `string-ref` / `substring`.
3. Interpreter hash linear-scan + alloc/GC on writes.
4. `tcp-recv` → new `string_heap_` string every chunk.

Lisp micro-opts already harvested ~+28% (112→143 totals). They cannot reach 80% of Redis.

### 1.2 Opportunity vs Redis

Redis is a **fixed** C data plane + fixed eviction knobs.

**Aura Redis** should be:

- **Fast** where bytes move (C kernels: built-in `lru`/`lfu`/`noop` + layout).
- **Alive** where policy lives: Aura **mutates policy code** (`std/hot-strategy` /
  `mutate:rebind`) under sandbox discipline, then applies choices via RESP
  `EVICT` / `LAYOUT`. See [`aura-native-control.md`](aura-native-control.md).

**Not the moat:** swapping eviction `.so` files (`PLUGIN` / dlopen) — that is an
escape hatch only.

That combination is the product story—not “another C Redis with an Aura logo.”

---


**Aura differentiation (post production P0–P3):** Redis-compatible data plane is table stakes; the moat is sandbox + mutate + hot-strategy policy (`policy_agent.aura`). Demand map and backlog: [`aura-demand.md`](aura-demand.md).

## 2. Design principles

1. **Aura owns the process and policy.** The server entry remains an Aura program (or Aura-launched). C is a library, not a fork that abandons Aura.
2. **Prefer Aura-native policy** — `std/hot-strategy` + RESP `EVICT`/`LAYOUT`/`INFO` in a **separate policy agent** (avoids FFI+closure inertness on this Aura rev). `std/ffi` remains for the optional in-process `server_ffi.aura` path.
3. **No Redis-shaped prims in Aura core.** Generic Aura work (if ever) = better strings/buffers/hash/GC/AOT for everyone. RESP, commands, LRU, memtier gates = **aura-redis only**.
4. **C kernels, Aura choose-fn.** Built-in eviction/layout vtables stay in C. Aura mutates the **choose-fn** (code string) and applies the name. `PLUGIN`/.so is optional escape hatch (§4.4).
5. **Two engines for honesty:**
   - `aura` — pure Lisp (demo / correctness reference).
   - `ffi` (default for perf) — C data plane + Aura control plane.

---

## 3. Process architecture

```text
  clients ──TCP──►  aura_redis_server (C data plane)
                      epoll / RESP / dict / lru|lfu|noop / LAYOUT
                      admin: EVICT, LAYOUT, INFO  (PLUGIN = escape hatch)
                            ▲
                            │ RESP (no FFI in agent)
                            │
                    policy_agent.aura (Aura control plane)
                      hot-strategy:register!/swap!/heal!
                      mutate:safety-snapshot / boundary-safe?
                      choose-fn bodies in src/redis/policy/

  Legacy (still supported): server_ffi.aura = FFI serve + inlined adaptive
  (closures after many c-func binds are inert on this Aura rev).
```

**Threading model (v1):** single-threaded event loop in C (Redis-like), called from Aura as “run forever” or “run N ms / until idle.” Aura fibers may run the **supervisor** concurrently later; dict access stays single-threaded until an explicit shard/mutex iteration.

**Sandbox:** Policy agent needs TCP (`AURA_SANDBOX=off` or `effect:network`) but **not** `effect:ffi`. Set `AURA_REDIS_DENY_PLUGIN=1` for the Aura-native profile (`scripts/sandbox-policy-profile.sh`).

---

## 4. C core API (`libaura_redis_core`)

Opaque handles only across the FFI boundary (Aura sees `Opaque` / ints). Strings crossing FFI use Aura `String` or pinned buffers per `c-func` rules (`Int`, `Float`, `String`, `Opaque`, `Void`).

### 4.1 Lifecycle

| Symbol | Sig (conceptual) | Notes |
|--------|------------------|-------|
| `ar_core_create` | `() -> Opaque` | Alloc server/db |
| `ar_core_destroy` | `(Opaque) -> Void` | |
| `ar_core_listen` | `(Opaque, Int port) -> Int` | Bind 127.0.0.1:port (v1 loopback; document) |
| `ar_core_serve_ms` | `(Opaque, Int ms) -> Int` | Pump epoll for up to `ms` (0 = one shot / until idle budget) |
| `ar_core_serve_forever` | `(Opaque) -> Int` | Blocking loop (memtier / production) |

Aura entry sketch:

```aura
(require "std/ffi" all:)
(define lib (c-load "native/build/libaura_redis_core.so"))
(define ar-create (c-func lib "ar_core_create" "() -> Opaque"))
(define ar-listen (c-func lib "ar_core_listen" "(Opaque Int) -> Int"))
(define ar-serve  (c-func lib "ar_core_serve_forever" "(Opaque) -> Int"))
(define core (ar-create))
(ar-listen core port)
(ar-serve core)
```

### 4.2 Data commands (also callable for tests without TCP)

| Symbol | Purpose |
|--------|---------|
| `ar_set` / `ar_get` / `ar_del` / `ar_exists` | String GET/SET/DEL/EXISTS |
| `ar_ping` | Health |

TCP path parses RESP and calls the same internals (one implementation).

### 4.3 Metrics (for adaptive Aura)

Export counters Aura can poll (via `c-func` returning Int, or fill a small C struct read with `c-struct-ref`):

- `ops_total`, `ops_get`, `ops_set`, `hits`, `misses`
- `evicted`, `expired`
- `used_memory` (approx)
- `avg_latency_ns` (EWMA)
- `strategy_id` (current eviction plugin id)

### 4.4 Eviction / layout vtable (the moat hook)

```c
typedef struct ArEvictOps {
  void (*on_get)(void* db, void* entry);
  void (*on_set)(void* db, void* entry);
  int  (*should_evict)(void* db);      /* e.g. over maxmemory */
  int  (*evict_one)(void* db);         /* free one key; 1=ok */
  const char* name;
} ArEvictOps;

int ar_core_set_evict(void* core, const ArEvictOps* ops); /* install */
int ar_core_set_evict_by_name(void* core, const char* name); /* built-ins */
```

**Built-in strategies (C, in-repo):**

| Name | Behavior |
|------|----------|
| `noop` | No eviction (default v1) |
| `lru` | Approximate LRU (Redis-like sampling) |
| `lfu` | Approximate LFU |
| `adaptive` | Blend/switch using local EWMA of hit rate & RSS (C-side helper); Aura may still replace the whole vtable |

**Aura adaptive layer (policy):**

- Reads metrics every T ms.
- Decides strategy name or generates parameters (sample size, thresholds).
- Calls `ar_core_set_evict_by_name` **or** `hot-strategy:swap!` on an Aura function that chooses the next name **or** (later) `hot-update:reload` a strategy `.so`.

Self-modification paths (**primary = Aura-native**):

| Mechanism | Use |
|-----------|-----|
| **`std/hot-strategy` + `mutate:rebind`** | **Primary moat:** swap Aura `choose-fn` bodies; heal via snapshot |
| RESP **`EVICT` / `LAYOUT` / `INFO`** | Apply/observe from policy agent without FFI |
| `std/evolve` | Optional: evolve policy bodies from analytics |
| Direct `ar_core_set_evict_by_name` | C/FFI fast path for built-ins |
| `ar_core_load_evict_plugin` / RESP `PLUGIN` | **Escape hatch only** (denied under `AURA_REDIS_DENY_PLUGIN=1`) |

Details: [`aura-native-control.md`](aura-native-control.md).  
Next: runtime mutation axes for big-tech loads — [`runtime-mutation-explore.md`](runtime-mutation-explore.md).

#### Live plugin reload (Iteration 7 stretch — escape hatch)

```c
int ar_core_load_evict_plugin(ArCore* core, const char* so_path);
/* Plugin exports: const ArEvictOps* ar_plugin_evict_ops(void); */
```

- Call is **safe while `serve_forever` / `serve_ms` runs**: dispatch runs between commands on the single-threaded loop; vtable swap then `dlclose(old)`.
- RESP: `PLUGIN` → status (`name plugin=0|1 reloads=N`); `PLUGIN /path/to.so` → load/reload (`+OK` / `-ERR`).
- Env / CLI: `AURA_REDIS_EVICT_SO` / `--plugin` (startup); Aura FFI: already bound as `ar-load-plugin` in `server_ffi.aura`.
- Sample plugins: `libar_evict_random.so` (`name=random`), `libar_evict_rr.so` (`name=rr`).
- Demo/test: `tests/test_plugin_reload.py` / `scripts/demo-plugin-reload.sh` — mid-traffic swap, persistent + new clients, no reconnect storm.

#### Why not `std/hot-update` / `aot:reload` (deferred)

Aura's `std/hot-update` → `(aot:reload path)` reloads **Aura AOT modules** into the runtime `func_table` (version/region checks, staged constructor registration, epoch bump). Eviction policy packs here are **plain C `ArEvictOps` vtables**, not AOT-emitted Aura modules — wiring them through `aot:reload` would either (a) require packaging policy as Aura AOT with a bridge into the C core, or (b) misuse the AOT loader for unrelated `.so`s. **dlopen + `ar_plugin_evict_ops` is the correct thin path** for this repo; AOT policy packs stay deferred unless/until Aura exports a generic “host vtable plugin” convention.


### 4.5 Layout evolution (Iteration 8)

Descriptor for dict/slab:

| Name | Behavior |
|------|----------|
| `flat` / `flat_hash` | Single open-addressing-style chain hash (v1 default) |
| `hot_cold` | Two hashes: **hot** (working set) + **cold**; GET on cold **promotes** to hot; excess hot **demotes** (approx LRU sample) when hot > ~nkeys/4 |

```c
int ar_core_set_layout(ArCore* core, const char* name); /* flat | hot_cold */
const char* ar_core_layout_name(ArCore* core);
uint64_t ar_core_layout_gen(ArCore* core); /* bumps each migrate */
int ar_core_adapt_layout(ArCore* core);    /* simple GET-heavy rule */
```

Migrate is **synchronous**, single-threaded, between commands (`layout_busy` guard). May block briefly while re-linking entries (fold cold→hot, or allocate cold table). RESP: `LAYOUT` / `LAYOUT hot_cold`. Env: `AURA_REDIS_LAYOUT`, optional `AURA_REDIS_LAYOUT_ADAPTIVE=1` (with adaptive serve_ms pump).

---

## 5. Aura control-plane modules (this repo)

| Module | Role |
|--------|------|
| `src/redis/policy_agent.aura` | **Aura-native control plane** (hot-strategy + RESP EVICT) |
| `src/redis/policy/*.aura` | `choose-fn` body strings for hot-strategy |
| `src/redis/server.aura` | Pure Lisp engine (`AURA_REDIS_ENGINE=aura`) |
| `src/redis/server_ffi.aura` | Optional FFI serve + inlined adaptive (legacy/perf) |
| `src/redis/adaptive.aura` | Choose rules (unit-testable; mirrored in agent bodies) |

Env:

| Var | Meaning |
|-----|---------|
| `AURA_REDIS_ENGINE` | `ffi` (default for benches) \| `aura` |
| `AURA_REDIS_PORT` | port |
| `AURA_REDIS_MAXMEMORY` | bytes; enables eviction |
| `AURA_REDIS_EVICT` | initial strategy name |
| `AURA_REDIS_ADAPTIVE` | `1` = run inlined supervisor in `server_ffi` |
| `AURA_REDIS_POLICY_DEMO` | `1` = policy_agent timed invert+heal demo |
| `AURA_REDIS_DENY_PLUGIN` | `1` = refuse RESP PLUGIN (Aura-native profile) |

---

## 6. Build & run

- Toolchain: same CI image `ghcr.io/cybrid-systems/dev:v1.0.7` (GCC 16).
- Build: `native/CMakeLists.txt` or Makefile → `native/build/libaura_redis_core.so`.
- Script: `scripts/build-native.sh`, `scripts/run-server.sh` selects engine.
- Smoke: extend Python client; add `scripts/memtier-cmp.sh` (ratio vs Redis).
- **Gate:** Totals_aura_ffi / Totals_redis ≥ 0.80 on frozen memtier matrix.

---

## 7. Security & limits (v1)

- Loopback bind until explicitly extended (Aura `tcp-listen` was loopback; C core may open `0.0.0.0` later behind a flag—default stay loopback for parity with current docs).
- No AUTH / AOF / cluster in early iterations.
- FFI = dangerous capability; document sandbox off for server.
- Strategy swap must be crash-safe: install new vtable only at quiescent points (between commands) or under a generation counter.

---

## 8. Non-goals (explicit)

- Adding `redis-*` primitives to **cybrid-systems/aura**.
- Replacing Aura with a standalone C main that never loads Aura (Aura must remain the control plane / story).
- 100% Redis command compatibility before the perf gate.

---

## 9. Success metrics

| Milestone | Metric |
|-----------|--------|
| M1 | FFI GET/SET in-process (no TCP) ≫ Lisp store path |
| M2 | memtier p=1 Totals ≥ 20% Redis |
| M3 | memtier p=1 Totals ≥ 80% Redis |
| M4 | Adaptive swap LRU↔LFU under synthetic load (correctness + telemetry) |
| M5 | Aura-native hot-strategy swap + heal demo (EVICT); PLUGIN documented as escape hatch |
| M6 | Layout migrate (`flat`↔`hot_cold`) under load |

