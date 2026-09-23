#!/usr/bin/env bash
# Strong-narrative + production P1/P2 gate (unit/integration).
# Long regret benches live in scripts/ci-bench.sh / bench-vs-redis.sh.
# Env:
#   AURA_REDIS_STRONG_TIMEOUT_SEC  per-test wall (default 180; agent suites need headroom)
#   AURA_REDIS_STRONG_SKIP_AGENT=1 skip agent suites (audit/canary/autofreeze/…)
#   Agent suites use tests/_agentutil.py (native in GHA container; docker locally).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TIMEOUT_SEC="${AURA_REDIS_STRONG_TIMEOUT_SEC:-180}"
SKIP_AGENT="${AURA_REDIS_STRONG_SKIP_AGENT:-0}"

run() {
  local name="$1"; shift
  echo "=== ci-strong: $name ==="
  if command -v timeout >/dev/null 2>&1; then
    timeout --foreground "${TIMEOUT_SEC}s" python3 "$@"
  else
    python3 "$@"
  fi
}

if [[ "${AURA_REDIS_SKIP_BUILD:-0}" != "1" ]]; then
  echo "=== ci-strong: build native ==="
  ./scripts/build-native.sh
fi

# ── P1 / P2 prod surfaces (missing from historic ci-prod P0+P3) ──
run P1.config tests/test_prod_config.py
run P1.clients tests/test_prod_clients.py
run P1.slowlog tests/test_prod_slowlog.py
run P2.rdb tests/test_prod_rdb.py
run P2.replica tests/test_prod_replica.py
run P2.tls tests/test_prod_tls.py
run P2.policy_ha tests/test_prod_policy_ha.py

# ── C-only strong-narrative (no Docker agent) ──
run A12.evict_slru tests/test_evict_slru.py
run A9.hot_cold_knobs tests/test_hot_cold_knobs.py
run A10.shadow_c_edges tests/test_strong_edges.py

if [[ "$SKIP_AGENT" == "1" ]]; then
  echo "=== ci-strong: SKIP agent suites (AURA_REDIS_STRONG_SKIP_AGENT=1) ==="
  echo "=== ci-strong: ALL PASSED (agent skipped) ==="
  exit 0
fi

# ── Agent strong-narrative ──
run A3.policy_audit tests/test_policy_audit.py
run A4.policy_canary tests/test_policy_canary.py
run A8.policy_autofreeze tests/test_policy_autofreeze.py
run A13.weight_evolve tests/test_policy_weight_evolve.py
run A10.shadow_ab tests/test_shadow_ab.py
run A18.shadow_autopromote tests/test_shadow_autopromote.py
run A19.poison_keys tests/test_poison_keys.py
run A7.typed_pressure tests/test_typed_pressure.py

echo "=== ci-strong: ALL PASSED ==="
