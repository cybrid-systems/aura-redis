# aura-redis production plan

**Status:** authoritative production roadmap (v1).  
**Reality check:** tip ~`e54f025`++ is ~2.2k LOC C data plane + Aura `policy_agent` control plane — a **string KV / adaptive cache** with RESP2, maxmemory eviction, and Aura-mutated policy. It is **not** Redis Cluster, modules, or full command compatibility. Production here means *production for this product*, not “become Redis overnight.”

**Companions:** [`iteration-plan.md`](iteration-plan.md) · [`mvp-plan.md`](mvp-plan.md) · [`aura-native-control.md`](aura-native-control.md) · [`commands.md`](commands.md) · [`mutation-gains.md`](mutation-gains.md)

**Invariant:** Aura `policy_agent` stays the product control plane. `AURA_REDIS_DENY_PLUGIN=1` for Aura-native demos. Redis-specific work stays in **this** repo; do not grow aura-grok / Aura core for Redis-shaped prims.

---

## Product definition (production bar)

**Aura-redis production v1** = **single-node RESP cache/KV** with:

| Pillar | Bar |
|--------|-----|
| Correctness | Safe under concurrency + pipelining; RESP edge cases handled; wrong-arity / unknown-command Redis-ish errors |
| Memory | Accurate `used_memory`; `maxmemory` never unbounded growth; eviction + TTL correctness |
| Operability | `CONFIG`-class runtime knobs, structured `INFO`, logging, graceful shutdown / drain |
| Security baseline | `AUTH` / `requirepass`, protected-mode / bind defaults (loopback today → explicit policy) |
| Observability + gates | Latency/stats; regression suite as CI gate; soak N minutes |
| Aura control plane | Reconnect, backoff, policy version pin, health; fail-safe if agent dies (stay on last `EVICT`) |
| Mutation story | Productionize Aura mutation — do **not** strip it for “plain Redis” |

### Explicit non-goals for v1

- Redis Cluster / sharding / slot migration  
- Redis module API  
- Full Redis command / data-type compatibility  
- Disk persistence — **optional phase** (P2); cache-only + documented restart semantics is acceptable for v1  
- Matching Redis absolute throughput on every workload (already competitive on hot path; not the ship gate)

---

## Priority bands

### P0 — correctness & safety (ship blockers)

| # | Item | Goal | Exit criteria (measurable) | Test command | Risk |
|---|------|------|----------------------------|--------------|------|
| **P0.1** | Command contract + RESP edges | Catalog supported/unsupported; harden parser (pipeline, big bulk, partial reads, null bulk, arity) | `tests/test_prod_protocol.py` green; [`commands.md`](commands.md) lists every dispatched command | `python3 tests/test_prod_protocol.py` | Silent protocol desync; client hangs; crash on null bulk |
| **P0.2** | Memory accounting + maxmemory | `used_memory` matches entry accounting; never unbounded under eviction | After fill past maxmemory: `used_memory ≤ maxmemory + slack`; `evicted > 0`; `tests/test_prod_memory.py` green | `python3 tests/test_prod_memory.py` | OOM / RSS blowup; false INFO; 64-cycle eviction guard undershoots |
| **P0.3** | TTL / EXPIRE under eviction | Lazy expire + active sampling; TTL keys interact correctly with `ttl_aware` / LRU | Expire removes keys; under pressure TTL deadlines respected; no use-after-expire | `python3 tests/test_prod_ttl.py` (when added) | Stale hits; expire counter drift; avg_ttl skew |
| **P0.4** | AUTH + protected-mode | `requirepass` / `AUTH`; default bind/protected-mode honest for deploy | Unauthed commands denied when pass set; docs for loopback vs bind | `python3 tests/test_prod_auth.py` | Accidental open bind; break policy_agent (must AUTH) |
| **P0.5** | Graceful shutdown | SIGTERM/INT drain; no corruption if persistence off (clean empty restart) | Signal → quit; listen closed; in-flight replies flushed or connections reset cleanly | manual + smoke with terminate | Half-open clients; epoll fd leak |
| **P0.6** | Structured INFO + ops stats | Redis-ish sections; latency/ops counters for ops + Aura | INFO parseable sections; counters monotonic under load | extend protocol/mvp tests | Agent parse break if format churns |
| **P0.7** | Regression + soak CI gate | Unit + integration suite; soak N minutes | CI job fails on red; soak script documents N (e.g. 5–10 min) | `scripts/ci-in-container.sh` + prod tests | Flaky ports; long CI |

