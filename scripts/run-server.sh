#!/usr/bin/env bash
# Run aura-redis server (loopback 127.0.0.1)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AURA_BIN="${AURA_BIN:-$ROOT/.deps/aura/build/aura}"
PORT="${1:-6379}"

if [[ ! -x "$AURA_BIN" ]]; then
  echo "run-server: aura binary not found at $AURA_BIN" >&2
  echo "  Run: ./scripts/fetch-aura.sh && ./scripts/build-aura.sh" >&2
  echo "  Or point AURA_BIN at an existing aura binary." >&2
  exit 1
fi

export AURA_SANDBOX=off
export AURA_PIPELINE_STRICT="${AURA_PIPELINE_STRICT:-0}"
export AURA_PATH="${AURA_PATH:-$ROOT/.deps/aura/lib}"
export AURA_REDIS_PORT="$PORT"

cd "$ROOT"
exec "$AURA_BIN" src/redis/server.aura
