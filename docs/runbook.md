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

See **[`client-allowlist.md`](client-allowlist.md)** (Tier 2 allowlist). Reject SCAN / Streams / Cluster / Lua / ACL apps at intake.

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
