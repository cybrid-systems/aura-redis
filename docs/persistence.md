# Persistence (v1)

**Status:** cache-only. No RDB, AOF, or replication in production v1.

| Event | Keyspace | CONFIG / EVICT |
|-------|----------|----------------|
| Clean shutdown (`SIGTERM`) | discarded | lost |
| Crash | discarded | lost |
| Restart | empty | CLI / env / defaults |

Operators must treat aura-redis as an **adaptive cache**: warm from origin after restart, or accept empty cold start.

Aura `policy_agent` (P1.9) reconnects and continues from the live empty/`INFO` state; it does not restore keys.

Optional durability is **P2.13** in [`production-plan.md`](production-plan.md).
