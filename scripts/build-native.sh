#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/native/build"
mkdir -p "$BUILD"
# Optional: AURA_REDIS_TLS=OFF to force cleartext-only build
TLS_FLAG="${AURA_REDIS_TLS:-ON}"
cmake -S "$ROOT/native" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release \
  -DAURA_REDIS_TLS="${TLS_FLAG}"
cmake --build "$BUILD" -j"$(nproc 2>/dev/null || echo 4)"
echo "built: $BUILD/libaura_redis_core.so"
ls -la "$BUILD"/libaura_redis_core.so
if grep -q "OpenSSL TLS enabled" "$BUILD/CMakeCache.txt" 2>/dev/null || \
   ldd "$BUILD/libaura_redis_core.so" 2>/dev/null | grep -q libssl; then
  echo "TLS: enabled (OpenSSL linked)"
else
  echo "TLS: disabled (rebuild with libssl-dev / AURA_REDIS_TLS=ON)"
fi
