# aura-redis

**English** | **中文**

A Redis-compatible **RESP2** server implemented in [Aura](https://github.com/cybrid-systems/aura) (AI-native Lisp from cybrid-systems). This is an **Aura-adaptive RESP cache/KV** (string-focused C data plane + Aura `policy_agent`) — **not** a full Redis Cluster replacement. Production bar: [`docs/production-plan.md`](docs/production-plan.md).

Pinned Aura revision: [`b0c6b3555e4287c8807b019a8b9316ee94c988b6`](https://github.com/cybrid-systems/aura/commit/b0c6b3555e4287c8807b019a8b9316ee94c988b6) (see `AURA_REF`).

---

## Architecture / 架构

Detailed design: [`docs/architecture.md`](docs/architecture.md)  
Iteration plan: [`docs/iteration-plan.md`](docs/iteration-plan.md)  
**Production plan (P0–P3):** [`docs/production-plan.md`](docs/production-plan.md) ← **active ship track**  
Command contract (C data plane): [`docs/commands.md`](docs/commands.md)  
**MVP plan (M0–M5):** [`docs/mvp-plan.md`](docs/mvp-plan.md) (done)  
High-ROI M6–M12: [`docs/high-roi-iterations.md`](docs/high-roi-iterations.md) (done)  
Runtime mutation exploration: [`docs/runtime-mutation-explore.md`](docs/runtime-mutation-explore.md)

**Direction:** C data plane for fast GET/SET + built-in kernels; **Aura mutates policy code** (`hot-strategy` / sandbox) and applies via RESP `EVICT`/`LAYOUT`. PLUGIN/.so is an escape hatch only — see [`docs/aura-native-control.md`](docs/aura-native-control.md).

---


## Hybrid engine (C data plane) / 混合引擎

| Env | Meaning |
|-----|---------|
| `AURA_REDIS_ENGINE` | `ffi` (C epoll/RESP/dict via `std/ffi`) or `aura` (pure Lisp) |
| `AURA_REDIS_CORE_SO` | path to `libaura_redis_core.so` |
| `AURA_REDIS_MAXMEMORY` | bytes; enables eviction when strategy ≠ `noop` |
| `AURA_REDIS_EVICT` | `noop` \| `lru` \| `lfu` |
| `AURA_REDIS_ADAPTIVE` | `1` = Aura supervisor polls metrics and swaps `lru`↔`lfu` via `serve_ms` pump |
| `AURA_REDIS_EVICT_SO` | path to eviction plugin `.so` (**escape hatch**; overrides name) |
| `AURA_REDIS_DENY_PLUGIN` | `1` = refuse RESP `PLUGIN` (Aura-native profile) |
| `AURA_REDIS_POLICY_DEMO` | `1` = timed hot-strategy invert+heal in `policy_agent` |
| `AURA_REDIS_LAYOUT` | `flat` \| `hot_cold` (Iteration 8 dict layout) |
| `AURA_REDIS_LAYOUT_ADAPTIVE` | `1` = adapt layout from adaptive tick |

```bash
./scripts/build-native.sh
./scripts/run-server-ffi.sh 6379          # Aura+FFI in dev container (host network)
AURA_REDIS_ADAPTIVE=1 ./scripts/run-server-ffi.sh 6379   # adaptive control plane
./scripts/demo-adaptive.sh               # two load patterns → strategy swaps in logs
AURA_REDIS_ENGINE=ffi ./scripts/smoke-test.sh
./scripts/bench-e2e.sh                     # e2e: throughput + hit-rate → docs/perf-eval.md
./scripts/memtier-cmp.sh                 # vs redis:7-alpine → docs/perf-log.md
python3 tests/test_eviction.py --evict lru
python3 tests/test_adaptive.py --spawn
python3 tests/test_evict_plugin.py          # dlopen random eviction plugin
python3 tests/test_plugin_reload.py         # live PLUGIN swap mid-traffic (no reconnect storm)
./scripts/demo-plugin-reload.sh            # same as above
python3 tests/test_layout.py                # flat↔hot_cold migrate under load
```


### Demo MVP (distinctive story)

```bash
./scripts/build-native.sh
./scripts/demo-mvp.sh                 # ~2 min: LRU lose → Aura adaptive win → WS shift
python3 tests/test_mvp.py
python3 scripts/bench_regret.py          # HEADLINE: phase_marathon cum hit% / regret
python3 scripts/bench_dynamic_evict.py --workloads phase_marathon,zipf_hotkey,oscillate  # Aura policy_agent default
```

Under Meta-like hot-key / Zipf pressure, **static LRU hit% collapses**; Aura-mutated
policy (`choose_normal` → joint `EVICT`+`LAYOUT`, optional `PIN`) recovers ~100%.
C only runs named kernels. `AURA_REDIS_DENY_PLUGIN=1`. Details: [`docs/mvp-plan.md`](docs/mvp-plan.md).

### Aura-native adaptation (moat)

```bash
./scripts/demo-aura-native.sh              # C server + policy_agent hot-strategy + loads
python3 tests/test_aura_native.py          # EVICT / INFO / DENY_PLUGIN smoke
python3 tests/test_aura_native.py --unit-aura
source scripts/sandbox-policy-profile.sh   # DENY_PLUGIN=1; no ffi required for agent
./scripts/bench-dynamic-evict.sh           # LRU vs LFU vs adaptive hit-rate workloads
```

Story: mutate `choose-fn` under sandbox → `EVICT lru|lfu|noop`. Not “swap a .so”.

**Perf (2026-09-22 e2e):** C data plane memtier vs Redis **~1.15–1.35×** (lru/adaptive); Lisp ~14 ops/s. Adaptive wins **cumulative** hit% on `phase_marathon` (+18pp vs LRU, +54pp vs LFU); zipf adaptive≈LFU (0pp regret). Do not cite adaptive for throughput. Full tables: [`docs/perf-eval.md`](docs/perf-eval.md). Also [`docs/perf-log.md`](docs/perf-log.md), [`docs/workloads.md`](docs/workloads.md).

Loopback bind **127.0.0.1** for both engines (documented).

---
## Purpose / 目的

| EN | 中文 |
|----|------|
| Prove Aura can host a small network service | 验证 Aura 可承载小型网络服务 |
| CI builds Aura itself inside the official image | CI 在官方镜像内编译 Aura 本体 |
| Document Aura TCP / RESP / fiber patterns | 沉淀 Aura TCP / RESP / fiber 用法 |

---

## Limitations / 限制

1. **Loopback only** — Aura `tcp-listen` binds **`127.0.0.1` only**. Remote clients cannot connect.
2. **Shared in-memory store** — all clients share one hash; no mutex (cooperative / coarse correctness for demo). Prefer `AURA_REDIS_SYNC=1` if `fiber:spawn` misbehaves in a sandbox.
3. **Persistence optional** — default cache-only; P2.13 `aura-rdb` SAVE/BGSAVE for warm-start (not full Redis RDB/AOF).
4. **KEYS** — supports `*` (all) and prefix globs like `foo*`; other patterns are exact match only.

---

## Perf notes (v0.2 → memtier hot-path)

| Change | Effect |
|--------|--------|
| **Constant RESP replies** | `*RESP-OK*` / `*RESP-PONG*` / `*RESP-NULL*` / `:0` / `:1` — no `string-append` on OK/PONG/null |
| **`cmd-eq?` / `ascii-cmd=?`** | Case-insensitive command match **without** allocating `ascii-upcase` on every dispatch |
| **SET/GET fast paths** | Exactly-3-arg SET skips `parse-set-opts`; 2-arg GET minimal touch+bulk; in-place `store-set-string!` mutate |
| **`store-touch!`** | Skip `current-time-ms` when `ex==0` |
| **O(n) list builds** | `resp-cmd-args` / `resp-parse-array` / `args-from` use `cons`+`reverse` (not `append`) |
| **Per-reply `tcp-send`** | Pipeline replies sent individually (beats O(n²) join; measured better than batched join for p=16) |
| **Lazy per-key TTL** | Hot-path touch one key; `store-purge!` only for KEYS / DBSIZE |
| **Fiber-per-client** | `fiber:spawn` per accept (`AURA_REDIS_SYNC=1` = sequential) |

memtier (`1c/1t`, SET:GET=1:10, 32B, key 1..10000 R:R; aura N=2000, container `ghcr.io/cybrid-systems/dev:v1.0.7`):

| | Totals ops/s | SET | GET |
|--|--|--|--|
| aura p=1 (before) | 112 | 10 | 102 |
| aura p=1 (after) | **~143** | **~13** | **~130** |
| aura p=16 (before) | 29 | 2.7 | 27 |
| aura p=16 (after) | **~81** | **~7.4** | **~74** |

Pipeline no longer collapses throughput (was worse than p=1). Absolute ops/s remain Aura interpreter / hash-write bound vs Redis (~38k / ~338k).

Run: `python3 scripts/bench.py --port 16379 -n 300 --pipeline 30`

---

## Command coverage / 命令覆盖

| Command | Status | Notes |
|---------|--------|-------|
| PING | ✅ | optional message → bulk reply |
| ECHO | ✅ | |
| GET / SET | ✅ | SET supports optional `EX` / `PX` / `NX` / `XX` |
| SETEX / PSETEX | ✅ | |
| MGET / MSET | ✅ | |
| APPEND / STRLEN / GETSET | ✅ | |
| DEL / UNLINK / EXISTS | ✅ | multi-key; UNLINK ≡ DEL |
| INCR / DECR | ✅ | integer strings |
| KEYS | ✅ | `*` and prefix `foo*` |
| DBSIZE | ✅ | |
| INFO | ✅ | simple bulk: version, keys, uptime |
| RENAME / RENAMENX | ✅ | |
| FLUSHDB | ✅ | |
| TYPE | ✅ | `string` / `list` / `hash` / `none` |
| TTL / EXPIRE | ✅ | lazy expiry on access |
| QUIT | ✅ | |
| LPUSH / RPUSH / LPOP / RPOP / LLEN | ✅ | |
| HSET / HGET / HDEL / HGETALL | ✅ | |

---

## Develop with the CI container / 用 CI 容器开发

The box/host may lack Docker or GCC 16. Aura’s official toolchain lives in:

```text
ghcr.io/cybrid-systems/dev:v1.0.7
```

Run the same steps CI runs:

```bash
docker run --rm -v "$PWD:/work" -w /work \
  ghcr.io/cybrid-systems/dev:v1.0.7 \
  ./scripts/ci-in-container.sh
```

(`podman` works the same way.)

Inside the container this script:

1. `scripts/fetch-aura.sh` — clone `cybrid-systems/aura` @ `AURA_REF` → `.deps/aura`
2. `scripts/build-aura.sh` — `./build.py build` (RelWithDebInfo + mold defaults)
3. `scripts/smoke-test.sh` — start server + Python RESP smoke client

Reuse a prebuilt binary (skip ~18min compile):

```bash
docker run --rm -v "$PWD:/work" -w /work \
  -e AURA_BIN=/work/.deps/aura/build/aura \
  ghcr.io/cybrid-systems/dev:v1.0.7 \
  ./scripts/smoke-test.sh
```

---

## Local scripts (once Aura is built)

```bash
./scripts/fetch-aura.sh
./scripts/build-aura.sh          # needs GCC 16 / mold — prefer the container
./scripts/run-server.sh [port]   # sets AURA_REDIS_PORT (default 6379); AURA_SANDBOX=off
./scripts/smoke-test.sh          # ephemeral port 16379
python3 scripts/bench.py --port 6379 -n 2000
```

Environment used by the server:

| Variable | Value |
|----------|-------|
| `AURA_SANDBOX` | `off` (required for TCP) |
| `AURA_PIPELINE_STRICT` | `0` |
| `AURA_PATH` | `.deps/aura/lib` |
| `AURA_BIN` | override path to `aura` binary |
| `AURA_REDIS_PORT` | listen port (default 6379) |
| `AURA_REDIS_SYNC` | `1` → sequential clients (no `fiber:spawn`) |

Manual client (any RESP client):

```bash
python3 tests/smoke_client.py --port 6379
```

Optional RESP unit checks (needs `aura` binary):

```bash
AURA_SANDBOX=off AURA_PATH=.deps/aura/lib \
  ./.deps/aura/build/aura tests/test_resp.aura
```

---

## Layout

```text
AURA_REF                     pinned Aura SHA
src/redis/resp.aura          RESP2 encode/decode
src/redis/store.aura         in-memory KV (+ list/hash); lazy TTL
src/redis/commands.aura      command dispatch
src/redis/server.aura        pure Lisp engine (AURA_REDIS_ENGINE=aura)
src/redis/policy_agent.aura  Aura-native control (hot-strategy → EVICT)
src/redis/policy/            choose-fn body strings
src/redis/server_ffi.aura    optional FFI serve + inlined adaptive
src/redis/ffi_boot.aura      in-process FFI helpers
docs/aura-native-control.md  sandbox + mutation + hot-strategy story
docs/runtime-mutation-explore.md  big-tech loads → mutation axes + Iter 10+
native/                      libaura_redis_core.so + aura_redis_server
scripts/build-native.sh
scripts/run-server-ffi.sh
scripts/memtier-cmp.sh
scripts/fetch-aura.sh
scripts/build-aura.sh
scripts/run-server.sh
scripts/smoke-test.sh
scripts/ci-in-container.sh
scripts/bench.py             SET/GET ops/sec helper
tests/smoke_client.py        pure Python RESP client
tests/test_resp.aura
.github/workflows/ci.yml     container: ghcr.io/cybrid-systems/dev:v1.0.7
```

---

## CI

Workflow **CI** job `build-aura + aura-redis-smoke`:

- Runner: `ubuntu-latest` + container `ghcr.io/cybrid-systems/dev:v1.0.7`
- Timeout: 120 minutes (Aura builds are long)
- Fails if Aura does not compile
- Then runs `scripts/smoke-test.sh` (core + MGET/MSET/APPEND/… + pipeline + concurrent)

Optional non-failing bench (local / after smoke):

```bash
python3 scripts/bench.py --port 16379 -n 1000 --pipeline 50 || true
```

---

## License

Apache License 2.0 (same as Aura).

## TLS (production P2.15)

Optional native OpenSSL on `--tls-port` (cleartext `--port` kept for `policy_agent`). See [`docs/tls.md`](docs/tls.md). Soft build: works without OpenSSL (`AURA_REDIS_TLS=OFF`).

## Persistence / restart semantics (production v1)

**aura-redis is a cache/KV data plane by default — not a durable store.**

On process restart (clean or crash) **without** a prior `SAVE`/`BGSAVE`:

- All keys are **gone**.
- Runtime `CONFIG SET` knobs reset to CLI/env/defaults.
- The last `EVICT` / `LAYOUT` kernel also resets to server startup flags (`--evict`, `--layout`).
- Aura `policy_agent` will reconnect and re-choose policy from live `INFO` (see P1.9 HA).

**Optional warm-start (P2.13):** `SAVE` / `BGSAVE` write a custom **`aura-rdb`** dump (string keys + TTL). Restart with `--dir` / `--dbfilename` (or env) reloads it. Not Redis-RDB compatible — see [`docs/persistence.md`](docs/persistence.md).

