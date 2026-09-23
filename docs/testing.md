# Testing aura-redis

## Aura vs Python — what goes where

| Suite | Language | Good for |
|-------|----------|----------|
| **Aura FFI** (`tests/test_ffi_iter1.aura`, `tests/test_types_ffi.aura`) | Aura + `std/ffi` | In-process unit/FFI: core create, string KV, typed ops (`ar_hset` / `ar_lpush` / `ar_zadd` / `ar_type`), WRONGTYPE flags |
| **Aura TCP** (`tests/test_prod_types.aura`) | Aura + `std/socket` + `src/redis/resp` | Product-surface RESP smoke on one connection: HASH/LIST/ZSET/TYPE/MULTI+EXEC |
| **Python prod** (`tests/test_prod_*.py`) | Python | Process lifecycle, TLS, replica, soak, full command coverage, pub/sub, multi-client edges |

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

# Python edges + full prod gate
python3 tests/test_prod_types_edge.py
./scripts/ci-prod.sh   # includes Aura FFI, Aura TCP, edge + existing prod_*.py
```

Test servers set `AURA_REDIS_DENY_PLUGIN=1`. Host Aura may need GLIBCXX from the
dev image; runners fall back to `ghcr.io/cybrid-systems/dev:v1.0.7` when needed.
