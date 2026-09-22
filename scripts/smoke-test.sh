#!/usr/bin/env bash
# Start aura-redis on an ephemeral port, run Python RESP smoke client, tear down.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AURA_BIN="${AURA_BIN:-$ROOT/.deps/aura/build/aura}"
PORT="${AURA_REDIS_PORT:-16379}"
LOG="${TMPDIR:-/tmp}/aura-redis-smoke-$$.log"
PID=""

cleanup() {
  if [[ -n "${PID}" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -f "$LOG"
}
trap cleanup EXIT

if [[ ! -x "$AURA_BIN" ]]; then
  echo "smoke-test: missing aura binary at $AURA_BIN" >&2
  exit 1
fi

export AURA_SANDBOX=off
export AURA_PIPELINE_STRICT="${AURA_PIPELINE_STRICT:-0}"
export AURA_PATH="${AURA_PATH:-$ROOT/.deps/aura/lib}"
export AURA_REDIS_PORT="$PORT"

cd "$ROOT"
echo "smoke-test: starting server on 127.0.0.1:$PORT"
"$AURA_BIN" src/redis/server.aura >"$LOG" 2>&1 &
PID=$!

# Wait until listen line appears or process dies
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

python3 "$ROOT/tests/smoke_client.py" --host 127.0.0.1 --port "$PORT"
echo "smoke-test: OK"
