# aura-redis command contract (C data plane)

**Scope:** `native/src/ar_server.c` dispatch (`aura_redis_server` / FFI serve).  
**Not covered here:** pure-Lisp `AURA_REDIS_ENGINE=aura` (broader demo subset in README).  
**Production product:** string KV cache + Aura control commands — see [`production-plan.md`](production-plan.md).

Last audited: 2026-09-23 (CST) for P3.16–P3.18 + SET opts + APPEND/RENAME/UNLINK (+ RENAMENX).

---

## Supported (implemented)

| Command | Arity | Reply | Notes |
|---------|-------|-------|-------|
| `PING` | 1 or 2 | `+PONG` or bulk | Extra args → wrong-arity error; allowed pre-AUTH |
| `AUTH` | 2 or 3 | `+OK` / WRONGPASS | `AUTH <pass>` or `AUTH <user> <pass>` (user ignored); need `--requirepass` / `AURA_REDIS_REQUIREPASS` |
| `HELLO` | 1+ | array map | Minimal stub; optional `AUTH` inline; allowed pre-AUTH |
| `CONFIG` | GET 3 / SET 4 | array / `+OK` | P1.1+P1.12+P2.13: `maxmemory`, `requirepass`, `protected-mode`, `evict-samples`, `bind`, `maxclients`, `timeout`, `tcp-backlog`, `slowlog-log-slower-than`, `dir`, `dbfilename`, `shadow-policy`, `shadow-sample-pct`, `hot-soft-cap-pct`, `hot-soft-cap-min`, `hot-promote-on-get` |
| `SAVE` | 1 | `+OK` | P2.13 sync aura-rdb **v2** (string+HASH+LIST+ZSET+TTL) |
| `BGSAVE` | 1 | `+OK` | P2.13 fork child aura-rdb v2 (or sync fallback) |
| `REPLICAOF` / `SLAVEOF` | 3 | `+OK` | P2.14: `host port` or `NO ONE`; replica read-only |
| `SYNC` | 1 | (stream) | P2.14 internal: full sync + feed; not for apps |
| `QUIT` | any | `+OK` then close | Allowed pre-AUTH |
| `GET` | 2 | bulk / null | |
| `SET` | ≥3 | `+OK` / null bulk / `ERR OOM` | Options: `NX`\|`XX` (mutex), `EX <sec>`\|`PX <ms>` (mutex); NX/XX fail → null bulk; overwrites hash/list/zset → string (Redis) |
| `SETNX` | 3 | integer 0/1 | Alias: set if absent |
| `GETSET` | 3 | bulk / null / WRONGTYPE | Atomically return old string then SET (clears TTL) |
| `EXPIRE` | 3 | integer 0/1 | |
| `TTL` | 2 | integer | −2 missing, −1 no expire, else seconds |
| `DEL` | ≥2 | integer deleted | multi-key |
| `UNLINK` | ≥2 | integer deleted | Tier-2: DEL-equivalent (single-threaded; no async reclaim) |
| `APPEND` | 3 | integer new length | create if missing; WRONGTYPE on non-string; keeps TTL |
| `RENAME` | 3 | `+OK` / ERR | overwrite dest; any type; ERR no such key |
| `RENAMENX` | 3 | integer 0/1 | rename only if dest absent |
| `EXISTS` | ≥2 | integer count | multi-key |
| `MGET` | ≥2 | array of bulks | |
| `MSET` | odd ≥3 | `+OK` | key val pairs |
| `INCR` / `DECR` | 2 | integer | integer strings only |
| `FLUSHDB` | 1 | `+OK` | |
| `COMMAND` | 1 | `*0` | stub for clients that probe |
| `INFO` | 1+ | bulk | Sectioned; flat keys for policy_agent; A7 `keys_{string,hash,list,zset}`/`mem_*`/`bigkey_*`; A9 `hot_soft_cap_*` |
| `EVICT` | 1 / 2 / 3 | bulk name / `+OK` | `EVICT` \| `EVICT <noop\|lru\|lfu\|ttl_aware\|slru\|tinylfu>` \| `EVICT samples <n>` |
| `LAYOUT` | 1 / 2 | bulk / `+OK` | `flat` \| `hot_cold` |
| `PIN` | 1 / 2 | list / `+OK` | `PIN` lists; `PIN key` pins |
| `UNPIN` | 2 | integer | |
| `POLICY` | 3 | `+OK` | `POLICY <prefix> <profile>` (M12 hints) |
| `SHADOW` | 1–3 | bulk / `+OK` | A10: `SHADOW` stats; `SHADOW policy <name>`; `SHADOW sample-pct <n>`; `SHADOW reset\|diverge` |
| `HOTCOLD` | 1 / 3 | bulk / `+OK` | A9: status; `HOTCOLD soft-cap-pct\|soft-cap-min\|promote-on-get <v>` |
| `PLUGIN` | 1 / 2 | bulk / `+OK` | **Denied** when `AURA_REDIS_DENY_PLUGIN=1` |
| `TYPE` | 2 | bulk | `string`/`hash`/`list`/`zset`/`none` (P3.16) |
| `HSET` | ≥4 even | integer | new fields count; multi field/value (P3.16a) |
| `HGET` | 3 | bulk/null | |
| `HMGET` | ≥3 | array | |
| `HGETALL` | 2 | flat array | field value pairs |
| `HDEL` | ≥3 | integer | |
| `HEXISTS` | 3 | 0/1 | |
| `HLEN` | 2 | integer | |
| `HINCRBY` | 4 | integer | integer field values |
| `LPUSH` / `RPUSH` | ≥3 | integer length | P3.16b |
| `LPOP` / `RPOP` | 2 | bulk/null | |
| `LLEN` | 2 | integer | |
| `LRANGE` | 4 | array | Redis index semantics (neg OK) |
| `LINDEX` | 3 | bulk/null | |
| `ZADD` | ≥4 even | integer added | sorted array O(n); P3.16c |
| `ZSCORE` | 3 | bulk/null | score string |
| `ZREM` | ≥3 | integer | |
| `ZCARD` | 2 | integer | |
| `ZRANGE` | 4–5 | array | optional WITHSCORES |
| `ZRANGEBYSCORE` | ≥4 | array | min/max, -inf/+inf; WITHSCORES; LIMIT |
| `MULTI` | 1 | +OK | P3.17a; subsequent cmds → +QUEUED |
| `EXEC` | 1 | array | replies; error if no MULTI |
| `DISCARD` | 1 | +OK | clear queue |
| `SUBSCRIBE` | ≥2 | array confirms | enters pubsub mode (P3.17b) |
| `UNSUBSCRIBE` | ≥1 | array confirms | no args = all |
| `PUBLISH` | 3 | integer receivers | local subscribers only |
| `SCAN` | ≥2 | `[cursor, [keys…]]` | Tier-2: opaque cursor; optional `MATCH` (`*`/`?`) + `COUNT` hint; string+HASH+LIST+ZSET keys; expires purged on visit |
| `KEYS` | 2 | array | Full keyspace scan via same glob as SCAN; **O(N)** — fine for small Tier-2 caches; prefer SCAN for pagination |



