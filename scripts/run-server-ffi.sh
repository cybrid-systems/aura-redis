#!/usr/bin/env bash
# Run FFI engine inside ghcr.io/cybrid-systems/dev (host often lacks GLIBCXX_3.4.35).
# Uses --network host so memtier/redis containers can reach 127.0.0.1.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-${AURA_REDIS_PORT:-6379}}"
SO="${AURA_REDIS_CORE_SO:-$ROOT/native/build/libaura_redis_core.so}"
IMG="${AURA_DEV_IMAGE:-ghcr.io/cybrid-systems/dev:v1.0.7}"

if [[ ! -f "$SO" ]]; then
  echo "run-server-ffi: building native..." >&2
  "$ROOT/scripts/build-native.sh"
fi

export AURA_REDIS_PORT="$PORT"
echo "run-server-ffi: starting on 127.0.0.1:$PORT via $IMG (adaptive=${AURA_REDIS_ADAPTIVE:-0})" >&2
exec sudo docker run --rm --network host --entrypoint '' \
  -v "$ROOT:/work" -w /work \
  -e AURA_SANDBOX=off \
  -e AURA_PIPELINE_STRICT=0 \
  -e AURA_PATH=/work/.deps/aura/lib \
  -e AURA_REDIS_PORT="$PORT" \
  -e AURA_REDIS_ENGINE=ffi \
  -e AURA_REDIS_CORE_SO=/work/native/build/libaura_redis_core.so \
  -e AURA_REDIS_MAXMEMORY="${AURA_REDIS_MAXMEMORY:-}" \
  -e AURA_REDIS_EVICT="${AURA_REDIS_EVICT:-noop}" \
  -e AURA_REDIS_ADAPTIVE="${AURA_REDIS_ADAPTIVE:-0}" \
  "$IMG" \
  /work/.deps/aura/build/aura /work/src/redis/server_ffi.aura
