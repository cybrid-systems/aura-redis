# Client allowlist (Tier 2 staging/canary)

**Purpose:** intake filter for apps targeting aura-redis. If an app needs a rejected surface, **do not onboard** — use Redis or wait for a later tier.

Full command table: [`commands.md`](commands.md). Ops context: [`runbook.md`](runbook.md).

---

## Allowed (Tier 2)

### Strings
`PING`, `AUTH`, `HELLO` (stub), `QUIT`, `GET`, `SET` (+ `EX`), `MGET`, `MSET`, `DEL`, `EXISTS`, `INCR`, `DECR`, `EXPIRE`, `TTL`, `TYPE`, `FLUSHDB`

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
| **SCAN family** | `SCAN`, `HSCAN`, `SSCAN`, `ZSCAN` | Not implemented |
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
3. Does it require Cluster, Lua, Streams, SCAN, or ACL? → **reject**.  
4. Will `policy_agent` be the only writer of `EVICT`/`LAYOUT`/`PIN`? (Apps should not fight the agent.)
