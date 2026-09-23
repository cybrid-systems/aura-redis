#!/usr/bin/env bash
# Long regret / hit-quality benches (NOT part of ci-prod wall-time gate).
# Prefer scripts/bench-vs-redis.sh for citeable Redis dual-scoreboard.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export AURA_REDIS_DENY_PLUGIN=1
./scripts/build-native.sh

echo "=== ci-bench: regret headline packs ==="
python3 scripts/bench_regret.py phase_marathon
python3 scripts/bench_regret.py zipf
python3 scripts/bench_regret.py hot_protect
python3 scripts/bench_regret.py poison_heal

# Gated mutation packs (longer; optional via AURA_REDIS_CI_BENCH_FULL=1)
if [[ "${AURA_REDIS_CI_BENCH_FULL:-0}" == "1" ]]; then
  echo "=== ci-bench: FULL mutation/prefix packs ==="
  python3 scripts/bench_regret.py mutation_gain
  python3 scripts/bench_regret.py evolve_gain
  python3 scripts/bench_regret.py prefix_mix_v2
  python3 scripts/bench_regret.py ttl_wave
  python3 scripts/bench_regret.py oscillate
  python3 scripts/bench_regret.py ws_shift
fi

echo "=== ci-bench: DONE ==="
