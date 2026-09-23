#!/usr/bin/env bash
# Comprehensive aura vs Redis dual scoreboard:
#   Scoreboard 1 — Throughput (ops/s) via memtier
#   Scoreboard 2 — Hit quality (useful-GET hit%) via bench_hit_vs_redis.py
#                  + optional full aura regret via bench_dynamic_evict.py
#
# Never collapses ops/s and regret into one number.
#
# Outputs:
#   docs/redis-compare.md   (citeable one-pager)
#   docs/perf-log.md        (throughput append/refresh section)
#   /tmp/aura-redis-bench-*/  machine-readable JSON fragments
#
# Env knobs:
#   BENCH_QUICK=1     smaller N / skip expanded / skip adaptive agent
#   BENCH_SKIP_HIT=1  throughput only
#   BENCH_SKIP_THR=1  hit quality only
#   BENCH_FULL_REGRET=1  also run aura-only phase_marathon regret pack
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export AURA_REDIS_DENY_PLUGIN=1

REDIS_PORT="${REDIS_PORT:-26379}"
AURA_PORT="${AURA_PORT:-26380}"
N="${MEMTIER_N:-20000}"
IMG_REDIS="${REDIS_IMAGE:-redis:7-alpine}"
IMG_MEM="${MEMTIER_IMAGE:-redislabs/memtier_benchmark}"
QUICK="${BENCH_QUICK:-0}"
SKIP_HIT="${BENCH_SKIP_HIT:-0}"
SKIP_THR="${BENCH_SKIP_THR:-0}"
FULL_REGRET="${BENCH_FULL_REGRET:-0}"

if [[ "$QUICK" == "1" ]]; then
  N=5000
fi

OUT_DIR="${ROOT}/docs"
LOG_DIR="${TMPDIR:-/tmp}/aura-redis-bench-$$"
mkdir -p "$OUT_DIR" "$LOG_DIR"
JSON_OUT="$LOG_DIR/bench.json"
MD_OUT="$OUT_DIR/redis-compare.md"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DATE_CST="$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S CST')"

