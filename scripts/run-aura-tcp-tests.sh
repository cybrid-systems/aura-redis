#!/usr/bin/env bash
# Build native, start aura_redis_server, run tests/test_prod_types.aura, tear down.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
./scripts/build-native.sh

# Do not inherit ambient AURA_REDIS_PORT (may be a live server / 6380).
PORT="${AURA_REDIS_TCP_TEST_PORT:-26990}"
export AURA_REDIS_PORT="$PORT"
BIN="$ROOT/native/build/aura_redis_server"
LOG="${TMPDIR:-/tmp}/aura-tcp-types-${PORT}.log"
PID=""

cleanup() {
  if [[ -n "${PID}" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
sleep 0.05
export AURA_REDIS_DENY_PLUGIN=1
"$BIN" --port "$PORT" --evict lru --maxmemory 0 >"$LOG" 2>&1 &
PID=$!
for _ in $(seq 1 50); do
  if grep -q "listening on" "$LOG" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "server died:" >&2
    cat "$LOG" >&2
    exit 1
  fi
  sleep 0.05
done

export AURA_SANDBOX=off AURA_PIPELINE_STRICT=0
export AURA_PATH="${AURA_PATH:-$ROOT/.deps/aura/lib}"
AURA_BIN="${AURA_BIN:-$ROOT/.deps/aura/build/aura}"
OUT=""
if [[ -x "$AURA_BIN" ]] && "$AURA_BIN" -e '(display 1)' >/dev/null 2>&1; then
  OUT="$("$AURA_BIN" tests/test_prod_types.aura 2>&1)" || true
else
  echo "run-aura-tcp-tests: host aura unavailable; using docker (host network)"
  OUT="$(sudo docker run --rm --network host -v "$ROOT:/work" -w /work \
    -e AURA_SANDBOX=off -e AURA_PIPELINE_STRICT=0 \
    -e AURA_PATH=/work/.deps/aura/lib \
    -e AURA_REDIS_PORT="$PORT" \
    ghcr.io/cybrid-systems/dev:v1.0.7 \
    /work/.deps/aura/build/aura tests/test_prod_types.aura 2>&1)" || true
fi
printf '%s\n' "$OUT"
if echo "$OUT" | grep -q 'test_prod_types: OK'; then
  exit 0
fi
echo "run-aura-tcp-tests: FAILED" >&2
exit 1
