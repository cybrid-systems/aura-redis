# aura-redis ops runbook (v0 — Tier 2 staging/canary)

**Audience:** operators bringing a single-node aura-redis into staging/canary.  
**Product:** RESP2 cache/KV + Aura `policy_agent` control plane — **not** Redis Cluster.  
**Companions:** [`prod-profile.md`](prod-profile.md) · [`client-allowlist.md`](client-allowlist.md) · [`persistence.md`](persistence.md) · [`tls.md`](tls.md) · [`commands.md`](commands.md) · [`production-plan.md`](production-plan.md)

**Invariant:** `AURA_REDIS_DENY_PLUGIN=1`. Do not ship with PLUGIN/.so as the adaptation path.

---

## 1. Bind / AUTH / TLS

| Concern | Guidance |
|---------|----------|
| **Bind** | Default loopback / protected-mode. For staging: `--bind 0.0.0.0` only behind network policy; keep `protected-mode yes` unless AUTH is set. |
| **AUTH** | `--requirepass <secret>` or `AURA_REDIS_REQUIREPASS` / `CONFIG SET requirepass`. Unauthenticated clients get `-NOAUTH`. **Do not put secrets in git docs.** |
| **policy_agent AUTH** | Agent must AUTH when requirepass is set (same password via env the agent already supports). |
| **TLS** | Optional `--tls-port` + cert/key (see [`tls.md`](tls.md)). Keep cleartext `--port` for local agent unless agent TLS is wired. |
| **Clients** | Prefer TLS port for external clients; agent may stay on cleartext loopback. |

```bash
export AURA_REDIS_DENY_PLUGIN=1
./native/build/aura_redis_server \
  --port 6379 --bind 127.0.0.1 --requirepass "$AURA_REDIS_REQUIREPASS" \
  --evict lru --maxmemory 256mb
# Optional TLS (separate port):
#   --tls-port 6380 --tls-cert-file cert.pem --tls-key-file key.pem
```

---

## 2. DENY_PLUGIN + sandbox profile

```bash
source scripts/sandbox-policy-profile.sh          # default PROFILE=off (Soft)
# AURA_REDIS_SANDBOX_PROFILE=restricted source scripts/sandbox-policy-profile.sh
```

| Profile | Meaning |
|---------|---------|
| **off (current prod default)** | `AURA_SANDBOX=off` Soft ergonomics; grants succeed without Tenant Admin. Still **DENY_PLUGIN=1**. |
| **restricted (target)** | Aura Restricted sandbox + `grant-effect!` network/mutate. **PARTIAL** until Tenant Admin — see [`prod-profile.md`](prod-profile.md). |

Smoke: `./scripts/smoke-sandbox-profile.sh`.

---

## 3. Agent death fail-safe (P1.9 / A6)

If `policy_agent` disconnects or dies:

1. **Data plane keeps last applied `EVICT` / `LAYOUT` / PIN state** — no silent revert to a different kernel.
2. Agent reconnects with backoff; re-applies last policy; optional **policy-pin** resume (A6).
3. Heartbeat / audit logs under `.ar-policy-audit-*.log` / pin files (gitignored).

Verify: `python3 tests/test_prod_policy_ha.py`.

---

## 4. REPLICAOF + policy pin (A6)

- Single async replica: `REPLICAOF <host> <port>` on the replica; `REPLICAOF NO ONE` to promote.
- Replica is **read-only** for writes (`-READONLY`).
- String KV feed (typed keys: treat replica as best-effort; prefer SAVE warm-start for typed durability).
- After failover, start `policy_agent` against the new primary; durable **policy-pin** resumes last version when configured.

Test: `python3 tests/test_prod_replica.py`.

---

## 5. Upgrade / rollback

