# Persistence (P2.13 aura-rdb)

**Default:** cache-only. Without an explicit `SAVE` / `BGSAVE`, restart discards the keyspace (same as P1.11).

**Optional warm-start:** custom **`aura-rdb`** snapshot for **string keys + TTL** (not Redis RDB byte-compatible).

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

## Format (`aura-rdb` v1)

Little-endian, documented custom format (prefer robust simplicity over Redis RDB subset):

```
magic[8]     = "AURARDB\0"
version u32  = 1
count   u64  = number of live string entries
repeated count times:
  klen u32 | key[klen] | vlen u32 | val[vlen] | expire_at u64
```

`expire_at` is absolute deadline ms (`0` = no TTL), matching the in-memory clock. Expired keys are skipped on save and on load.

Pinned / LFU / layout tier state is **not** persisted (keys reload into flat hot tier).

## Restart semantics

| Event | Without SAVE | After SAVE |
|-------|--------------|------------|
| Clean `SIGTERM` | keys discarded | dump file retained on disk; **not** auto-written |
| `SIGKILL` / crash | keys discarded | last successful dump retained |
| Restart | empty unless dump loaded | `GET` / `TTL` restored from dump |

`CONFIG` / last `EVICT` / `LAYOUT` still reset to CLI/env/defaults. Aura `policy_agent` reconnects against the warm keyspace + live `INFO`.

## INFO (Persistence)

Flat keys (policy_agent-safe): `loading`, `aof_enabled`, `rdb_bgsave_in_progress`, `rdb_last_save_time`, `rdb_last_bgsave_status`, `dir`, `dbfilename`, `aura_rdb:1`.

## Test

```bash
python3 tests/test_prod_rdb.py
```

Exit criteria: `SET` + `EXPIRE` → `SAVE` → kill → restart with same `--dir`/`--dbfilename` → `GET`/`TTL` restored.
