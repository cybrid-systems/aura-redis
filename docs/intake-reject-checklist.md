# Intake reject checklist (one page)

**Use before onboarding an app to aura-redis Tier 2.** If any **REJECT** fires → use Redis (or wait for a later tier). Full surface: [`client-allowlist.md`](client-allowlist.md).

| # | Question | Pass | Reject |
|---|----------|------|--------|
| 1 | Only allowlisted string / HASH / LIST / ZSET / MULTI+WATCH / local pubsub? | yes | Sets, bitmaps, geo, HLL, streams, modules |
| 2 | Needs field iteration? | `HSCAN` (allowed) / keyspace `SCAN` | `SSCAN` / `ZSCAN` |
| 3 | ZSET cardinality / range size? | ≤4096 members/key; ZRANGEBYSCORE pages ≤256 matches | Large leaderboards / unbounded ZADD |
| 4 | Cluster / slot redirects / Lua / ACL? | no | any of these |
| 5 | Durability need? | cache-only **or** scheduled `SAVE`/`BGSAVE` (aura-rdb) OK | Redis AOF / sync replication RPO |
| 6 | Replica expectations? | best-effort single async + manual re-`REPLICAOF` OK | PSYNC, sentinel, auto-failover |
| 7 | Auth model? | `requirepass` only | Redis ACL users/rules |
| 8 | PLUGIN / `.so` eviction? | `AURA_REDIS_DENY_PLUGIN=1` | needs MODULE/PLUGIN load |
| 9 | Who writes EVICT/LAYOUT/PIN? | `policy_agent` only | app fights the agent |
| 10 | Secrets / bind? | loopback or AUTH+protected-mode; config file mode locked if `requirepass` persisted | open bind + empty pass + `protected-mode no` |

**Quick reject phrases:** Streams · Cluster · Lua · ACL · SSCAN/ZSCAN · large ZSET · AOF · PSYNC · PLUGIN.

---

## Commercial fit pointer

Full “who is a fit vs early-no” narrative (phase-shifting cache / poison risk / governed autopilot vs Cluster·Lua·Streams·Restricted): [`commercial-fit.md`](commercial-fit.md).

| Extra | Pass | Reject |
|-------|------|--------|
| 11 | Soft sandbox + host isolation OK for this buyer | Requires Restricted / TA hard isolation (A11 Soft does **not** unlock Restricted) |
| 12 | Hit-quality / regret story is the buying criteria | Needs memtier adaptive win claims (we do not claim these) |

Cite data: [`diff-vs-redis.md`](diff-vs-redis.md) E1–E4.