| Step | Action |
|------|--------|
| Pre-upgrade | `SAVE` if cache warm-start desired; note `aura_rdb` INFO=`2`; record tip SHA |
| Binary | Replace `aura_redis_server`; keep same `--dir`/`--dbfilename` |
| Aura pin | `AURA_REF` + `./scripts/fetch-aura.sh` — rebuild agent only if pin moves |
| Rollback | Prior binary + prior dump file; v2 RDB loads on current builds; v1 string dumps still load |
| Gate | `./scripts/ci-prod.sh` on the build artifact before promoting canary → staging |

Do **not** claim cross-version Redis RDB compatibility — format is **aura-rdb**, not Redis RDB.

---

## 6. What clients may use

See **[`client-allowlist.md`](client-allowlist.md)** (Tier 2 allowlist). Keyspace `SCAN`/`KEYS` allowed (prefer SCAN; KEYS O(N)). Reject SSCAN/ZSCAN / Streams / Cluster / Lua / ACL apps at intake.

---

## 7. Cache-only vs SAVE semantics

| Mode | Restart behavior |
|------|------------------|
| **Cache-only (default)** | No SAVE → empty keyspace on restart. Honest for pure cache. |
| **Warm-start** | `SAVE` / `BGSAVE` writes **aura-rdb v2** (string + HASH + LIST + ZSET + TTL). Load at startup from `--dir`/`--dbfilename`. |
| **Not durable like AOF** | Crash between SAVEs loses recent writes. No AOF/fsync-every-write. |

Details: [`persistence.md`](persistence.md).

---

## 8. Adaptive gate SSOT (hit quality)

**Citeable hit quality** = `python3 scripts/bench_regret.py phase_marathon` (Aura adaptive vs fixed kernels).

Short `bench_hit_vs_redis.py` **adaptive** rows are **non-citeable** until harness parity (agent needs longer phases). Use them only for fixed-kernel vs Redis tables.

Dual scoreboards (never collapse): [`redis-compare.md`](redis-compare.md) · [`perf-eval.md`](perf-eval.md).

---

## 9. Staging soak / CI

| Gate | Command |
|------|---------|
| PR / push wall | `.github/workflows/ci.yml` → smoke + `./scripts/ci-prod.sh` (short soak ~30s via `AURA_REDIS_SOAK_SEC`) |
| Nightly / manual | `.github/workflows/ci-bench.yml` → `./scripts/ci-bench.sh` + `./scripts/soak-prod.sh` |
| Local hour soak | `./scripts/soak-prod.sh` (default 3600s) |
| Local short soak | `AURA_REDIS_SOAK_SEC=120 ./scripts/soak-prod.sh` or `./scripts/prod-soak.sh 45` |


## 10. Quick health checklist

```bash
redis-cli -p 6379 PING
redis-cli -p 6379 INFO | egrep 'used_memory|evicted|keys_|aura_rdb|role|evict|layout'
# Agent: last audit / pin files in cwd (see policy_agent docs); DENY_PLUGIN still 1
```

## 10. CONFIG file + CLIENT LIST

| Concern | Guidance |
|---------|----------|
| **Enable persist** | `--config /var/lib/aura-redis/aura-redis.conf` or `AURA_REDIS_CONFIG=…`. **Unset/empty = runtime-only** (no auto file in cwd). |
| **Boot order** | defaults → config file → CLI/env (**CLI wins**). |
| **What survives** | Durable knobs rewritten on `CONFIG SET` / `CONFIG REWRITE`: maxmemory, requirepass, timeout, maxclients, evict-samples, protected-mode, tcp-backlog, slowlog-*, dir, dbfilename, bind, shadow-*, hot-*. |
| **Secrets** | `requirepass` is stored **cleartext** in the config file — mode `0600` + restricted dir. |
| **CLIENT LIST** | `CLIENT LIST` for id/addr/fd/name/age/idle/flags/db/cmd; `CLIENT ID` / `SETNAME` / `KILL ID` for ops. |

