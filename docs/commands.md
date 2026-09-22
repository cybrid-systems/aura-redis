# aura-redis command contract (C data plane)

**Scope:** `native/src/ar_server.c` dispatch (`aura_redis_server` / FFI serve).  
**Not covered here:** pure-Lisp `AURA_REDIS_ENGINE=aura` (broader demo subset in README).  
**Production product:** string KV cache + Aura control commands — see [`production-plan.md`](production-plan.md).

Last audited: 2026-09-22 (CST) for P0.1.

---

## Supported (implemented)

| Command | Arity | Reply | Notes |
|---------|-------|-------|-------|
| `PING` | 1 or 2 | `+PONG` or bulk | Extra args → wrong-arity error |
| `QUIT` | any | `+OK` then close | |
| `GET` | 2 | bulk / null | |
| `SET` | ≥3 | `+OK` / `ERR OOM` | Optional `EX <sec>` only (no PX/NX/XX on C path) |
| `EXPIRE` | 3 | integer 0/1 | |
| `TTL` | 2 | integer | −2 missing, −1 no expire, else seconds |
| `DEL` | ≥2 | integer deleted | multi-key |
| `EXISTS` | ≥2 | integer count | multi-key |
| `MGET` | ≥2 | array of bulks | |
| `MSET` | odd ≥3 | `+OK` | key val pairs |
| `INCR` / `DECR` | 2 | integer | integer strings only |
| `FLUSHDB` | 1 | `+OK` | |
| `COMMAND` | 1 | `*0` | stub for clients that probe |
| `INFO` | 1+ | bulk | Multi-signal metrics (evict, layout, hits, used_memory, …) |
| `EVICT` | 1 / 2 / 3 | bulk name / `+OK` | `EVICT` \| `EVICT <noop\|lru\|lfu\|ttl_aware>` \| `EVICT samples <n>` |
| `LAYOUT` | 1 / 2 | bulk / `+OK` | `flat` \| `hot_cold` |
| `PIN` | 1 / 2 | list / `+OK` | `PIN` lists; `PIN key` pins |
| `UNPIN` | 2 | integer | |
| `POLICY` | 3 | `+OK` | `POLICY <prefix> <profile>` (M12 hints) |
| `PLUGIN` | 1 / 2 | bulk / `+OK` | **Denied** when `AURA_REDIS_DENY_PLUGIN=1` |

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
| Auth / admin | `AUTH`, `CONFIG`, `SHUTDOWN`, `CLIENT`, `SLOWLOG`, `MONITOR` |
| Persistence / repl | `SAVE`, `BGSAVE`, `BGREWRITEAOF`, `REPLICAOF`, `PSYNC` |
| Strings extras | `APPEND`, `STRLEN`, `GETSET`, `SETEX`, `PSETEX`, `SET` NX/XX/PX |
| Keys extras | `KEYS`, `DBSIZE`, `RENAME`, `TYPE`, `UNLINK` (≠ DEL alias) |
| Lists / hashes | `LPUSH`, `HSET`, … |
| Pub/Sub, transactions | `SUBSCRIBE`, `MULTI`/`EXEC` |
| Cluster / modules | all |

Clients needing these should not assume Redis parity; extend only under production P3 demand.

---

## Aura control-plane usage

`policy_agent` applies policy via **supported** RESP: `INFO` → `EVICT` / `LAYOUT` / `PIN` / `POLICY`.  
Production profile: `AURA_REDIS_DENY_PLUGIN=1`.
