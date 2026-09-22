# aura-redis

**English** | **中文**

A Redis-compatible **RESP2** server implemented in [Aura](https://github.com/cybrid-systems/aura) (AI-native Lisp from cybrid-systems). This is a **v0 subset** — enough to exercise Aura sockets, hashes, fibers, and strings end-to-end, not a production Redis replacement.

Pinned Aura revision: [`b0c6b3555e4287c8807b019a8b9316ee94c988b6`](https://github.com/cybrid-systems/aura/commit/b0c6b3555e4287c8807b019a8b9316ee94c988b6) (see `AURA_REF`).

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

## Perf notes (v0.2)

| Change | Effect |
|--------|--------|
| **Lazy per-key TTL** | Hot-path get/exists/type/ttl/incr/list/hash touch **one** key; `store-purge!` only for KEYS / DBSIZE |
| **Pipelined write** | `process-buffer` accumulates replies → **one `tcp-send`** per recv burst |
| **`ascii-upcase`** | Build via char list → `list->string` (no O(n²) `string-append`) |
| **Fiber-per-client** | `fiber:spawn` per accept (fallback / `AURA_REDIS_SYNC=1` = sequential) |

Rough local numbers (container `ghcr.io/cybrid-systems/dev:v1.0.7`, N=300, pipeline batch 30; FLUSH between phases):

| Phase | Before (O(n) purge / per-cmd send) | After (lazy TTL + piped write) |
|-------|-------------------------------------|--------------------------------|
| SET sequential | ~52 ops/sec | ~88–100 ops/sec |
| GET sequential | ~61 ops/sec | ~450 ops/sec |
| SET pipelined | ~12 ops/sec | ~18–20 ops/sec (Aura hash/CPU bound) |
| GET pipelined | ~35 ops/sec | ~560 ops/sec |

GET improved ~7–16×; SET ~1.7×. Pipelined SET remains CPU-bound inside Aura (hash writes), not RTT.

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
src/redis/server.aura        listen / accept / fiber-per-client (entry)
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
