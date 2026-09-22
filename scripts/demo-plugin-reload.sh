#!/usr/bin/env bash
# Demo: live-reload eviction plugins under traffic (Iteration 7 stretch).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
./scripts/build-native.sh
exec python3 tests/test_plugin_reload.py
