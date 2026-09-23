#!/usr/bin/env bash
# Cheap staging-packaging smoke (no Docker compose required for CI):
#   build native → start aura_redis_server → scripts/healthcheck.sh → stop
# Compose remains a human staging path (see docs/runbook.md §11).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${AURA_REDIS_STAGING_SMOKE_PORT:-26940}"
PASS="${AURA_REDIS_REQUIREPASS:-}"
LOG="${TMPDIR:-/tmp}/aura-redis-smoke-staging-${PORT}.log"
CFG="${TMPDIR:-/tmp}/aura-redis-smoke-staging-${PORT}.conf"
DIR="${TMPDIR:-/tmp}/aura-redis-smoke-staging-data-${PORT}"
PID=""

cleanup() {
  if [[ -n "${PID}" ]] && kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -f "$CFG" "$LOG" 2>/dev/null || true
  rm -rf "$DIR" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== smoke-staging: build native ==="
if [[ "${AURA_REDIS_SKIP_BUILD:-0}" != "1" ]]; then
  ./scripts/build-native.sh
fi
BIN="$ROOT/native/build/aura_redis_server"
test -x "$BIN"

mkdir -p "$DIR"
cat >"$CFG" <<CFGEOF
bind 127.0.0.1
protected-mode yes
maxmemory 1048576
dir $DIR
dbfilename dump.rdb
CFGEOF
if [[ -n "$PASS" ]]; then
  printf 'requirepass %s\n' "$PASS" >>"$CFG"
fi

echo "=== smoke-staging: start server :$PORT ==="
export AURA_REDIS_DENY_PLUGIN=1
: >"$LOG"
"$BIN" --port "$PORT" --bind 127.0.0.1 --evict lru \
  --config "$CFG" --dir "$DIR" --dbfilename dump.rdb \
  ${PASS:+--requirepass "$PASS"} \
  >"$LOG" 2>&1 &
PID=$!

for _ in $(seq 1 80); do
  if grep -q "listening on" "$LOG" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "smoke-staging: server died:" >&2
    cat "$LOG" >&2
    exit 1
  fi
  sleep 0.05
done
grep -q "listening on" "$LOG"

echo "=== smoke-staging: healthcheck ==="
export AURA_REDIS_HOST=127.0.0.1
export AURA_REDIS_PORT="$PORT"
if [[ -n "$PASS" ]]; then
  export AURA_REDIS_REQUIREPASS="$PASS"
fi
./scripts/healthcheck.sh

# Negative: wrong password should fail when pass set
if [[ -n "$PASS" ]]; then
  if AURA_REDIS_REQUIREPASS=wrong-password-xyz ./scripts/healthcheck.sh 2>/dev/null; then
    echo "smoke-staging: expected AUTH fail with wrong password" >&2
    exit 1
  fi
  echo "smoke-staging: wrong-password rejected (OK)"
fi

echo "=== smoke-staging: OK ==="
echo "Compose (human staging): docker compose up -d --build"
echo "  optional agent: docker compose --profile agent up -d"