### P1 — operability & Aura control plane prod

| # | Item | Goal | Exit criteria | Test command | Risk |
|---|------|------|---------------|--------------|------|
| **P1.8** | CONFIG GET/SET | Persist/runtime knobs (`maxmemory`, `evict-samples`, etc.) | Round-trip CONFIG; survives documented restart policy | `tests/test_prod_config.py` | Agent fights CONFIG; knob skew |
| **P1.9** | policy_agent HA | Reconnect + backoff; policy version pin; health; **fail-safe: stay on last EVICT if agent dies** | Kill agent → kernel unchanged; reconnect applies new policy | demo + agent kill test | Silent freeze on bad policy; flap |
| **P1.10** | Logging / metrics | Structured logs; Redis-compatible INFO enough or export; slowlog-ish | Slow command threshold countable | INFO + log grep | Log volume |
| **P1.11** | Persistence optional **or** document cache-only | RDB/simple snapshot **or** explicit restart = empty cache | Doc + README semantics; if snapshot: reload smoke | docs / optional test | False durability claims |
| **P1.12** | Client limits | `maxclients`, idle timeout, TCP backlog | Over-limit rejected; timeout closes idle | connection flood test | DoS via AR_MAX_CONN |

### P2 — durability / HA stretch

| # | Item | Goal | Exit criteria | Test | Risk |
|---|------|------|---------------|------|------|
| **P2.13** | aura-rdb snapshot | Durable string+TTL warm-start (`SAVE`/`BGSAVE`) | Reload after kill restores GET/TTL | `tests/test_prod_rdb.py` | Perf / fsync policy |
| **P2.14** | Replication `REPLICAOF` | Async single replica (string KV) | Replica converges under SET; READONLY writes | `tests/test_prod_replica.py` | Split brain without cluster story |
| **P2.15** | TLS | Encrypted client path | stunnel or native TLS accept | TLS client smoke | Cert ops; Aura agent TLS |

### P3 — compatibility expansion

| # | Item | Goal | Exit criteria | Test | Risk |
|---|------|------|---------------|------|------|
| **P3.16** | More types (HASH/LIST/ZSET) | Only as product demand | Per-type smoke + memory accounting | type tests | used_memory complexity |
| **P3.17** | MULTI/EXEC, Pub/Sub | Only if demanded | Spec’d subset green | dedicated tests | Pipeline interaction |

---

## Track status (as of production kickoff)

| Track | Status |
|-------|--------|
| Design + C RESP/KV + eviction + layout (iters 0–8) | **DONE** |
| Aura-native control (iter 9) | **DONE** |
| MVP M0–M5 | **DONE** |
| High-ROI M6–M12 (mutation, TTL kernel, soft-goal, evolve, prefix POLICY) | **DONE** |
| **Production P0** | **P0.1–P0.7 DONE** |
| **P1.1 / P1.8 CONFIG** | **DONE** (runtime; no persist) |
| **P1.9 policy_agent HA** | **DONE** — reconnect/backoff, re-apply last EVICT/LAYOUT, policy-pin log, heartbeat; fail-safe documented |
| **P1.12 client limits** | **DONE** — maxclients / timeout / tcp-backlog (CONFIG + CLI); `tests/test_prod_clients.py` |
| **P1.11 cache-only restart** | **DONE** — README + `docs/persistence.md` (cache-only default; optional P2.13) |
| **P1.10 latency/slowlog** | **DONE** — INFO cmd_* histogram + slowlog_count; CONFIG slowlog-log-slower-than |
| **P2.13 aura-rdb** | **DONE** — SAVE/BGSAVE + startup load; `tests/test_prod_rdb.py` |
| **P2.14 REPLICAOF** | **DONE** — async single replica, read-only GET; `tests/test_prod_replica.py` |
| **Production P1–P3** | **ACTIVE** ← P2.15 TLS optional next |

