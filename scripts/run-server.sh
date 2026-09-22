#!/usr/bin/env bash
# Run aura-redis server (loopback 127.0.0.1)
# AURA_REDIS_ENGINE=ffi|aura  (default: ffi if .so exists, else aura)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AURA_BIN="${AURA_BIN:-$ROOT/.deps/aura/build/aura}"
PORT="${1:-${AURA_REDIS_PORT:-6379}}"
SO="${AURA_REDIS_CORE_SO:-$ROOT/native/build/libaura_redis_core.so}"

ENGINE="${AURA_REDIS_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
  if [[ -f "$SO" ]]; then ENGINE=ffi; else ENGINE=aura; fi
fi

if [[ ! -x "$AURA_BIN" ]]; then
  echo "run-server: aura binary not found at $AURA_BIN" >&2
  echo "  Run: ./scripts/fetch-aura.sh && ./scripts/build-aura.sh" >&2
  echo "  Or point AURA_BIN at an existing aura binary." >&2
  echo "  Host may lack GLIBCXX — use: ./scripts/run-server-ffi.sh" >&2
  exit 1
fi

export AURA_SANDBOX=off
export AURA_PIPELINE_STRICT="${AURA_PIPELINE_STRICT:-0}"
export AURA_PATH="${AURA_PATH:-$ROOT/.deps/aura/lib}"
export AURA_REDIS_PORT="$PORT"
export AURA_REDIS_ENGINE="$ENGINE"
export AURA_REDIS_CORE_SO="$SO"

cd "$ROOT"
if [[ "$ENGINE" == "ffi" ]]; then
  if [[ ! -f "$SO" ]]; then
    echo "run-server: missing $SO — run ./scripts/build-native.sh" >&2
    exit 1
  fi
  exec "$AURA_BIN" src/redis/server_ffi.aura
else
  exec "$AURA_BIN" src/redis/server.aura
fi
