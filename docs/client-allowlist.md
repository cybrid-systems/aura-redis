# Client allowlist (Tier 2 staging/canary)

**Purpose:** intake filter for apps targeting aura-redis. If an app needs a rejected surface, **do not onboard** — use Redis or wait for a later tier.

Full command table: [`commands.md`](commands.md). Ops context: [`runbook.md`](runbook.md).

---

## Allowed (Tier 2)

### Strings
`PING`, `AUTH`, `HELLO` (stub), `QUIT`, `GET`, `SET` (+ `NX`/`XX`/`EX`/`PX`), `SETNX`, `GETSET`, `MGET`, `MSET`, `DEL`, `EXISTS`, `INCR`, `DECR`, `EXPIRE`, `TTL`, `TYPE`, `FLUSHDB`, `SCAN`, `KEYS`

### HASH
`HSET`, `HGET`, `HMGET`, `HGETALL`, `HDEL`, `HEXISTS`, `HLEN`, `HINCRBY`

### LIST
`LPUSH`, `RPUSH`, `LPOP`, `RPOP`, `LLEN`, `LRANGE`, `LINDEX`

### ZSET
`ZADD`, `ZSCORE`, `ZREM`, `ZCARD`, `ZRANGE`, `ZRANGEBYSCORE` (+ `WITHSCORES`, `LIMIT`)

### Transactions (no WATCH)
`MULTI`, `EXEC`, `DISCARD` — **no `WATCH` / `UNWATCH`**

### Pub/Sub (local)
`SUBSCRIBE`, `UNSUBSCRIBE`, `PUBLISH` — **no `PSUBSCRIBE`**

### Ops / Aura control (operators & agent — not app hot path)
`INFO`, `CONFIG GET/SET`, `SAVE`, `BGSAVE`, `EVICT`, `LAYOUT`, `PIN`, `UNPIN`, `POLICY`, `SHADOW`, `HOTCOLD`, `REPLICAOF` / `SLAVEOF`

---

## Reject at intake

| Surface | Examples | Why |
|---------|----------|-----|
| **Field SCAN** | `HSCAN`, `SSCAN`, `ZSCAN` | Not implemented (keyspace `SCAN`/`KEYS` allowed) |
| **Streams** | `XADD`, `XREAD`, `XGROUP`, … | Not implemented |
| **Cluster** | `CLUSTER`, slot migration, redirects | Explicit non-goal |
| **Lua** | `EVAL`, `EVALSHA`, `SCRIPT` | Not implemented |
| **ACL** | `ACL SETUSER`, … | Use requirepass only |
| **Sets / Bitmaps / Geo / HyperLogLog** | `SADD`, `SETBIT`, `GEOADD`, … | Not in Tier 2 |
| **WATCH** | `WATCH` / `UNWATCH` | MULTI only without optimism |
| **Modules / PLUGIN** | Redis modules, `PLUGIN` .so | `AURA_REDIS_DENY_PLUGIN=1` |
| **AOF / PSYNC** | `BGREWRITEAOF`, full Redis PSYNC | aura-rdb + best-effort REPLICAOF only |

---

## Intake questions

1. Does the app only need the allowlisted string/hash/list/zset/MULTI subset?  
2. Can it tolerate cache-only restart **or** explicit SAVE warm-start (not Redis RDB)?  
3. Does it require Cluster, Lua, Streams, field-SCAN (`HSCAN`/…), or ACL? → **reject**. (`SCAN`/`KEYS` OK for Tier 2; prefer SCAN; KEYS is O(N).)  
4. Will `policy_agent` be the only writer of `EVICT`/`LAYOUT`/`PIN`? (Apps should not fight the agent.)

---

## SET option caveats (Tier 2)

- **Allowed:** `SET key value [NX|XX] [EX seconds|PX milliseconds]`, plus `SETNX` / `GETSET`.
- **NX/XX:** mutually exclusive; condition fail → null bulk (`$-1`), not an error.
- **EX/PX:** mutually exclusive; expire `≤0` → `ERR invalid expire time`.
- **Overwrite:** plain `SET` / `XX` replaces HASH/LIST/ZSET with a string (Redis 7); `NX`/`SETNX` leave typed keys untouched.
- **Not yet:** `GET` option on SET, `KEEPTTL`, `EXAT`/`PXAT`.

## SCAN / KEYS caveats (Tier 2)

- **Allowed:** keyspace `SCAN cursor [MATCH pattern] [COUNT count]` and `KEYS pattern`.
- **Types:** returns key names for string, HASH, LIST, and ZSET (not hash fields / zset members).
- **MATCH:** Redis-ish glob with `*` and `?` only (no `[abc]` character classes).
- **COUNT:** hint for buckets examined per call (default 10); not a hard return size.
- **Cursor:** opaque integer (bucket index across hot+cold tables). Concurrent SET/DEL/rehash/layout migrate may skip or duplicate keys across pages — same class of caveat as Redis SCAN. A full iteration until cursor `0` is best-effort complete for a quiescent store.
- **KEYS:** returns all matches in one reply (**O(N)**). Prefer SCAN for large keyspaces; OK for small Tier-2 caches.
- **Still reject:** `HSCAN` / `SSCAN` / `ZSCAN`.
