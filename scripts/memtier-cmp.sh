#!/usr/bin/env bash
# Compare aura-redis FFI (C data plane) vs redis:7-alpine with memtier_benchmark.
# Frozen matrix: 1c/1t, SET:GET=1:10, 32B, key 1..10000 R:R, pipeline 1 and 16.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REDIS_PORT="${REDIS_PORT:-26379}"
AURA_PORT="${AURA_PORT:-26380}"
N="${MEMTIER_N:-20000}"
IMG_REDIS="${REDIS_IMAGE:-redis:7-alpine}"
IMG_MEM="${MEMTIER_IMAGE:-redislabs/memtier_benchmark}"
IMG_DEV="${AURA_DEV_IMAGE:-ghcr.io/cybrid-systems/dev:v1.0.7}"
USE_STANDALONE="${AURA_REDIS_STANDALONE:-1}"  # 1=host C binary (fast); 0=Aura FFI in docker
OUT_DIR="${ROOT}/docs"
mkdir -p "$OUT_DIR"
LOG_DIR="${TMPDIR:-/tmp}/aura-redis-memtier-$$"
mkdir -p "$LOG_DIR"

REDIS_CID=""
AURA_PID=""
AURA_CID=""

cleanup() {
  [[ -n "$AURA_PID" ]] && kill "$AURA_PID" 2>/dev/null || true
  [[ -n "$AURA_PID" ]] && wait "$AURA_PID" 2>/dev/null || true
  [[ -n "$AURA_CID" ]] && sudo docker rm -f "$AURA_CID" 2>/dev/null || true
  [[ -n "$REDIS_CID" ]] && sudo docker rm -f "$REDIS_CID" 2>/dev/null || true
}
trap cleanup EXIT

"$ROOT/scripts/build-native.sh" >/dev/null

echo "== memtier-cmp: start redis on :$REDIS_PORT =="
REDIS_CID=$(sudo docker run -d --network host --name "ar-redis-$$" \
  "$IMG_REDIS" redis-server --port "$REDIS_PORT" --save "" --appendonly no \
  --bind 127.0.0.1)
sleep 0.5

echo "== memtier-cmp: start aura-redis on :$AURA_PORT (standalone=$USE_STANDALONE) =="
if [[ "$USE_STANDALONE" == "1" ]]; then
  "$ROOT/native/build/aura_redis_server" --port "$AURA_PORT" \
    >"$LOG_DIR/aura.log" 2>&1 &
  AURA_PID=$!
else
  AURA_CID=$(sudo docker run -d --network host --name "ar-aura-$$" \
    -v "$ROOT:/work" -w /work \
    -e AURA_SANDBOX=off -e AURA_PIPELINE_STRICT=0 \
    -e AURA_PATH=/work/.deps/aura/lib \
    -e AURA_REDIS_PORT="$AURA_PORT" \
    -e AURA_REDIS_CORE_SO=/work/native/build/libaura_redis_core.so \
    "$IMG_DEV" \
    /work/.deps/aura/build/aura /work/src/redis/server_ffi.aura)
fi

# wait for listen
for i in $(seq 1 40); do
  if grep -q "listening on" "$LOG_DIR/aura.log" 2>/dev/null; then break; fi
  if [[ -n "$AURA_CID" ]] && sudo docker logs "$AURA_CID" 2>&1 | grep -q "listening on"; then break; fi
  sleep 0.25
done

run_memtier() {
  local name=$1 port=$2 pipeline=$3
  local out="$LOG_DIR/${name}-p${pipeline}.txt"
  echo "-- memtier $name pipeline=$pipeline --"
  sudo docker run --rm --network host "$IMG_MEM" \
    -s 127.0.0.1 -p "$port" \
    -c 1 -t 1 --ratio=1:10 -d 32 \
    --key-pattern=R:R --key-minimum=1 --key-maximum=10000 \
    -n "$N" --pipeline="$pipeline" \
    2>&1 | tee "$out"
}

run_memtier redis "$REDIS_PORT" 1
run_memtier aura "$AURA_PORT" 1
run_memtier redis "$REDIS_PORT" 16
run_memtier aura "$AURA_PORT" 16

extract_totals() {
  # memtier prints: "Totals ..." line with Ops/sec as 2nd field often
  # Example: Totals     38123.45 ...
  local f=$1
  awk '/^[[:space:]]*Totals/{print $2; exit}' "$f"
}

R1=$(extract_totals "$LOG_DIR/redis-p1.txt")
A1=$(extract_totals "$LOG_DIR/aura-p1.txt")
R16=$(extract_totals "$LOG_DIR/redis-p16.txt")
A16=$(extract_totals "$LOG_DIR/aura-p16.txt")

python3 - "$R1" "$A1" "$R16" "$A16" <<'PY' | tee "$OUT_DIR/perf-log.md"
import sys, datetime
r1,a1,r16,a16 = map(float, sys.argv[1:5])
def ratio(a,r):
    return (a/r) if r else 0.0
print("# aura-redis perf log")
print()
print(f"Recorded: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')} (Asia/Shanghai)")
print()
print("Frozen memtier: 1c×1t, SET:GET=1:10, 32B, key 1..10000 R:R")
print()
print("| Engine | pipeline | Totals ops/s | vs Redis |")
print("|--------|----------|--------------|----------|")
print(f"| redis:7-alpine | 1 | {r1:.2f} | 1.00 |")
print(f"| aura-redis FFI (C) | 1 | {a1:.2f} | {ratio(a1,r1):.3f} |")
print(f"| redis:7-alpine | 16 | {r16:.2f} | 1.00 |")
print(f"| aura-redis FFI (C) | 16 | {a16:.2f} | {ratio(a16,r16):.3f} |")
print()
print(f"**Iteration gate:** p=1 ratio = **{ratio(a1,r1):.3f}** (target ≥0.20 iter2, ≥0.50 iter3, ≥0.80 iter4)")
print()
print("Lisp engine baseline (prior): p=1 ~143 ops/s — FFI should be ≫10×.")
PY

echo "Wrote $OUT_DIR/perf-log.md"
