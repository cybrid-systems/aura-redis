# aura-redis

**English** | **中文**

A Redis-compatible **RESP2** server implemented in [Aura](https://github.com/cybrid-systems/aura) (AI-native Lisp from cybrid-systems). This is a **v0 subset** — enough to exercise Aura sockets, hashes, and strings end-to-end, not a production Redis replacement.

Pinned Aura revision: [`b0c6b3555e4287c8807b019a8b9316ee94c988b6`](https://github.com/cybrid-systems/aura/commit/b0c6b3555e4287c8807b019a8b9316ee94c988b6) (see `AURA_REF`).

---

## Purpose / 目的

| EN | 中文 |
|----|------|
| Prove Aura can host a small network service | 验证 Aura 可承载小型网络服务 |
| CI builds Aura itself inside the official image | CI 在官方镜像内编译 Aura 本体 |
| Document Aura TCP / RESP patterns | 沉淀 Aura TCP / RESP 用法 |

---

## Limitations / 限制

1. **Loopback only** — Aura `tcp-listen` binds **`127.0.0.1` only**. Remote clients cannot connect.
2. **Sequential clients** — one connection is fully served before the next `accept` (no concurrent client fibers in v0).
3. **In-memory** — no RDB/AOF persistence, no AUTH, no pub/sub, no cluster.
4. **KEYS** — pattern matching is simplistic (`*` = all keys).

---

## Command coverage / 命令覆盖

| Command | Status | Notes |
|---------|--------|-------|
| PING | ✅ | optional message → bulk reply |
| ECHO | ✅ | |
| GET / SET | ✅ | SET supports optional `EX` / `PX` / `NX` / `XX` |
| DEL / EXISTS | ✅ | multi-key |
| INCR / DECR | ✅ | integer strings |
| KEYS | ✅ | `*` (all) |
| FLUSHDB | ✅ | |
| TYPE | ✅ | `string` / `list` / `hash` / `none` |
| TTL / EXPIRE | ✅ | |
| QUIT | ✅ | |
| LPUSH / RPUSH / LPOP / RPOP / LLEN | ✅ | stretch |
| HSET / HGET / HDEL / HGETALL | ✅ | stretch |

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

---

## Local scripts (once Aura is built)

```bash
./scripts/fetch-aura.sh
./scripts/build-aura.sh          # needs GCC 16 / mold — prefer the container
./scripts/run-server.sh [port]   # default 6379; AURA_SANDBOX=off
./scripts/smoke-test.sh          # ephemeral port 16379
```

Environment used by the server:

| Variable | Value |
|----------|-------|
| `AURA_SANDBOX` | `off` (required for TCP) |
| `AURA_PIPELINE_STRICT` | `0` |
| `AURA_PATH` | `.deps/aura/lib` |
| `AURA_BIN` | override path to `aura` binary |

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
src/redis/store.aura         in-memory KV (+ list/hash)
src/redis/commands.aura      command dispatch
src/redis/server.aura        listen / accept loop (entry)
scripts/fetch-aura.sh
scripts/build-aura.sh
scripts/run-server.sh
scripts/smoke-test.sh
scripts/ci-in-container.sh
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
- Then runs `scripts/smoke-test.sh` (PING / SET / GET / DEL / INCR / EXISTS / FLUSHDB + pipeline)

---

## License

Apache License 2.0 (same as Aura).
