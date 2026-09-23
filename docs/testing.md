# Testing aura-redis

## Aura vs Python — what goes where

| Suite | Language | Good for |
|-------|----------|----------|
| **Aura FFI** (`tests/test_ffi_iter1.aura`, `tests/test_types_ffi.aura`) | Aura + `std/ffi` | In-process unit/FFI: core create, string KV, typed ops (`ar_hset` / `ar_lpush` / `ar_zadd` / `ar_type`), WRONGTYPE flags |
| **Aura TCP** (`tests/test_prod_types.aura`) | Aura + `std/socket` + `src/redis/resp` | Product-surface RESP smoke on one connection: HASH/LIST/ZSET/TYPE/MULTI+EXEC |
| **Python prod** (`tests/test_prod_*.py`) | Python | Process lifecycle, TLS, replica, soak, full command coverage, pub/sub, multi-client edges |
| **Python strong-narrative** (`tests/test_policy_*.py`, `test_evict_slru.py`, `test_shadow_ab.py`, `test_typed_pressure.py`, `test_hot_cold_knobs.py`, `test_strong_edges.py`) | Python + Docker agent | Audit/canary/autofreeze/weight-evolve/shadow/typed-pressure/slru/hot_cold edges |

**Not all tests can be written in Aura.** Keep Python for process/TLS/replica/soak and dense edge matrices. Prefer Aura where FFI or a thin RESP client is enough. **Do not delete existing Python prod tests.**

## How to run

```bash
# Native build (shared by all)
./scripts/build-native.sh

# Aura FFI (typed thin API + iter1-style)
./scripts/run-aura-ffi-tests.sh
# also: ./scripts/test-ffi-iter1.sh

# Aura TCP product types (starts/kills aura_redis_server)
./scripts/run-aura-tcp-tests.sh

# Python edges + full prod gate (P0 + P3 + P1/P2 + strong-narrative)
python3 tests/test_prod_types_edge.py
./scripts/ci-prod.sh
# ci-prod includes ./scripts/ci-strong.sh

# Strong-narrative / P1–P2 only
./scripts/ci-strong.sh
AURA_REDIS_STRONG_SKIP_AGENT=1 ./scripts/ci-strong.sh   # C-only + edges, no Docker agent

# Long regret benches (NOT in ci-prod wall-time gate)
./scripts/ci-bench.sh
AURA_REDIS_CI_BENCH_FULL=1 ./scripts/ci-bench.sh

# Dual scoreboard vs Redis (throughput + hit quality) → docs/redis-compare.md
./scripts/bench-vs-redis.sh
BENCH_QUICK=1 ./scripts/bench-vs-redis.sh
python3 scripts/bench_hit_vs_redis.py
./scripts/memtier-cmp.sh
```

Test servers set `AURA_REDIS_DENY_PLUGIN=1`. Host Aura may need GLIBCXX from the
dev image; runners fall back to `ghcr.io/cybrid-systems/dev:v1.0.7` when needed.

### CI layout

| Script | Contents | In `ci-prod`? |
|--------|----------|---------------|
| `ci-prod.sh` | Aura FFI/TCP + P0 + P3 + calls `ci-strong.sh` | — (top gate) |
| `ci-strong.sh` | P1 config/clients/slowlog + P2 rdb/replica/tls/policy_ha + A3–A13 strong suites + `test_strong_edges.py` | yes |
| `ci-bench.sh` | Regret packs (`phase_marathon`, zipf, hot_protect, poison_heal; full via env) | **no** |
| `bench-vs-redis.sh` | Memtier ops/s + hit-quality vs `redis:7-alpine` dual scoreboard | **no** |



### GitHub Actions (Tier 2)

| Workflow | Trigger | What |
|----------|---------|------|
| `.github/workflows/ci.yml` | push/PR `main`, `workflow_dispatch` | Fetch+build Aura → `smoke-test.sh` → `ci-prod.sh` (short soak via `AURA_REDIS_SOAK_SEC`, default 30s) |
| `.github/workflows/ci-bench.yml` | nightly cron + `workflow_dispatch` | `ci-bench.sh` (long regret; optional FULL) + `soak-prod.sh` (default 3600s; override input) |

```bash
# Local mirrors
./scripts/ci-prod.sh
./scripts/ci-bench.sh
AURA_REDIS_SOAK_SEC=120 ./scripts/soak-prod.sh
```

Hour-scale soak covers eviction + TTL + typed keys + optional `policy_agent` + client reconnect; keep default `ci-prod` soak short.

## Sandbox profile (A11)

```bash
./scripts/smoke-sandbox-profile.sh   # off grants true; Restricted grants false (TA blocker)
source scripts/sandbox-policy-profile.sh   # default PROFILE=off
AURA_REDIS_SANDBOX_PROFILE=restricted source scripts/sandbox-policy-profile.sh
```

Live `policy_agent` demos still use `AURA_SANDBOX=off` until Tenant Admin unlocks Restricted grants.

## Edge coverage (`test_strong_edges.py`)

- EVICT `slru` / `tinylfu` name round-trip + WRONGTYPE under slru
- SHADOW CONFIG/INFO fields + sample-pct clamp 0..100
- HOTCOLD bounds (pct clamp 1..100) + WRONGTYPE under `LAYOUT hot_cold`
- POLICY prefix smoke
- Typed-pressure INFO keys parseable over TCP
- Canary **default-off** does not break fitness mutate path

Tier-2 harden edges: `tests/test_prod_tier2_edges.py` (wired in `ci-prod.sh`).

## Staging packaging smoke (T2.16)

```bash
./scripts/smoke-staging.sh          # native build + healthcheck (no compose)
./scripts/healthcheck.sh -p 6379    # against a running server
# Human staging: docker compose up -d --build  (see docs/runbook.md §11)
```

`ci-prod.sh` runs `smoke-staging.sh` after strong suites. Full image/compose builds stay out of the cheap PR wall.

