#!/usr/bin/env bash
# Wrapper: build C server then run dynamic eviction comparison harness.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/scripts/build-native.sh" >/dev/null
exec python3 "$ROOT/scripts/bench_dynamic_evict.py" "$@"
