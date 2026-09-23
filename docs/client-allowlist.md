# Client allowlist (Tier 2 staging/canary)

**Purpose:** intake filter for apps targeting aura-redis. If an app needs a rejected surface, **do not onboard** — use Redis or wait for a later tier.

Full command table: [`commands.md`](commands.md). Ops context: [`runbook.md`](runbook.md). One-page reject gate: [`intake-reject-checklist.md`](intake-reject-checklist.md).

---

## Allowed (Tier 2)

### Strings
`PING`, `AUTH`, `HELLO` (stub), `QUIT`, `GET`, `SET` (+ `NX`/`XX`/`EX`/`PX`/`EXAT`/`PXAT`/`KEEPTTL`), `SETNX`, `SETEX`, `PSETEX`, `GETSET`, `APPEND`, `STRLEN`, `MGET`, `MSET`, `DEL`, `UNLINK`, `RENAME`, `RENAMENX`, `EXISTS`, `DBSIZE`, `INCR`, `DECR`, `INCRBY`, `DECRBY`, `EXPIRE`, `PEXPIRE`, `EXPIREAT`, `PEXPIREAT`, `TTL`, `PTTL`, `TYPE`, `FLUSHDB`, `SCAN`, `KEYS`

### HASH
`HSET`, `HGET`, `HMGET`, `HGETALL`, `HDEL`, `HEXISTS`, `HLEN`, `HINCRBY`, `HSCAN`

### LIST
`LPUSH`, `RPUSH`, `LPOP`, `RPOP`, `LLEN`, `LRANGE`, `LINDEX`, `LREM`, `LTRIM`

### ZSET
`ZADD`, `ZSCORE`, `ZREM`, `ZCARD`, `ZRANGE`, `ZRANGEBYSCORE` (+ `WITHSCORES`, `LIMIT`)

### Transactions (+ WATCH)
`MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH`

### Pub/Sub (local)
`SUBSCRIBE`, `UNSUBSCRIBE`, `PUBLISH` — **no `PSUBSCRIBE`**

### Ops / Aura control (operators & agent — not app hot path)
`INFO`, `CONFIG GET/SET/REWRITE`, `CLIENT LIST/ID/SETNAME/KILL`, `SAVE`, `BGSAVE`, `SLOWLOG GET/RESET/LEN`, `SHUTDOWN`, `EVICT`, `LAYOUT`, `PIN`, `UNPIN`, `POLICY`, `SHADOW`, `HOTCOLD`, `REPLICAOF` / `SLAVEOF`

---

## Reject at intake

| Surface | Examples | Why |
|---------|----------|-----|
| **Field SCAN** | `SSCAN`, `ZSCAN` | Not implemented (`HSCAN` allowed; keyspace `SCAN`/`KEYS` allowed) |
| **Streams** | `XADD`, `XREAD`, `XGROUP`, … | Not implemented |
| **Cluster** | `CLUSTER`, slot migration, redirects | Explicit non-goal |
| **Lua** | `EVAL`, `EVALSHA`, `SCRIPT` | Not implemented |
| **ACL** | `ACL SETUSER`, … | Use requirepass only |
| **Sets / Bitmaps / Geo / HyperLogLog** | `SADD`, `SETBIT`, `GEOADD`, … | Not in Tier 2 |
| **Modules / PLUGIN** | Redis modules, `PLUGIN` .so | `AURA_REDIS_DENY_PLUGIN=1` |
| **AOF / PSYNC** | `BGREWRITEAOF`, full Redis PSYNC | aura-rdb + best-effort REPLICAOF only |

---

## Intake questions

1. Does the app only need the allowlisted string/hash/list/zset/MULTI subset?  
2. Can it tolerate cache-only restart **or** explicit SAVE warm-start (not Redis RDB)?  
3. Does it require Cluster, Lua, Streams, `SSCAN`/`ZSCAN`, or ACL? → **reject**. (`SCAN`/`KEYS`/`HSCAN` OK for Tier 2; prefer SCAN; KEYS is O(N).)  
4. Will `policy_agent` be the only writer of `EVICT`/`LAYOUT`/`PIN`? (Apps should not fight the agent.)

---

## SET option caveats (Tier 2)

- **Allowed:** `SET key value [NX|XX] [EX seconds|PX milliseconds|EXAT unix-sec|PXAT unix-ms|KEEPTTL]`, plus `SETNX` / `SETEX` / `PSETEX` / `GETSET`.
- **NX/XX:** mutually exclusive; condition fail → null bulk (`$-1`), not an error.
- **EX/PX/EXAT/PXAT/KEEPTTL:** mutually exclusive expire modes; expire `≤0` → `ERR invalid expire time`.
- **KEEPTTL:** overwrite value but preserve existing TTL (no-op if key had none).
- **Overwrite:** plain `SET` / `XX` replaces HASH/LIST/ZSET with a string (Redis 7); `NX`/`SETNX` leave typed keys untouched.
- **Not yet:** `GET` option on SET.
- **MGET:** missing **or** typed (HASH/LIST/ZSET) slots return null bulk (Redis parity); `GET` still WRONGTYPE on typed.
- **PTTL/PEXPIRE/EXPIREAT/PEXPIREAT:** millisecond / absolute-unix variants of TTL/EXPIRE.
- **INCRBY/DECRBY:** integer delta; preserve TTL (same as INCR/DECR).

## SCAN / KEYS caveats (Tier 2)

