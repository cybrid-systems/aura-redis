/* P2.13 — optional aura-rdb snapshot (string keys + TTL warm-start).
 * P3.16: non-string types (HASH/LIST/ZSET) are skipped on SAVE — see docs/persistence.md. */
#include "ar_internal.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define AURA_RDB_MAGIC "AURARDB\0"
#define AURA_RDB_MAGIC_LEN 8
#define AURA_RDB_VERSION 1u

static int wr_u32(FILE* f, uint32_t v) {
  unsigned char b[4] = {(unsigned char)v, (unsigned char)(v >> 8),
                        (unsigned char)(v >> 16), (unsigned char)(v >> 24)};
  return fwrite(b, 1, 4, f) == 4;
}

static int wr_u64(FILE* f, uint64_t v) {
  unsigned char b[8];
  for (int i = 0; i < 8; ++i)
    b[i] = (unsigned char)(v >> (8 * i));
  return fwrite(b, 1, 8, f) == 8;
}

static int rd_u32(FILE* f, uint32_t* out) {
  unsigned char b[4];
  if (fread(b, 1, 4, f) != 4)
    return 0;
  *out = (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) |
         ((uint32_t)b[3] << 24);
  return 1;
}

static int rd_u64(FILE* f, uint64_t* out) {
  unsigned char b[8];
  if (fread(b, 1, 8, f) != 8)
    return 0;
  uint64_t v = 0;
  for (int i = 0; i < 8; ++i)
    v |= ((uint64_t)b[i]) << (8 * i);
  *out = v;
  return 1;
}

int ar_rdb_build_path(ArCore* core, char* out, size_t outsz) {
  if (!core || !out || outsz < 4)
    return 0;
  const char* dir =
      (core->rdb_dir[0]) ? core->rdb_dir : ".";
  const char* name =
      (core->rdb_filename[0]) ? core->rdb_filename : "dump.aura-rdb";
  int n = snprintf(out, outsz, "%s/%s", dir, name);
  return (n > 0 && (size_t)n < outsz) ? 1 : 0;
}

static uint64_t count_live_entries(ArCore* core, uint64_t now) {
  uint64_t n = 0;
  for (size_t i = 0; i < core->nbuckets; ++i) {
    for (ArEntry* e = core->buckets[i]; e; e = e->next) {
      if (e->type != AR_TYPE_STRING)
        continue; /* P3.16: RDB string-only until later */
      if (e->expire_at && now >= e->expire_at)
        continue;
      n++;
    }
  }
  if (core->cold_buckets) {
    for (size_t i = 0; i < core->cold_nbuckets; ++i) {
      for (ArEntry* e = core->cold_buckets[i]; e; e = e->next) {
        if (e->type != AR_TYPE_STRING)
          continue;
        if (e->expire_at && now >= e->expire_at)
          continue;
        n++;
      }
    }
  }
  return n;
}

static int write_entry(FILE* f, ArEntry* e) {
  if (e->klen > 0x7fffffffu || e->vlen > 0x7fffffffu)
    return 0;
  if (!wr_u32(f, (uint32_t)e->klen))
    return 0;
  if (e->klen && fwrite(e->key, 1, e->klen, f) != e->klen)
    return 0;
  if (!wr_u32(f, (uint32_t)e->vlen))
    return 0;
  if (e->vlen && fwrite(e->val, 1, e->vlen, f) != e->vlen)
    return 0;
  if (!wr_u64(f, e->expire_at))
    return 0;
  return 1;
}

static int write_table(FILE* f, ArEntry** table, size_t nb, uint64_t now) {
  if (!table)
    return 1;
  for (size_t i = 0; i < nb; ++i) {
    for (ArEntry* e = table[i]; e; e = e->next) {
      if (e->type != AR_TYPE_STRING)
        continue;
      if (e->expire_at && now >= e->expire_at)
        continue;
      if (!write_entry(f, e))
        return 0;
    }
  }
  return 1;
}