```bash
# Persist knobs across restart
./native/build/aura_redis_server --port 6379 --config /var/lib/aura-redis/aura-redis.conf ...
# redis-cli: CONFIG SET maxmemory 268435456   # auto-rewrites file
#            CONFIG REWRITE
#            CLIENT LIST
```

Verify: `python3 tests/test_prod_config_persist.py` · `python3 tests/test_prod_client_list.py`.


## 11. Staging one-shot (Docker Compose)

**Goal:** bring up a single-node canary with a data volume + healthcheck. Optional Aura `policy_agent` via compose profile. **Not** Redis Cluster / drop-in.

### Up / down

```bash
export AURA_REDIS_DENY_PLUGIN=1
# Optional secret (injected into /data/aura-redis.conf on first boot):
export AURA_REDIS_REQUIREPASS='your-staging-secret'

# Needs Docker Compose v2 (`docker compose`) or compatible `docker-compose`.
docker compose up -d --build          # C server only
docker compose ps
./scripts/healthcheck.sh -h 127.0.0.1 -p 6379 -a "$AURA_REDIS_REQUIREPASS"

# Optional control plane (needs .deps/aura built — same as CI/demos):
#   docker run --rm -v "$PWD:/work" -w /work ghcr.io/cybrid-systems/dev:v1.0.7 \
#     ./scripts/fetch-aura.sh && ./scripts/build-aura.sh
docker compose --profile agent up -d --build

docker compose down                   # keep volume
docker compose down -v                # wipe aura-redis-staging-data
```

| Piece | Path / note |
|-------|-------------|
| Compose | `docker-compose.yml` (service `aura-redis`, profile `agent` → `policy_agent`) |
| Image | `deploy/Dockerfile` → `aura-redis:staging` (C binary + healthcheck) |
| Config example | `deploy/staging/aura-redis.conf.example` (seeded to `/data/aura-redis.conf`) |
| Persist volume | named volume `aura-redis-staging-data` → `/data` (conf + `dump.rdb`) |
| Env | `AURA_REDIS_CONFIG=/data/aura-redis.conf`, `DENY_PLUGIN=1`, `AURA_REDIS_REQUIREPASS` |
| Health | compose `healthcheck` → `aura-redis-healthcheck` (= `scripts/healthcheck.sh`) |

### Config file example

See `deploy/staging/aura-redis.conf.example`: `bind 0.0.0.0`, `protected-mode yes`, `maxmemory 268435456`, `dir`/`dbfilename` under `/data`, commented `requirepass` placeholder. Live `CONFIG SET` / `CONFIG REWRITE` rewrite the volume file (cleartext secrets — restrict volume access).

### Persist volume

- RDB: `SAVE` / `BGSAVE` → `/data/dump.rdb` (aura-rdb, **not** Redis RDB).
- Config: durable knobs land in `/data/aura-redis.conf` when `AURA_REDIS_CONFIG` is set.
- `docker compose down` keeps the volume; `-v` deletes it.

### Fail-safe when agent dies

Same as §3: data plane keeps last `EVICT`/`LAYOUT`/PIN. Compose `policy_agent` uses `restart: unless-stopped` + Soft sandbox (`AURA_SANDBOX=off`) until Restricted/TA (A11 PARTIAL). Killing the agent container does **not** reset kernels on `aura-redis`.

### Promote replica (brief)

1. On replica: `REPLICAOF NO ONE` (becomes writable primary).
2. Point clients / `policy_agent` (`AURA_REDIS_HOST`/`PORT`) at the new primary; durable policy-pin resumes when configured.
3. Old primary: stop writes or `REPLICAOF <new> <port>` if re-attaching as replica.
4. Details: §4 · `tests/test_prod_replica.py`. Typed keys: prefer SAVE warm-start; live feed is best-effort async.

### CI without Compose

```bash
./scripts/smoke-staging.sh    # build native + healthcheck (wired in ci-prod)
# Full compose image build is for human staging — not required on every PR.
```