REDIS_CID=""
AURA_PID=""
cleanup() {
  [[ -n "${AURA_PID:-}" ]] && kill "$AURA_PID" 2>/dev/null || true
  [[ -n "${AURA_PID:-}" ]] && wait "$AURA_PID" 2>/dev/null || true
  [[ -n "${REDIS_CID:-}" ]] && sudo docker rm -f "$REDIS_CID" 2>/dev/null || true
  # hit harness cleans its own containers; sweep leftovers
  sudo docker ps -aq --filter "name=ar-hit-redis-" 2>/dev/null | xargs -r sudo docker rm -f >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== bench-vs-redis: build native =="
"$ROOT/scripts/build-native.sh" >/dev/null

extract_totals() {
  awk '/^[[:space:]]*Totals/{print $2; exit}' "$1"
}

run_memtier() {
  local name=$1 port=$2 pipeline=$3 clients=${4:-1} threads=${5:-1} nreq=${6:-$N} tag=${7:-}
  local out
  if [[ -n "$tag" ]]; then
    out="$LOG_DIR/${name}-p${pipeline}-c${clients}t${threads}-${tag}.txt"
  else
    out="$LOG_DIR/${name}-p${pipeline}-c${clients}t${threads}.txt"
  fi
  echo "-- memtier $name p=$pipeline c=${clients} t=${threads} n=$nreq --"
  sudo docker run --rm --network host "$IMG_MEM" \
    -s 127.0.0.1 -p "$port" \
    -c "$clients" -t "$threads" --ratio=1:10 -d 32 \
    --key-pattern=R:R --key-minimum=1 --key-maximum=10000 \
    -n "$nreq" --pipeline="$pipeline" \
    2>&1 | tee "$out" >/dev/null
  extract_totals "$out"
}

start_redis_thr() {
  REDIS_CID=$(sudo docker run -d --network host --name "ar-redis-thr-$$" \
    "$IMG_REDIS" redis-server --port "$REDIS_PORT" --save "" --appendonly no \
    --bind 127.0.0.1)
  sleep 0.4
}

start_aura_thr() {
  local evict=$1
  "$ROOT/native/build/aura_redis_server" --port "$AURA_PORT" --evict "$evict" \
    >"$LOG_DIR/aura-${evict}.log" 2>&1 &
  AURA_PID=$!
  for _ in $(seq 1 40); do
    if grep -q "listening on" "$LOG_DIR/aura-${evict}.log" 2>/dev/null; then break; fi
    sleep 0.15
  done
}

stop_aura_thr() {
  if [[ -n "${AURA_PID:-}" ]]; then
    kill "$AURA_PID" 2>/dev/null || true
    wait "$AURA_PID" 2>/dev/null || true
    AURA_PID=""
  fi
  fuser -k "${AURA_PORT}/tcp" >/dev/null 2>&1 || true
}

# ── Scoreboard 1: Throughput ──────────────────────────────────────────
declare -A THR=()
if [[ "$SKIP_THR" != "1" ]]; then
  echo "== Scoreboard 1: Throughput (memtier) =="
  start_redis_thr
  THR[redis_p1]=$(run_memtier redis "$REDIS_PORT" 1)
  THR[redis_p16]=$(run_memtier redis "$REDIS_PORT" 16)
  if [[ "$QUICK" != "1" ]]; then
    THR[redis_p8]=$(run_memtier redis "$REDIS_PORT" 8 1 1 "$N" p8)
    THR[redis_n100k_p1]=$(run_memtier redis "$REDIS_PORT" 1 1 1 100000 n100k)
    THR[redis_n100k_p16]=$(run_memtier redis "$REDIS_PORT" 16 1 1 100000 n100k)
    THR[redis_4c4t_p1]=$(run_memtier redis "$REDIS_PORT" 1 4 4 "$N" c4t4)
  fi

  for EV in lru lfu slru; do
    stop_aura_thr
    start_aura_thr "$EV"
    THR[aura_${EV}_p1]=$(run_memtier "aura-${EV}" "$AURA_PORT" 1)
    THR[aura_${EV}_p16]=$(run_memtier "aura-${EV}" "$AURA_PORT" 16)
    if [[ "$QUICK" != "1" ]]; then
      THR[aura_${EV}_p8]=$(run_memtier "aura-${EV}" "$AURA_PORT" 8 1 1 "$N" p8)
      THR[aura_${EV}_n100k_p1]=$(run_memtier "aura-${EV}" "$AURA_PORT" 1 1 1 100000 n100k)
      THR[aura_${EV}_n100k_p16]=$(run_memtier "aura-${EV}" "$AURA_PORT" 16 1 1 100000 n100k)
      THR[aura_${EV}_4c4t_p1]=$(run_memtier "aura-${EV}" "$AURA_PORT" 1 4 4 "$N" c4t4)
    fi
  done
  stop_aura_thr
  sudo docker rm -f "$REDIS_CID" >/dev/null 2>&1 || true
  REDIS_CID=""
fi

# ── Scoreboard 2: Hit quality ─────────────────────────────────────────
HIT_JSON="$LOG_DIR/hit.json"
HIT_MD="$LOG_DIR/hit.md"
if [[ "$SKIP_HIT" != "1" ]]; then
  echo "== Scoreboard 2: Hit quality =="
  HIT_ARGS=(
    python3 scripts/bench_hit_vs_redis.py
    --aura-port 26480 --redis-port 26479
    --maxmemory 120000
    --json-out "$HIT_JSON" --md-out "$HIT_MD"
  )
  if [[ "$QUICK" == "1" ]]; then
    HIT_ARGS+=(--workloads phase_marathon,zipf,hot_protect --aura-policies lru,lfu,adaptive --skip-adaptive)
    # still want adaptive for citeable marathon — re-enable adaptive only
    HIT_ARGS=(
      python3 scripts/bench_hit_vs_redis.py
      --workloads phase_marathon,zipf,hot_protect
      --aura-policies lru,lfu,adaptive
      --redis-policies lru,lfu
      --aura-port 26480 --redis-port 26479
      --maxmemory 120000
      --json-out "$HIT_JSON" --md-out "$HIT_MD"
    )
  fi
  "${HIT_ARGS[@]}" | tee "$LOG_DIR/hit-run.log"
fi

# Optional full aura regret (aura-only adaptive story)
REGRET_LOG="$LOG_DIR/regret.txt"
if [[ "$FULL_REGRET" == "1" ]]; then
  echo "== Aura-only regret packs (bench_dynamic_evict) =="
  python3 scripts/bench_regret.py phase_marathon 2>&1 | tee "$REGRET_LOG"
fi

# ── Compose report ────────────────────────────────────────────────────
python3 - "$MD_OUT" "$SHA" "$DATE_CST" "$LOG_DIR" "$QUICK" <<'PY'
import json, os, sys, datetime
from pathlib import Path

md_out, sha, date_cst, log_dir, quick = sys.argv[1:6]
log = Path(log_dir)

def ratio(a, r):
    try:
        a, r = float(a), float(r)
        return a / r if r else 0.0
    except Exception:
        return 0.0

def read_tot(name):
    # find first matching totals file prefix
    cands = sorted(log.glob(f"{name}*.txt"))
    for f in cands:
        for line in f.read_text(errors="replace").splitlines():
            if line.strip().startswith("Totals") or (line[:1].isspace() and "Totals" in line):
                parts = line.split()
                # Totals <ops/sec> ...
                for i, p in enumerate(parts):
                    if p == "Totals" and i + 1 < len(parts):
                        try:
                            return float(parts[i + 1])
                        except ValueError:
                            pass
                try:
                    return float(parts[1])
                except Exception:
                    pass
    return None

# Prefer exact frozen matrix files
def tot(stem):
    f = log / f"{stem}.txt"
    if f.exists():
        for line in f.read_text(errors="replace").splitlines():
            if "Totals" in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == "Totals" and i + 1 < len(parts):
                        try:
                            return float(parts[i + 1])
                        except ValueError:
                            pass
                try:
                    return float(parts[1])
                except Exception:
                    pass
    return None

lines = []
lines.append("# aura-redis vs Redis — comprehensive comparison")
lines.append("")
lines.append(f"**Date:** {date_cst} (Asia/Shanghai)  ")
lines.append(f"**Tip:** `{sha}`  ")
lines.append(f"**Host:** Linux x86_64 (this box)  ")
lines.append(f"**Redis baseline:** `redis:7-alpine` (Docker `--network host`)  ")
lines.append(f"**Aura data plane:** `native/build/aura_redis_server` (DENY_PLUGIN=1)  ")
lines.append(f"**Quick mode:** {quick == '1'}")
lines.append("")
lines.append("Two independent scoreboards — **do not collapse**:")
lines.append("")
lines.append("1. **Throughput (ops/s)** — memtier, no maxmemory pressure")
lines.append("2. **Hit quality (useful-GET hit%)** — small maxmemory; Redis = fixed `allkeys-lru` / `allkeys-lfu` only; Aura adaptive is Aura-only")
lines.append("")
lines.append("---")
lines.append("")
lines.append("## Scoreboard 1 — Throughput (ops/s)")
lines.append("")
lines.append("Frozen matrix: **1c×1t**, SET:GET=**1:10**, 32B, key 1..10000 R:R.")
lines.append("")
lines.append("| Engine | pipeline | Totals ops/s | vs Redis |")
lines.append("|--------|----------|--------------|----------|")

rows = []
r1 = tot("redis-p1-c1t1")
r16 = tot("redis-p16-c1t1")
for eng, label in [
    ("redis", "redis:7-alpine"),
    ("aura-lru", "aura EVICT=lru"),
    ("aura-lfu", "aura EVICT=lfu"),
    ("aura-slru", "aura EVICT=slru"),
]:
    for p, rv in [(1, r1), (16, r16)]:
        v = tot(f"{eng}-p{p}-c1t1")
        if v is None:
            continue
        vs = "1.00" if eng == "redis" else f"{ratio(v, rv):.3f}"
        lines.append(f"| {label} | {p} | {v:.2f} | {vs} |")
        rows.append((eng, p, v, vs))

lines.append("")
# Expanded
exp = []
for stem, label in [
    ("redis-p1-c1t1", None),  # placeholder
]:
    pass
n100_r1 = tot("redis-p1-c1t1")  # will also look for n=100k files
# n=100k files named redis-p1-c1t1 with different n — our run_memtier uses same name; last write wins.
# For expanded we used same filename pattern — re-read from env by scanning all.
# Simpler: print expanded if files with n100k were intended; we used same stem.
# Re-run naming: look for any *n* — skip if quick.

def tot_tag(eng, p, clients=1, threads=1, tag=""):
    if tag:
        return tot(f"{eng}-p{p}-c{clients}t{threads}-{tag}")
    return tot(f"{eng}-p{p}-c{clients}t{threads}")

lines.append("### Expanded matrix (when not BENCH_QUICK)")
lines.append("")
lines.append("| Variant | redis ops/s | aura-lru | aura-lfu | aura-slru | lru÷redis |")
lines.append("|---------|-------------|----------|----------|-----------|-----------|")
for label, kwargs in [
    ("n=100000 p=1", dict(p=1, clients=1, threads=1, tag="n100k")),
    ("n=100000 p=16", dict(p=16, clients=1, threads=1, tag="n100k")),
    ("n=default p=8", dict(p=8, clients=1, threads=1, tag="p8")),
    ("n=default p=1 4c×4t", dict(p=1, clients=4, threads=4, tag="c4t4")),
]:
    rr = tot_tag("redis", **kwargs)
    alru = tot_tag("aura-lru", **kwargs)
    alfu = tot_tag("aura-lfu", **kwargs)
    aslru = tot_tag("aura-slru", **kwargs)
    if rr is None and alru is None:
        continue
    def fmt(v):
        return f"{v:.2f}" if v is not None else "—"
    vs = f"{ratio(alru, rr):.3f}" if (rr and alru) else "—"
    lines.append(
        f"| {label} | {fmt(rr)} | {fmt(alru)} | {fmt(alfu)} | {fmt(aslru)} | {vs} |"
    )
lines.append("")
if r1 and tot("aura-lru-p1-c1t1"):
    lines.append(
        f"**Headline p=1:** aura-lru / redis = **{ratio(tot('aura-lru-p1-c1t1'), r1):.3f}×**; "
        f"p=16 = **{ratio(tot('aura-lru-p16-c1t1'), r16):.3f}×**."
    )
lines.append("")
lines.append("---")
lines.append("")
lines.append("## Scoreboard 2 — Hit quality (useful-GET)")
lines.append("")
hit_md = log / "hit.md"
hit_json = log / "hit.json"
if hit_md.exists():
    lines.append(hit_md.read_text())
else:
    lines.append("_Hit harness skipped (BENCH_SKIP_HIT=1)._")
lines.append("")

# Marathon headline extract
if hit_json.exists():
    data = json.loads(hit_json.read_text())
    by = {}
    for r in data.get("results", []):
        if r.get("workload") == "phase_marathon":
            by[(r["engine"], r["policy"])] = r
    def pct(eng, pol):
        r = by.get((eng, pol))
        return r["hit_pct"] if r else None
    lines.append("### Marathon headline (phase_marathon)")
    lines.append("")
    lines.append("| Engine | Policy | Hit% |")
    lines.append("|--------|--------|------|")
    for eng, pol in [
        ("aura", "adaptive"), ("aura", "lru"), ("aura", "lfu"), ("aura", "slru"),
        ("redis", "lru"), ("redis", "lfu"),
    ]:
        v = pct(eng, pol)
        if v is not None:
            lines.append(f"| {eng} | {pol} | **{v:.1f}** |")
    lines.append("")
    aa, rl, rf = pct("aura", "adaptive"), pct("redis", "lru"), pct("redis", "lfu")
    if aa is not None and rl is not None:
        lines.append(
            f"**Cite:** aura adaptive **{aa:.1f}%** vs redis allkeys-lru **{rl:.1f}%**"
            + (f" / allkeys-lfu **{rf:.1f}%**" if rf is not None else "")
            + "."
        )
        lines.append("")

lines.append("---")
lines.append("")
lines.append("## How to re-run")
lines.append("")
lines.append("```bash")
lines.append("export AURA_REDIS_DENY_PLUGIN=1")
lines.append("./scripts/build-native.sh")
lines.append("./scripts/bench-vs-redis.sh              # full dual scoreboard → docs/redis-compare.md")
lines.append("BENCH_QUICK=1 ./scripts/bench-vs-redis.sh")
lines.append("BENCH_FULL_REGRET=1 ./scripts/bench-vs-redis.sh  # + aura phase_marathon regret")
lines.append("python3 scripts/bench_hit_vs_redis.py    # hit-quality only")
lines.append("./scripts/memtier-cmp.sh                 # frozen throughput only")
lines.append("./scripts/ci-bench.sh                    # aura regret packs")
lines.append("```")
lines.append("")
lines.append("CI gates: `./scripts/ci-prod.sh` (includes `ci-strong.sh`). Long benches stay out of the wall-time gate.")
lines.append("")

Path(md_out).write_text("\n".join(lines) + "\n")
print(f"Wrote {md_out}")

# Refresh perf-log throughput section (prepend)
plog = Path(md_out).parent / "perf-log.md"
r1v = tot("redis-p1-c1t1")
a1 = tot("aura-lru-p1-c1t1")
r16v = tot("redis-p16-c1t1")
a16 = tot("aura-lru-p16-c1t1")
block = []
block.append(f"# aura-redis perf log")
block.append("")
block.append(f"## {date_cst} — tip `{sha}` (bench-vs-redis)")
block.append("")
block.append("Frozen memtier: **1c×1t**, SET:GET=**1:10**, 32B, key 1..10000 **R:R**.")
block.append("")
block.append("| Engine | pipeline | Totals ops/s | vs Redis |")
block.append("|--------|----------|--------------|----------|")
if r1v and a1:
    block.append(f"| redis:7-alpine | 1 | {r1v:.2f} | 1.00 |")
    block.append(f"| aura-redis C EVICT=lru | 1 | {a1:.2f} | **{ratio(a1,r1v):.3f}** |")
    for ev in ("lfu", "slru"):
        v = tot(f"aura-{ev}-p1-c1t1")
        if v:
            block.append(f"| aura-redis C EVICT={ev} | 1 | {v:.2f} | **{ratio(v,r1v):.3f}** |")
if r16v and a16:
    block.append(f"| redis:7-alpine | 16 | {r16v:.2f} | 1.00 |")
    block.append(f"| aura-redis C EVICT=lru | 16 | {a16:.2f} | **{ratio(a16,r16v):.3f}** |")
    for ev in ("lfu", "slru"):
        v = tot(f"aura-{ev}-p16-c1t1")
        if v:
            block.append(f"| aura-redis C EVICT={ev} | 16 | {v:.2f} | **{ratio(v,r16v):.3f}** |")
block.append("")
block.append(f"Full dual scoreboard: [`redis-compare.md`](redis-compare.md).")
block.append("")
block.append("---")
block.append("")
prev = plog.read_text() if plog.exists() else ""
# Drop old leading title if present to avoid duplicate H1
if prev.startswith("# aura-redis perf log"):
    prev = "\n".join(prev.splitlines()[1:]).lstrip("\n")
plog.write_text("\n".join(block) + "\n" + prev)
print(f"Updated {plog}")
PY

echo "=== bench-vs-redis: DONE → $MD_OUT ==="