/* Write snapshot to path (caller supplies final path; uses .tmp + rename). */
static int ar_rdb_save_to_path(ArCore* core, const char* path) {
  char tmp[640];
  int tn = snprintf(tmp, sizeof(tmp), "%s.tmp.%d", path, (int)getpid());
  if (tn <= 0 || (size_t)tn >= sizeof(tmp))
    return 0;
  FILE* f = fopen(tmp, "wb");
  if (!f)
    return 0;
  uint64_t now = ar_now_ms();
  uint64_t nlive = count_live_entries(core, now);
  int ok = 1;
  if (fwrite(AURA_RDB_MAGIC, 1, AURA_RDB_MAGIC_LEN, f) != AURA_RDB_MAGIC_LEN)
    ok = 0;
  if (ok && !wr_u32(f, AURA_RDB_VERSION))
    ok = 0;
  if (ok && !wr_u64(f, nlive))
    ok = 0;
  if (ok && !write_table(f, core->buckets, core->nbuckets, now))
    ok = 0;
  if (ok && !write_table(f, core->cold_buckets, core->cold_nbuckets, now))
    ok = 0;
  if (ok && fflush(f) != 0)
    ok = 0;
  if (ok) {
    int fd = fileno(f);
    if (fd >= 0)
      (void)fsync(fd);
  }
  if (fclose(f) != 0)
    ok = 0;
  if (!ok) {
    unlink(tmp);
    return 0;
  }
  if (rename(tmp, path) != 0) {
    unlink(tmp);
    return 0;
  }
  return 1;
}

int ar_rdb_save(ArCore* core) {
  if (!core)
    return 0;
  char path[512];
  if (!ar_rdb_build_path(core, path, sizeof(path)))
    return 0;
  if (!ar_rdb_save_to_path(core, path))
    return 0;
  core->rdb_last_save_time = (uint64_t)time(NULL);
  core->rdb_last_bgsave_ok = 1;
  return 1;
}

void ar_rdb_poll_bgsave(ArCore* core) {
  if (!core || core->rdb_bgsave_pid <= 0)
    return;
  int status = 0;
  pid_t r = waitpid(core->rdb_bgsave_pid, &status, WNOHANG);
  if (r == 0)
    return; /* still running */
  if (r == core->rdb_bgsave_pid) {
    int ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
    core->rdb_last_bgsave_ok = ok ? 1 : 0;
    if (ok)
      core->rdb_last_save_time = (uint64_t)time(NULL);
    core->rdb_bgsave_pid = 0;
  } else if (r < 0 && errno != EINTR) {
    core->rdb_bgsave_pid = 0;
    core->rdb_last_bgsave_ok = 0;
  }
}

int ar_rdb_bgsave(ArCore* core) {
  if (!core)
    return 0;
  ar_rdb_poll_bgsave(core);
  if (core->rdb_bgsave_pid > 0)
    return -1; /* already in progress */
  char path[512];
  if (!ar_rdb_build_path(core, path, sizeof(path)))
    return 0;
  pid_t pid = fork();
  if (pid < 0) {
    /* fork failed — fall back to sync SAVE */
    return ar_rdb_save(core) ? 1 : 0;
  }
  if (pid == 0) {
    /* Child: snapshot via COW; do not touch network. */
    int ok = ar_rdb_save_to_path(core, path);
    _exit(ok ? 0 : 1);
  }
  core->rdb_bgsave_pid = (int)pid;
  return 1;
}

