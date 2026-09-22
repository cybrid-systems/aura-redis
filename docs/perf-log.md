# aura-redis perf log

Recorded: 2026-09-22 19:43:58 Asia/Shanghai

Frozen memtier matrix: **1c×1t**, SET:GET=**1:10**, value **32B**, key 1..10000 **R:R**, `-n 30000`.

Data plane under test: `libaura_redis_core.so` via host `aura_redis_server` (same epoll/RESP/dict as Aura `server_ffi.aura` / `c-load`). Aura FFI entry smoke-verified in `ghcr.io/cybrid-systems/dev:v1.0.7` (host lacks `GLIBCXX_3.4.35`).

| Engine | pipeline | Totals ops/s | vs Redis |
|--------|----------|--------------|----------|
| redis:7-alpine | 1 | 42452.99 | 1.00 |
| aura-redis C data plane | 1 | 47753.14 | **1.125** |
| redis:7-alpine | 16 | 381674.53 | 1.00 |
| aura-redis C data plane | 16 | 495098.52 | **1.297** |

**Gates:** p=1 ratio **1.125** (≥0.20 iter2, ≥0.50 iter3, ≥0.80 iter4) — all cleared.

Lisp engine baseline (prior): p=1 ~143 ops/s — C path ≈ **334×** Lisp.

Miss rates matched Redis on the same random key pattern (~87% cold misses at ratio 1:10 before working set fills) — not error-inflated.

Reproduce: `./scripts/memtier-cmp.sh`

### Iteration 4 levers (headroom / if regresses)

Already applied in v1 C path: epoll ET, TCP_NODELAY, pipelined parse-many-per-read, buffered writes, binary-safe dict with rehash, static `+OK`/`+PONG`/`$-1`, zero-copy GET reply from entry pointer.

Next if needed: `writev` multi-reply, arena/slab values, dict load-factor tuning, io_uring, multi-thread shard.

### Iteration 8 — layout (2026-09-22 Asia/Shanghai)

Added `flat` / `hot_cold` layouts with quiescent `ar_core_set_layout` migrate. Not a memtier gate change; correctness demo: `tests/test_layout.py` (migrate under concurrent SET/GET; promote/demote counters). Soft hot cap ≈ nkeys/4.
