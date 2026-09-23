#!/usr/bin/env bash
# Differentiation scoreboard vs Redis: E1 marathon, E2 poison (A19),
# E3 shadow→canary (A18), E4 provenance explain (A17), optional E5 memtier hygiene.
#
# Dual scoreboard discipline:
#   - Hit quality / regret / explainability for adaptive story
#   - memtier throughput ONLY for C dataplane (lru) parity — never adaptive wins
#
# Usage:
#   ./scripts/bench-diff-vs-redis.sh
#   BENCH_DIFF_SKIP_MEMTIER=1 ./scripts/bench-diff-vs-redis.sh
#   BENCH_DIFF_SKIP_MARATHON=1 ./scripts/bench-diff-vs-redis.sh   # reuse cached E1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export AURA_REDIS_DENY_PLUGIN=1

OUT="${DIFF_OUT:-/tmp/aura-diff-vs-redis-$$}"
mkdir -p "$OUT"
SHA="$(git rev-parse --short HEAD)"
SHA_FULL="$(git rev-parse HEAD)"
DATE_CST="$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S CST')"
SKIP_MEM="${BENCH_DIFF_SKIP_MEMTIER:-0}"
SKIP_MAR="${BENCH_DIFF_SKIP_MARATHON:-0}"
SKIP_HIT="${BENCH_DIFF_SKIP_HIT_REDIS:-0}"

echo "== bench-diff-vs-redis: tip=$SHA $DATE_CST out=$OUT =="
./scripts/build-native.sh >/dev/null

# ── E1: authoritative Aura phase_marathon ──────────────────────────────────
if [[ "$SKIP_MAR" != "1" ]]; then
  echo "== E1 phase_marathon (bench_regret.py) =="
  python3 scripts/bench_regret.py phase_marathon 2>&1 | tee "$OUT/e1_phase_marathon.txt"
else
  echo "== E1 skipped (BENCH_DIFF_SKIP_MARATHON=1) =="
fi

# Side-by-side Redis fixed kernels (short harness; adaptive rows non-citeable)
if [[ "$SKIP_HIT" != "1" ]]; then
  echo "== E1b bench_hit_vs_redis (fixed kernels + redis; skip adaptive cite) =="
  AURA_REDIS_REDIS_HEADROOM="${AURA_REDIS_REDIS_HEADROOM:-150000}" \
    python3 scripts/bench_hit_vs_redis.py \
      --workloads phase_marathon,zipf,hot_protect,ws_shift \
      --aura-policies lru,lfu,slru \
      --redis-policies lru,lfu \
      --skip-adaptive \
      --json-out "$OUT/e1_hit_vs_redis.json" \
      --md-out "$OUT/e1_hit_vs_redis.md" \
      2>&1 | tee "$OUT/e1_hit_vs_redis.txt"
fi

# ── E2: A19 poison vs Redis ────────────────────────────────────────────────
echo "== E2 poison_vs_redis (A19) =="
# Tight Redis headroom so unique-SET flood triggers eviction (citeable keep*)
python3 scripts/bench_poison_vs_redis.py \
  --headroom "${POISON_REDIS_HEADROOM:-45000}" \
  --maxmemory "${POISON_AURA_MAXMEMORY:-120000}" \
  --json-out "$OUT/e2_poison.json" \
  --md-out "$OUT/e2_poison.md" \
  2>&1 | tee "$OUT/e2_poison.txt"

# ── E3 + E4: A18 canary + A17 explain artifacts ────────────────────────────
echo "== E3/E4 explain + shadow→canary capture =="
DIFF_OUT="$OUT" python3 scripts/bench_explain_canary_capture.py 2>&1 | tee "$OUT/e34_capture.txt"

# ── E5: optional short memtier hygiene (dataplane parity only) ─────────────
if [[ "$SKIP_MEM" != "1" ]]; then
  echo "== E5 memtier hygiene (aura-lru vs redis p=1/p=16) =="
  REDIS_PORT=26379 AURA_PORT=26380 MEMTIER_N="${MEMTIER_N:-20000}" \
    ./scripts/memtier-cmp.sh 2>&1 | tee "$OUT/e5_memtier.txt" || {
      echo "WARN: memtier-cmp failed; continuing with hit-quality docs" >&2
    }
fi

# Persist metadata for doc generator
cat > "$OUT/meta.json" << EOF
{
  "sha": "$SHA",
  "sha_full": "$SHA_FULL",
  "date_cst": "$DATE_CST",
  "out": "$OUT"
}
EOF

echo "== generating docs/diff-vs-redis.md =="
python3 scripts/_gen_diff_vs_redis_doc.py --in "$OUT" --out docs/diff-vs-redis.md

echo "== bench-diff-vs-redis DONE → docs/diff-vs-redis.md =="
echo "Artifacts: $OUT"
