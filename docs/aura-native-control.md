# Aura-native control plane

**User-visible story:** Aura redis adapts by **mutating Aura policy code** under
sandbox discipline. The C core only executes the chosen kernels (`lru` / `lfu` /
`noop`, plus layout). Swapping a `.so` (`PLUGIN`) is an **escape hatch**, not the
product moat.

**Product control plane:** `policy_agent.aura` (Docker) is the DEFAULT for
`demo-mvp.sh` and adaptive benches. Python `choose_policy()` only mirrors
`choose_*.aura` for host-only CI (`--python-ctl` / `AURA_AGENT=0`).

## Split process (why)

On the pinned Aura rev, many top-level `(c-func)` binds in one workspace make
**user closures inert** (calls return 0 / garbage). That breaks
`hot-strategy:swap!` if it shares a file with the FFI serve loop
(`server_ffi.aura`).

| Process | Role |
|---------|------|
| **`aura_redis_server` (C)** | Data plane: epoll, RESP, dict, built-in eviction/layout kernels |
| **`policy_agent.aura` (Aura)** | Control plane: workspace policy, `std/hot-strategy`, apply via RESP |

Optional legacy path: `server_ffi.aura` with **inlined** adaptive rules (no
closures after `c-func`) still works for Iteration 6 demos — but it does **not**
exercise mutate/hot-strategy.

## Aura surfaces used

| API | Use |
|-----|-----|
| `std/hot-strategy` → `register!` / `swap!` / `heal!` | Rebind `choose-fn` body strings; snapshot + restore |
| `mutate:rebind` (via hot-strategy) | Pure-Aura strategy denseness |
| `mutate:safety-snapshot` / `mutate:boundary-safe?` | Probe before swap |
| `mutate:summary` | Observability after demo |
| `ast:snapshot` / `ast:restore` (via heal) | Last-good recovery |
| RESP **`EVICT`** / **`LAYOUT`** / **`INFO`** | Apply / observe without FFI in the agent |
| Sandbox: `AURA_SANDBOX` + `AURA_REDIS_DENY_PLUGIN=1` | Network for RESP; **no** `effect:ffi` / PLUGIN |

Optional later: `std/evolve` to evolve policy body text from metrics (not wired
in the v1 agent loop).

## RESP admin commands (C)

```text
EVICT              → bulk current name (noop|lru|lfu|plugin-name)
EVICT lru|lfu|noop → +OK (clears plugin handle if any)
EVICT samples <n>  → set approx eviction sample size (default 16; MVP M4)
LAYOUT [name]      → query / migrate (unchanged)
PIN key / UNPIN key / PIN → pin set (eviction skips; MVP M4)
INFO               → multi-signal metrics (gets/sets/hits/misses/evicted/keys/
                     samples/pinned/evict/layout/…)
PLUGIN [path]      → escape hatch; **denied** when AURA_REDIS_DENY_PLUGIN=1
```

## Control loop (MVP joint)

```text
  INFO deltas ──► choose-fn (Aura, hot-swappable)
                        │
                        ▼
              "lfu|hot_cold|pin"  (or bare "lfu" / "")
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
     EVICT name    LAYOUT name    PIN / samples
```

1. Seed `(define (choose-fn dgets dsets dhits dmisses devicted nkeys) …)`.
2. Each tick: parse `INFO` deltas (+ agent hit% EWMA) → choose-fn.
3. Parse `|` fields → apply `EVICT` and optional `LAYOUT` / pin hint.
4. When agent owns layout, leave `AURA_REDIS_LAYOUT_ADAPTIVE` **off**.
5. On policy update: `mutate:safety-snapshot` → `hot-strategy:swap!` → heal on failure.
6. **A4 canary (optional):** `AURA_REDIS_CANARY=1` → trial body N ticks (`AURA_REDIS_CANARY_TICKS`); fitness drop / poison → `heal!` + audit `canary_heal`; else `canary_commit`. Inject: `AURA_REDIS_CANARY_INJECT=broken|inverted|aggressive`.

Policy bodies: `choose_normal` / `choose_aggressive` / `choose_conservative`
(+ inverted / broken for demos). See [`mvp-plan.md`](mvp-plan.md).

## Sandbox profile

See `scripts/sandbox-policy-profile.sh`:

- Demo/dev: `AURA_SANDBOX=off` (Soft) so TCP works without tenant authority.
- Always set `AURA_REDIS_DENY_PLUGIN=1` for the Aura-native story so `.so`
  reload cannot silently become the adaptation path.
- Restricted + `security:grant-effect!` for `network` (not `ffi`) is the
  production-shaped target when TA is available.

## Contrast vs PLUGIN.so

| | Aura-native (this doc) | PLUGIN / dlopen |
|--|------------------------|-----------------|
| What changes under load | **Aura code** (`choose-fn` body) | Native vtable in a `.so` |
| Safety | mutate boundary + snapshot/heal | process-level dlopen risk |
| Sandbox | no `effect:ffi` required for agent | needs ffi / DENY_PLUGIN off |
| Role | **product moat** | escape hatch / Iteration 7 stretch |

## How to run

```bash
./scripts/build-native.sh
./scripts/demo-mvp.sh                          # ~2 min distinctive MVP story
python3 tests/test_mvp.py                      # INFO/PIN/samples/choose unit+smoke
python3 tests/test_aura_native.py              # EVICT/INFO/DENY_PLUGIN smoke
python3 tests/test_aura_native.py --unit-aura  # hot-strategy swap+heal in docker
./scripts/demo-aura-native.sh                  # full: C server + policy agent + loads
```


## Production HA (P1.9)

Fail-safe contract:

1. **C data plane owns the live kernel.** `EVICT` / `LAYOUT` state lives in
   `aura_redis_server`. If `policy_agent` dies or its TCP session drops, the
   server **keeps the last applied kernel** — it does not revert to `noop`.
2. **Reconnect + exponential backoff.** On INFO/TCP failure the agent closes
   the socket and reconnects with backoff (`AURA_REDIS_POLICY_BACKOFF_CAP_MS`,
   default 4000). Optional `AURA_REDIS_PASSWORD` / `REQUIREPASS` for AUTH.
3. **Re-apply last choice after reconnect.** The agent remembers `*last-evict*` /
   `*last-layout*` from its own applies and re-asserts them if the server
   differs (covers in-process drops). After a full process crash/restart the
   agent re-reads `INFO` and continues from the server’s current kernel.
4. **Policy pin log.** Each connect/reconnect logs
   `policy_agent: policy-pin tag=… profile=… version=… last-evict=…`.
5. **Optional heartbeat file.** `AURA_REDIS_POLICY_HEARTBEAT=/path` is rewritten
   each tick (`ts_ms`, `profile`, `version`, `last_evict`, `reconnects`, …).

Test: `python3 tests/test_prod_policy_ha.py` (Docker Aura agent + C server).

## Files

- `src/redis/policy_agent.aura` — agent loop
- `src/redis/policy/choose_*.aura` — body strings
- `scripts/demo-aura-native.sh` / `scripts/sandbox-policy-profile.sh`
- `tests/test_hot_strategy_policy.aura` / `tests/test_aura_native.py`