Wrong arity → `-ERR wrong number of arguments for '<cmd>'`.  
Unknown → `-ERR unknown command`.  
Protocol parse failure → `-ERR Protocol error` and connection close.

### RESP parser limits (P0.1)

- Commands must be RESP arrays of bulk strings (`*…` + `$…`); inline / non-`*` rejected.
- Null bulk argv (`$-1`) → protocol error (not a crash).
- Array length `> 64` (`AR_MAX_ARGV`) → protocol error.
- Single bulk length `> 16 MiB` → protocol error; read buffer also capped at 16 MiB.

---

## Unsupported on C data plane (explicit)

These may exist on the Lisp engine or Redis; **not** in `ar_server.c` today:

| Area | Examples |
|------|----------|
| Auth / admin | `SHUTDOWN`, `CLIENT`, `SLOWLOG`, `MONITOR` (AUTH/HELLO done in P0.4) |
| Persistence / repl | `BGREWRITEAOF`, `PSYNC` (SAVE/BGSAVE/REPLICAOF/SYNC done P2.13–14) |
| Strings extras | `STRLEN`, `SETEX`, `PSETEX`, `SET` GET/KEEPTTL/EXAT/PXAT |
| Keys extras | `DBSIZE`; `APPEND`/`RENAME`/`RENAMENX`/`UNLINK` done T2.10; `KEYS`/`SCAN` done P3.18 |
| (types) | HASH/LIST/ZSET done P3.16 |
| Patterns / WATCH | `PSUBSCRIBE`, `WATCH`/`UNWATCH` (deferred) |
| Cluster / modules | all |

Clients needing these should not assume Redis parity; extend only under production P3 demand.

---

## Aura control-plane usage

`policy_agent` applies policy via **supported** RESP: `INFO` → `EVICT` / `LAYOUT` / `PIN` / `POLICY`.  
Production profile: `AURA_REDIS_DENY_PLUGIN=1`.


---


## Thin public FFI (Aura `std/ffi`)

In-process typed helpers on `libaura_redis_core.so` (C-string keys; see `ar_core.h`):
`ar_type`, `ar_hset`/`ar_hget`/`ar_hdel`/`ar_hexists`/`ar_hlen`/`ar_hincrby`,
`ar_lpush`/`ar_rpush`/`ar_lpop`/`ar_rpop`/`ar_llen`/`ar_lindex`,
`ar_zadd`/`ar_zscore`/`ar_zrem`/`ar_zcard`. WRONGTYPE → return `-1`/`NULL` and `*wrongtype=1`.
Exercised by `tests/test_types_ffi.aura` — not a substitute for RESP product tests.

## Security (P0.4)

| Knob | Flag | Env | Default |
|------|------|-----|---------|
| Password | `--requirepass <pass>` | `AURA_REDIS_REQUIREPASS` | unset (no AUTH) |
| Bind | `--bind <ip>` | `AURA_REDIS_BIND` | `127.0.0.1` |
| Protected mode | `--protected-mode yes|no` | `AURA_REDIS_PROTECTED_MODE` | `yes` |

- When `requirepass` is set, unauthenticated clients may only run `AUTH` / `PING` / `QUIT` / `HELLO`; others → `-NOAUTH Authentication required.`
- **Protected-mode** (Redis spirit): if enabled **and** no password, non-loopback peers are refused with `-DENIED …` even when `--bind 0.0.0.0`. Loopback always allowed. Password **or** `--protected-mode no` permits remote.
- Default bind remains loopback — safest deploy default; use `--bind 0.0.0.0` + `requirepass` for remote + Aura policy_agent on another host.
- `CONFIG SET/GET` knobs at runtime (not persisted across restart except via CLI/env): maxmemory, requirepass, protected-mode, evict-samples, maxclients, timeout, tcp-backlog, dir, dbfilename.
