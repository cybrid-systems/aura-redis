#!/usr/bin/env bash
# P0.7 — production gate: P0.1–P0.7 tests (no Aura build; native C server only).
# Skip long CI wait preference: soak defaults to 30s via AURA_REDIS_SOAK_SEC.
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
run P3.17a tests/test_prod_multi.py

echo "=== ci-prod: ALL PASSED ==="
