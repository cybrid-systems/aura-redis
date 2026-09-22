#!/usr/bin/env bash
# Demo: mutation-attributable gain — Aura fitness-swap/heal vs frozen choose-fn.
# Control plane = policy_agent.aura (Docker). DENY_PLUGIN=1.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "════════════════════════════════════════════════════════════"
echo " AURA-REDIS — mutation vs frozen policy (M6)"
echo "════════════════════════════════════════════════════════════"
echo "  adaptive_frozen  = Aura agent, seed=conservative, NO fitness swap"
echo "  adaptive_mutate  = Aura agent, same seed, fitness-swap/heal ON"
echo "  poison_*         = seed=inverted; mutate heals, frozen stays bad"
echo

./scripts/build-native.sh >/tmp/ar-build-native.log 2>&1 || {
  echo "FAIL: build-native"; tail -40 /tmp/ar-build-native.log; exit 1
}

echo "── 1) mutation_gain (diurnal-ish; conservative seed) ──"
python3 scripts/bench_regret.py mutation_gain --skip-assert || true
# re-run with assert for gate
python3 scripts/bench_dynamic_evict.py \
  --workloads mutation_gain \
  --policies lru,lfu,adaptive_frozen,adaptive_mutate \
  --skip-assert | tee /tmp/ar-mutation-gain.out

echo
echo "── 2) poison_heal (inverted seed; heal recovery) ──"
python3 scripts/bench_dynamic_evict.py \
  --workloads poison_heal \
  --policies poison_frozen,poison_mutate \
  --skip-assert | tee /tmp/ar-poison-heal.out

echo
echo "── fitness log proof (from last agent logs) ──"
for f in /tmp/ar-policy-agent-*.log; do
  [[ -f "$f" ]] || continue
  echo "## $f"
  grep -E 'fitness-swap|fitness-heal|hot-strategy:heal!|seed-profile|fitness-mutate=' "$f" | tail -n 30 || true
done

echo
echo "Done. See docs/mutation-gains.md"
