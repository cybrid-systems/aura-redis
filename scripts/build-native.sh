#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/native/build"
mkdir -p "$BUILD"
cmake -S "$ROOT/native" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD" -j"$(nproc 2>/dev/null || echo 4)"
echo "built: $BUILD/libaura_redis_core.so"
ls -la "$BUILD"/libaura_redis_core.so