- **Allowed:** keyspace `SCAN cursor [MATCH pattern] [COUNT count]` and `KEYS pattern`.
- **Types:** returns key names for string, HASH, LIST, and ZSET (not hash fields / zset members).
- **MATCH:** Redis-ish glob with `*` and `?` only (no `[abc]` character classes).
- **COUNT:** hint for buckets examined per call (default 10); not a hard return size.
- **Cursor:** opaque integer (bucket index across hot+cold tables). Concurrent SET/DEL/rehash/layout migrate may skip or duplicate keys across pages — same class of caveat as Redis SCAN. A full iteration until cursor `0` is best-effort complete for a quiescent store.
- **KEYS:** returns all matches in one reply (**O(N)**). Prefer SCAN for large keyspaces; OK for small Tier-2 caches.
- **HSCAN:** `HSCAN key cursor [MATCH pattern] [COUNT count]` — cursor over hash **fields**; reply `[cursor, [field, value, …]]`. Same MATCH (`*`/`?`) + COUNT hint as SCAN. Missing key → `["0", []]`. WRONGTYPE on non-hash.
- **Still reject:** `SSCAN` / `ZSCAN`.

## APPEND / RENAME / UNLINK caveats (Tier 2)

- **APPEND:** creates the key if missing; returns new string length; **WRONGTYPE** on HASH/LIST/ZSET; does **not** clear TTL.
- **RENAME:** overwrites `newkey` if present; moves any type (string/HASH/LIST/ZSET); preserves TTL/pin; `ERR no such key` if source missing; same-key is `+OK`.
- **RENAMENX:** integer `1` if renamed, `0` if destination exists; still errors if source missing.
- **UNLINK:** same semantics as multi-key `DEL` (returns deleted count). No background reclaim on this single-threaded server.

## STRLEN / SETEX / PSETEX / DBSIZE caveats (Tier 2)

- **STRLEN:** returns byte length of a string value; **0** if key missing; **WRONGTYPE** on HASH/LIST/ZSET.
- **SETEX:** `SETEX key seconds value` (Redis argument order). Same write path as `SET … EX`; seconds ≤0 → `ERR invalid expire time`.
- **PSETEX:** `PSETEX key milliseconds value` → same as `SET … PX`.
- **DBSIZE:** integer count of non-expired keys (string + HASH/LIST/ZSET). **O(N)** full walk; expired keys are purged as encountered (may differ briefly from INFO `keys=` until walk/active-expire).

## WATCH / UNWATCH caveats (Tier 2)

- **WATCH key [key…]:** marks keys for optimistic locking; duplicate keys ignored; max 64 keys/connection.
- **Dirty:** any successful mutation of a watched key (SET/DEL/typed writes/EXPIRE/evict/expire/FLUSHDB/RENAME src|dst) sets dirty — including writes by the same connection outside MULTI.
- **EXEC:** if dirty → RESP2 null array (`*-1`); queued commands are **not** executed; watches cleared. Else run queue and clear watches.
- **DISCARD / UNWATCH / disconnect:** clear watches.
- **WATCH inside MULTI:** `-ERR WATCH inside MULTI is not allowed`.

## Typed REPLICAOF caveats (Tier 2)

- Full sync emits `SET`/`HSET`/`RPUSH`/`ZADD` (+ `EXPIRE` when TTL) for existing keys.
- Live feed propagates string writes **and** HSET/HDEL/HINCRBY/LPUSH/RPUSH/LPOP/RPOP/ZADD/ZREM.
- Best-effort async **single** replica (not Redis PSYNC/Cluster/sentinel).
- **Link loss:** fail-closed — replica stays read-only with last applied state; **no auto-reconnect**. Operator must re-issue `REPLICAOF host port` (or restart with the same). Test: `tests/test_prod_replica_failover.py`.
- **Promote:** `REPLICAOF NO ONE` on the survivor; point clients + `policy_agent` at the new primary. Old primary should stop writes or re-attach as replica.

## CONFIG persist / CLIENT LIST caveats (Tier 2)

- **CONFIG file:** enable with `--config <path>` or `AURA_REDIS_CONFIG=<path>`. Empty disables. Boot order: defaults → file → CLI/env (CLI wins).
- **Auto-save:** durable `CONFIG SET` (maxmemory, requirepass, timeout, maxclients, evict-samples, protected-mode, tcp-backlog, slowlog-*, dir, dbfilename, shadow-*, hot-*) rewrites the file; `CONFIG REWRITE` forces a rewrite.
- **Not Redis redis.conf:** simple `key value` lines only; not a full Redis `CONFIG REWRITE` of an imported redis.conf.
- **CLIENT LIST:** Redis-ish single bulk of `id=… addr=… fd=… name=… age=… idle=… flags=… db=0 cmd=…` lines. Flags: `N` normal, `S` replica feed, `M` master link, `P` pubsub, `x` MULTI.
- **CLIENT KILL / SETNAME / ID:** supported; no `CLIENT PAUSE` / tracking / caching.


## ZSET scale caveats (Tier 2)

- **Implementation:** sorted array (score asc, member lex) — **O(n)** insert/delete, not Redis skiplist.
- **Hard size bar:** `AR_ZSET_MAX_MEMBERS` = **4096** members per key. `ZADD` that would grow past the bar → `ERR zset max members exceeded` + stderr WARN. Updates (same member, new score) still allowed at the bar.
- **ZRANGEBYSCORE match-cap:** at most **256** matching members returned per call (`AR_ZSET_RANGE_MATCH_CAP`). Larger ranges need Redis or a later skiplist tier.
- **Intake:** apps that need large leaderboards / unbounded ZADD → **reject** for Tier 2.