int ar_rdb_load(ArCore* core) {
  if (!core)
    return 0;
  char path[512];
  if (!ar_rdb_build_path(core, path, sizeof(path)))
    return 0;
  FILE* f = fopen(path, "rb");
  if (!f) {
    if (errno == ENOENT)
      return 1; /* missing = empty warm-start OK */
    fprintf(stderr, "ar_rdb: cannot open %s: %s\n", path, strerror(errno));
    return 0;
  }
  core->rdb_loading = 1;
  unsigned char magic[AURA_RDB_MAGIC_LEN];
  int ok = 1;
  if (fread(magic, 1, AURA_RDB_MAGIC_LEN, f) != AURA_RDB_MAGIC_LEN ||
      memcmp(magic, AURA_RDB_MAGIC, AURA_RDB_MAGIC_LEN) != 0) {
    fprintf(stderr, "ar_rdb: bad magic in %s\n", path);
    ok = 0;
  }
  uint32_t ver = 0;
  if (ok && !rd_u32(f, &ver))
    ok = 0;
  if (ok && ver != AURA_RDB_VERSION) {
    fprintf(stderr, "ar_rdb: unsupported version %u in %s\n", ver, path);
    ok = 0;
  }
  uint64_t nentries = 0;
  if (ok && !rd_u64(f, &nentries))
    ok = 0;
  if (ok && nentries > 10000000ull) {
    fprintf(stderr, "ar_rdb: entry count too large (%llu)\n",
            (unsigned long long)nentries);
    ok = 0;
  }
  uint64_t now = ar_now_ms();
  uint64_t loaded = 0;
  uint64_t skipped_expired = 0;
  for (uint64_t i = 0; ok && i < nentries; ++i) {
    uint32_t klen = 0, vlen = 0;
    uint64_t exp = 0;
    if (!rd_u32(f, &klen) || klen > (16u * 1024u * 1024u)) {
      ok = 0;
      break;
    }
    char* key = (char*)malloc(klen ? klen : 1);
    if (!key) {
      ok = 0;
      break;
    }
    if (klen && fread(key, 1, klen, f) != klen) {
      free(key);
      ok = 0;
      break;
    }
    if (!rd_u32(f, &vlen) || vlen > (16u * 1024u * 1024u)) {
      free(key);
      ok = 0;
      break;
    }
    char* val = (char*)malloc(vlen ? vlen : 1);
    if (!val) {
      free(key);
      ok = 0;
      break;
    }
    if (vlen && fread(val, 1, vlen, f) != vlen) {
      free(key);
      free(val);
      ok = 0;
      break;
    }
    if (!rd_u64(f, &exp)) {
      free(key);
      free(val);
      ok = 0;
      break;
    }
    if (exp && now >= exp) {
      skipped_expired++;
      free(key);
      free(val);
      continue;
    }
    if (!ar_entry_set_ex(core, key, (size_t)klen, val, (size_t)vlen, exp)) {
      free(key);
      free(val);
      ok = 0;
      break;
    }
    loaded++;
    free(key);
    free(val);
  }
  fclose(f);
  core->rdb_loading = 0;
  if (!ok) {
    fprintf(stderr, "ar_rdb: load failed for %s (partial keys may remain)\n",
            path);
    return 0;
  }
  fprintf(stderr,
          "ar_rdb: loaded %llu keys from %s (skipped_expired=%llu)\n",
          (unsigned long long)loaded, path,
          (unsigned long long)skipped_expired);
  return 1;
}

int ar_core_set_rdb_dir(ArCore* core, const char* dir) {
  if (!core || !dir || !dir[0])
    return 0;
  if (strlen(dir) >= sizeof(core->rdb_dir))
    return 0;
  snprintf(core->rdb_dir, sizeof(core->rdb_dir), "%s", dir);
  return 1;
}

const char* ar_core_rdb_dir(ArCore* core) {
  if (!core)
    return ".";
  return core->rdb_dir[0] ? core->rdb_dir : ".";
}

int ar_core_set_rdb_filename(ArCore* core, const char* name) {
  if (!core || !name || !name[0])
    return 0;
  if (strchr(name, '/') || strchr(name, '\\'))
    return 0; /* basename only */
  if (strlen(name) >= sizeof(core->rdb_filename))
    return 0;
  snprintf(core->rdb_filename, sizeof(core->rdb_filename), "%s", name);
  return 1;
}

const char* ar_core_rdb_filename(ArCore* core) {
  if (!core)
    return "dump.aura-rdb";
  return core->rdb_filename[0] ? core->rdb_filename : "dump.aura-rdb";
}

void ar_rdb_wait_bgsave(ArCore* core) {
  if (!core || core->rdb_bgsave_pid <= 0)
    return;
  int status = 0;
  while (waitpid(core->rdb_bgsave_pid, &status, 0) < 0 && errno == EINTR) {
  }
  int ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
  core->rdb_last_bgsave_ok = ok ? 1 : 0;
  if (ok)
    core->rdb_last_save_time = (uint64_t)time(NULL);
  core->rdb_bgsave_pid = 0;
}
