#!/usr/bin/env bash
# Start aura-redis on an ephemeral port, run Python RESP smoke client, tear down.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AURA_BIN="${AURA_BIN:-$ROOT/.deps/aura/build/aura}"
PORT="${AURA_REDIS_PORT:-16379}"
ENGINE="${AURA_REDIS_ENGINE:-aura}"
LOG="${TMPDIR:-/tmp}/aura-redis-smoke-$$.log"
PID=""
USE_DOCKER_AURA=0

cleanup() {
  if [[ -n "${PID}" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -f "$LOG"
}
trap cleanup EXIT

export AURA_SANDBOX=off
export AURA_PIPELINE_STRICT="${AURA_PIPELINE_STRICT:-0}"
export AURA_PATH="${AURA_PATH:-$ROOT/.deps/aura/lib}"
export AURA_REDIS_PORT="$PORT"
export AURA_REDIS_ENGINE="$ENGINE"
export AURA_REDIS_CORE_SO="${AURA_REDIS_CORE_SO:-$ROOT/native/build/libaura_redis_core.so}"

cd "$ROOT"

if [[ "$ENGINE" == "ffi" ]]; then
  "$ROOT/scripts/build-native.sh"
  # Prefer host standalone if aura lacks GLIBCXX; still covers C data plane.
  if [[ -x "$ROOT/native/build/aura_redis_server" ]] && ! "$AURA_BIN" -e '(display 1)' >/dev/null 2>&1; then
    echo "smoke-test: starting standalone C server on 127.0.0.1:$PORT (host aura GLIBCXX missing)"
    "$ROOT/native/build/aura_redis_server" --port "$PORT" >"$LOG" 2>&1 &
    PID=$!
  elif [[ -x "$AURA_BIN" ]]; then
    echo "smoke-test: starting FFI server (Aura) on 127.0.0.1:$PORT"
    "$AURA_BIN" src/redis/server_ffi.aura >"$LOG" 2>&1 &
    PID=$!
  else
    echo "smoke-test: starting standalone C server on 127.0.0.1:$PORT"
    "$ROOT/native/build/aura_redis_server" --port "$PORT" >"$LOG" 2>&1 &
    PID=$!
  fi
  SMOKE_ENGINE=ffi
else
  if [[ ! -x "$AURA_BIN" ]]; then
    echo "smoke-test: missing aura binary at $AURA_BIN" >&2
    exit 1
  fi
  echo "smoke-test: starting Lisp server on 127.0.0.1:$PORT"
  "$AURA_BIN" src/redis/server.aura >"$LOG" 2>&1 &
  PID=$!
  SMOKE_ENGINE=aura
fi

for _ in $(seq 1 120); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "smoke-test: server exited early:" >&2
    cat "$LOG" >&2 || true
    exit 1
  fi
  if grep -q "listening on" "$LOG" 2>/dev/null; then
    break
  fi
  sleep 0.25
done

if ! grep -q "listening on" "$LOG" 2>/dev/null; then
  echo "smoke-test: timeout waiting for listen banner:" >&2
  cat "$LOG" >&2 || true
  exit 1
fi

python3 "$ROOT/tests/smoke_client.py" --host 127.0.0.1 --port "$PORT" --engine "$SMOKE_ENGINE"
echo "smoke-test: OK (engine=$SMOKE_ENGINE)"
