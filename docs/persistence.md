# Persistence (P2.13 aura-rdb)

**Default:** cache-only. Without an explicit `SAVE` / `BGSAVE`, restart discards the keyspace (same as P1.11).

**Optional warm-start:** custom **`aura-rdb`** snapshot for **string keys + TTL** (not Redis RDB byte-compatible).

**Typed keys (Tier 2):** `SAVE`/`BGSAVE` persist **string + HASH + LIST + ZSET** with TTL (**aura-rdb v2**). Legacy **v1** string-only dumps still load. Not Redis-RDB compatible.

## Knobs

| Knob | CLI | Env | CONFIG | Default |
|------|-----|-----|--------|---------|
| Directory | `--dir <path>` | `AURA_REDIS_DIR` | `CONFIG SET dir` | `.` |
| Filename | `--dbfilename <name>` | `AURA_REDIS_DBFILENAME` | `CONFIG SET dbfilename` | `dump.aura-rdb` |

Path = `{dir}/{dbfilename}`. Load runs once at process start (missing file = empty OK; corrupt file = startup failure).

## Commands

| Command | Behavior |
|---------|----------|
| `SAVE` | Synchronous write (temp + `rename`); `+OK` / `ERR save failed` |
| `BGSAVE` | `fork` child writes the same format (COW); falls back to sync `SAVE` if `fork` fails. Parent reaps in the serve loop. |

## Format (`aura-rdb` v2)

Little-endian custom format (not Redis RDB):

```
magic[8]     = "AURARDB" + NUL
version u32  = 2
count   u64  = number of live entries (all types)
repeated count times:
  type u8              # 0=string 1=hash 2=list 3=zset
  klen u32 | key[klen]
  expire_at u64        # 0 = none; absolute ms
  payload:
    string: vlen u32 | val[vlen]
    hash:   nfields u32 | repeated (flen|field|vlen|val)
    list:   nitems u32 | repeated (vlen|val)   # head→tail
    zset:   nmembers u32 | repeated (score f64 LE | mlen|member)
```

**v1 load:** still supported (no type byte; string payload only).

Pinned / LFU / layout tier state is **not** persisted (keys reload into flat hot tier).

## Restart semantics

| Event | Without SAVE | After SAVE |
|-------|--------------|------------|
| Clean `SIGTERM` | keys discarded | dump file retained on disk; **not** auto-written |
| `SIGKILL` / crash | keys discarded | last successful dump retained |
| Restart | empty unless dump loaded | `GET` / `TTL` restored from dump |

`CONFIG` durable knobs survive when a config file is enabled (`--config` / `AURA_REDIS_CONFIG`; see runbook §10). Last `EVICT` / `LAYOUT` still reset to CLI/env/defaults unless re-applied by `policy_agent`. Aura `policy_agent` reconnects against the warm keyspace + live `INFO`.

## INFO (Persistence)

Flat keys (policy_agent-safe): `loading`, `aof_enabled`, `rdb_bgsave_in_progress`, `rdb_last_save_time`, `rdb_last_bgsave_status`, `dir`, `dbfilename`, `aura_rdb:2`.

## Test

```bash
python3 tests/test_prod_rdb.py
```

Exit criteria: strings + TTL + HASH/LIST/ZSET → `SAVE` → kill → restart → typed GET/HGET/LRANGE/ZSCORE + TTL restored (`tests/test_prod_rdb.py`).

## Replication (P2.14)

Best-effort **single async replica** for string KV (not Redis Cluster / PSYNC):

- Replica: `REPLICAOF <host> <port>` → connects, sends `SYNC`, applies streamed `SET`/`DEL`/`EXPIRE`/…
- Replica clients: `GET`/`TTL`/… OK; writes → `-READONLY …`
- `REPLICAOF NO ONE` restores master role on that node
- Master `INFO`: `role:master`, `connected_slaves`
- Test: `python3 tests/test_prod_replica.py`

## TLS (P2.15)

See [`tls.md`](tls.md) — native `--tls-port` (OpenSSL optional build).
