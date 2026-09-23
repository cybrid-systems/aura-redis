#!/usr/bin/env bash
# Production gate: native C server tests + Aura FFI/TCP typed suites +
# strong-narrative / P1–P2 via scripts/ci-strong.sh.
# Skip long CI wait preference: soak defaults to 30s via AURA_REDIS_SOAK_SEC.
# Long regret benches: scripts/ci-bench.sh or scripts/bench-vs-redis.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== ci-prod: build native ==="
./scripts/build-native.sh

run() {
  local name="$1"; shift
  echo "=== ci-prod: $name ==="
  python3 "$@"
}

echo "=== ci-prod: Aura FFI typed ==="
./scripts/run-aura-ffi-tests.sh

echo "=== ci-prod: Aura TCP typed ==="
./scripts/run-aura-tcp-tests.sh

run P0.1 tests/test_prod_protocol.py
run P0.2 tests/test_prod_memory.py
run P0.3 tests/test_prod_ttl.py
run P0.4 tests/test_prod_auth.py
run P0.5 tests/test_prod_shutdown.py
run P0.6 tests/test_prod_info.py
export AURA_REDIS_SOAK_SEC="${AURA_REDIS_SOAK_SEC:-30}"
run P0.7 tests/test_prod_soak.py

run P3.16a tests/test_prod_hash.py
run P3.16b tests/test_prod_list.py
run P3.16c tests/test_prod_zset.py
run P3.16-edge tests/test_prod_types_edge.py
run P3.17a tests/test_prod_multi.py
run P3.17b tests/test_prod_pubsub.py
run P3.18-scan tests/test_prod_scan.py
run T2.9-set-opts tests/test_prod_set_opts.py
run T2.10-string-keys tests/test_prod_string_keys.py
run T2.11-string-meta tests/test_prod_string_meta.py
run T2.edges tests/test_prod_tier2_edges.py
run T2.12-watch tests/test_prod_watch.py

# P1/P2 + strong-narrative (config/clients/rdb/replica/tls/policy_ha + A3–A13)
AURA_REDIS_SKIP_BUILD=1 ./scripts/ci-strong.sh

echo "=== ci-prod: ALL PASSED ==="
