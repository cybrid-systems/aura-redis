# aura-redis

**English** | **中文**

A Redis-compatible **RESP2** server implemented in [Aura](https://github.com/cybrid-systems/aura) (AI-native Lisp from cybrid-systems). This is a **v0 subset** — enough to exercise Aura sockets, hashes, fibers, and strings end-to-end, not a production Redis replacement.

Pinned Aura revision: [`b0c6b3555e4287c8807b019a8b9316ee94c988b6`](https://github.com/cybrid-systems/aura/commit/b0c6b3555e4287c8807b019a8b9316ee94c988b6) (see `AURA_REF`).

---

## Architecture / 架构

Detailed design: [`docs/architecture.md`](docs/architecture.md)  
Iteration plan: [`docs/iteration-plan.md`](docs/iteration-plan.md)

**Direction:** C data plane via Aura `std/ffi` + Aura control plane for adaptive eviction/layout (self-modification). Pure Lisp engine remains as `AURA_REDIS_ENGINE=aura`.

---


## Hybrid engine (C data plane) / 混合引擎

| Env | Meaning |
|-----|---------|
| `AURA_REDIS_ENGINE` | `ffi` (C epoll/RESP/dict via `std/ffi`) or `aura` (pure Lisp) |
| `AURA_REDIS_CORE_SO` | path to `libaura_redis_core.so` |
| `AURA_REDIS_MAXMEMORY` | bytes; enables eviction when strategy ≠ `noop` |
| `AURA_REDIS_EVICT` | `noop` \| `lru` \| `lfu` |

```bash
./scripts/build-native.sh
./scripts/run-server-ffi.sh 6379          # Aura+FFI in dev container (host network)
AURA_REDIS_ENGINE=ffi ./scripts/smoke-test.sh
./scripts/memtier-cmp.sh                 # vs redis:7-alpine → docs/perf-log.md
python3 tests/test_eviction.py --evict lru
```

**Perf (2026-09-22):** C data plane memtier p=1 **~1.13× Redis**, p=16 **~1.30× Redis**; Lisp path ~143 ops/s. Details: [`docs/perf-log.md`](docs/perf-log.md).

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
3. **No persistence** — no RDB/AOF, no AUTH, no pub/sub, no cluster.
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
src/redis/server_ffi.aura    FFI control plane → C serve_forever
src/redis/ffi_boot.aura      in-process FFI helpers
native/                      libaura_redis_core.so (epoll/RESP/dict/evict)
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
