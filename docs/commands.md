# aura-redis command contract (C data plane)

**Scope:** `native/src/ar_server.c` dispatch (`aura_redis_server` / FFI serve).  
**Not covered here:** pure-Lisp `AURA_REDIS_ENGINE=aura` (broader demo subset in README).  
**Production product:** string KV cache + Aura control commands — see [`production-plan.md`](production-plan.md).

Last audited: 2026-09-22 (CST) for P0.4 AUTH / protected-mode.

---

## Supported (implemented)

| Command | Arity | Reply | Notes |
|---------|-------|-------|-------|
| `PING` | 1 or 2 | `+PONG` or bulk | Extra args → wrong-arity error; allowed pre-AUTH |
| `AUTH` | 2 or 3 | `+OK` / WRONGPASS | `AUTH <pass>` or `AUTH <user> <pass>` (user ignored); need `--requirepass` / `AURA_REDIS_REQUIREPASS` |
| `HELLO` | 1+ | array map | Minimal stub; optional `AUTH` inline; allowed pre-AUTH |
| `CONFIG` | GET 3 / SET 4 | array / `+OK` | P1.1+P1.12+P2.13: `maxmemory`, `requirepass`, `protected-mode`, `evict-samples`, `bind`, `maxclients`, `timeout`, `tcp-backlog`, `slowlog-log-slower-than`, `dir`, `dbfilename` |
| `SAVE` | 1 | `+OK` | P2.13 sync `aura-rdb` snapshot |
| `BGSAVE` | 1 | `+OK` | P2.13 fork child (or sync fallback) |
| `REPLICAOF` / `SLAVEOF` | 3 | `+OK` | P2.14: `host port` or `NO ONE`; replica read-only |
| `SYNC` | 1 | (stream) | P2.14 internal: full sync + feed; not for apps |
| `QUIT` | any | `+OK` then close | Allowed pre-AUTH |
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
| `INFO` | 1+ | bulk | Sectioned (Server/Clients/Memory/Stats/Keyspace/Persistence/Aura); flat keys kept for policy_agent |
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
| Auth / admin | `SHUTDOWN`, `CLIENT`, `SLOWLOG`, `MONITOR` (AUTH/HELLO done in P0.4) |
| Persistence / repl | `BGREWRITEAOF`, `PSYNC` (SAVE/BGSAVE/REPLICAOF/SYNC done P2.13–14) |
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


---

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
