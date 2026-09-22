#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/scripts/build-native.sh"
export AURA_SANDBOX=off AURA_PIPELINE_STRICT=0
export AURA_PATH="${AURA_PATH:-$ROOT/.deps/aura/lib}"
export AURA_REDIS_CORE_SO="$ROOT/native/build/libaura_redis_core.so"
cd "$ROOT"
sudo docker run --rm -v "$ROOT:/work" -w /work \
  -e AURA_SANDBOX=off -e AURA_PIPELINE_STRICT=0 \
  -e AURA_PATH=/work/.deps/aura/lib \
  -e AURA_REDIS_CORE_SO=/work/native/build/libaura_redis_core.so \
  ghcr.io/cybrid-systems/dev:v1.0.7 \
  /work/.deps/aura/build/aura tests/test_ffi_iter1.aura