MVP/explore remains valuable demos; **ship bar moves to this document.**

---

## How we iterate

1. Implement **one P0 item at a time, in order**.  
2. **Commit + push `main`** after each item (CI wait optional per project preference).  
3. Keep `DENY_PLUGIN` / Aura policy story in demos and prod profile.  
4. Exit criteria must be **automatable** (`tests/test_prod_*.py`).

### Run production gates (current)

```bash
./scripts/build-native.sh
python3 tests/test_prod_protocol.py   # P0.1
python3 tests/test_prod_memory.py     # P0.2
python3 tests/test_prod_ttl.py        # P0.3
python3 tests/test_prod_auth.py       # P0.4
python3 tests/test_prod_shutdown.py   # P0.5
python3 tests/test_prod_info.py       # P0.6
AURA_REDIS_SOAK_SEC=30 python3 tests/test_prod_soak.py  # P0.7
./scripts/ci-prod.sh              # P0.1–P0.7 gate
```

---

## Aura control plane in production

Production does **not** mean “C-only Redis clone.”

- Data plane: `aura_redis_server` (C) — GET/SET/evict kernels, RESP.  
- Control plane: `policy_agent.aura` — `hot-strategy` / fitness / evolve → RESP `EVICT` / `LAYOUT` / `PIN` / `POLICY`.  
- Fail-safe (P1.9 **DONE**): if agent disconnects, **last applied kernel remains**; reconnect re-applies last EVICT/LAYOUT; policy-pin + optional heartbeat.  
- Sandbox profile: `AURA_REDIS_DENY_PLUGIN=1` (see `scripts/sandbox-policy-profile.sh`).

---

## Changelog (production track)

| Date (CST) | Item | Notes |
|------------|------|-------|
| 2026-09-22 | Plan freeze | This doc; iteration-plan → production track |
| 2026-09-22 | **P0.1** | `docs/commands.md`; RESP harden (null bulk, length digits, 16MiB cap); `tests/test_prod_protocol.py` |
| 2026-09-22 | **P0.2** | `maybe_evict` scales with nkeys (was 64); `tests/test_prod_memory.py` maxmemory bound + large-SET |
| 2026-09-22 | **P0.3** | Active expire in serve loop; expire-if-needed in eviction samples; `tests/test_prod_ttl.py` |
| 2026-09-22 | **P0.4** | `AUTH`/`requirepass`/`--bind`/`protected-mode`; `tests/test_prod_auth.py` |
| 2026-09-22 | **P0.5** | SIGTERM/SIGINT drain; `tests/test_prod_shutdown.py` clean exit 0 |
| 2026-09-22 | **P0.6** | INFO Server/Clients/Memory/Stats/Keyspace/Persistence/Aura; flat keys for policy_agent; `tests/test_prod_info.py` |
| 2026-09-22 | **P0.7** | `scripts/prod-soak.sh` + `tests/test_prod_soak.py`; `scripts/ci-prod.sh` P0.1–P0.7; CI workflow note |
| 2026-09-22 | **P1.1** (start) | `CONFIG GET/SET` maxmemory/requirepass/protected-mode/evict-samples/bind; `tests/test_prod_config.py` |
| 2026-09-22 | **P1.9** | policy_agent reconnect+backoff; re-apply last EVICT/LAYOUT; policy-pin + heartbeat; fail-safe docs; `tests/test_prod_policy_ha.py` |
| 2026-09-23 | **P1.12** | maxclients / idle timeout / tcp-backlog; CONFIG GET/SET; `tests/test_prod_clients.py` |
| 2026-09-23 | **P1.11** | Document cache-only restart (README + docs/persistence.md) |
| 2026-09-23 | **P1.10** | INFO latency histogram + slowlog_count; CONFIG slowlog-log-slower-than; `tests/test_prod_slowlog.py` |
| 2026-09-23 | **P2.13** | Optional `aura-rdb` SAVE/BGSAVE + `--dir`/`--dbfilename` load; `docs/persistence.md`; `tests/test_prod_rdb.py` |
| 2026-09-23 | **P2.14** | `REPLICAOF`/`SYNC` best-effort async replica; read-only slave; `tests/test_prod_replica.py` |
