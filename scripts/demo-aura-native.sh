#!/usr/bin/env bash
# Demo: Aura redis adapts by mutating Aura policy code (hot-strategy),
# C only executes chosen kernels via RESP EVICT — not PLUGIN/.so.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-26520}"
IMG="${AURA_DEV_IMAGE:-ghcr.io/cybrid-systems/dev:v1.0.7}"
LOG_AGENT="${TMPDIR:-/tmp}/aura-redis-policy-agent.log"
LOG_SRV="${TMPDIR:-/tmp}/aura-redis-native-srv.log"
SRV_PID=""
AGENT_CID=""

cleanup() {
  if [[ -n "$AGENT_CID" ]]; then
    sudo docker kill "$AGENT_CID" >/dev/null 2>&1 || true
    sudo docker rm -f "$AGENT_CID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$SRV_PID" ]]; then
    kill "$SRV_PID" >/dev/null 2>&1 || true
    wait "$SRV_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

"$ROOT/scripts/build-native.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/sandbox-policy-profile.sh"

: >"$LOG_AGENT"
: >"$LOG_SRV"

echo "demo-aura-native: C data plane on 127.0.0.1:$PORT (DENY_PLUGIN=1)"
AURA_REDIS_DENY_PLUGIN=1 AURA_REDIS_MAXMEMORY="${AURA_REDIS_MAXMEMORY:-500000}" \
  AURA_REDIS_EVICT=noop \
  "$ROOT/native/build/aura_redis_server" --port "$PORT" --evict noop --maxmemory "${AURA_REDIS_MAXMEMORY:-500000}" \
  >"$LOG_SRV" 2>&1 &
SRV_PID=$!

for i in $(seq 1 40); do
  if grep -q "listening on" "$LOG_SRV" 2>/dev/null; then break; fi
  if ! kill -0 "$SRV_PID" 2>/dev/null; then
    echo "demo-aura-native: server died:" >&2
    cat "$LOG_SRV" >&2
    exit 1
  fi
  sleep 0.1
done
grep -q "listening on" "$LOG_SRV"

echo "demo-aura-native: Aura policy agent (hot-strategy + RESP EVICT)"
AGENT_CID=$(sudo docker run -d --network host --entrypoint '' \
  -v "$ROOT:/work" -w /work \
  -e AURA_SANDBOX=off \
  -e AURA_PIPELINE_STRICT=0 \
  -e AURA_PATH=/work/.deps/aura/lib \
  -e AURA_REDIS_PORT="$PORT" \
  -e AURA_REDIS_HOST=127.0.0.1 \
  -e AURA_REDIS_POLICY_DEMO=1 \
  -e AURA_REDIS_POLICY_MS=150 \
  -e AURA_REDIS_DENY_PLUGIN=1 \
  "$IMG" \
  /work/.deps/aura/build/aura /work/src/redis/policy_agent.aura)

# Wait for agent connect
for i in $(seq 1 60); do
  sudo docker logs "$AGENT_CID" >"$LOG_AGENT" 2>&1 || true
  if grep -q "PING" "$LOG_AGENT"; then break; fi
  if ! sudo docker inspect -f '{{.State.Running}}' "$AGENT_CID" 2>/dev/null | grep -q true; then
    # may have finished very fast — still check log
    break
  fi
  sleep 0.25
done
sudo docker logs "$AGENT_CID" >"$LOG_AGENT" 2>&1 || true
if ! grep -q "PING" "$LOG_AGENT"; then
  echo "demo-aura-native: agent failed to connect:" >&2
  cat "$LOG_AGENT" >&2
  exit 1
fi

echo "demo-aura-native: phase A write-heavy under NORMAL policy (expect → lfu)"
python3 "$ROOT/tests/test_adaptive.py" --port "$PORT" --phase write --seconds 2.5

# Wait past invert swap (~3s from agent start); keep write load
sleep 1
echo "demo-aura-native: phase B write-heavy under INVERTED policy (expect → lru)"
python3 "$ROOT/tests/test_adaptive.py" --port "$PORT" --phase write --seconds 2.5

# Wait for heal + demo exit
for i in $(seq 1 40); do
  sudo docker logs "$AGENT_CID" >"$LOG_AGENT" 2>&1 || true
  if grep -q "demo done" "$LOG_AGENT"; then break; fi
  if ! sudo docker inspect -f '{{.State.Running}}' "$AGENT_CID" 2>/dev/null | grep -q true; then
    break
  fi
  sleep 0.25
done
sudo docker logs "$AGENT_CID" >"$LOG_AGENT" 2>&1 || true

echo "── agent log (tail) ──"
tail -n 60 "$LOG_AGENT"

# Assertions
fail=0
if ! grep -q 'hot-strategy:swap! → inverted' "$LOG_AGENT"; then
  echo "FAIL: missing inverted swap" >&2; fail=1
fi
if ! grep -qE 'heal' "$LOG_AGENT"; then
  echo "FAIL: missing heal path" >&2; fail=1
fi
if ! grep -q 'EVICT' "$LOG_AGENT"; then
  echo "FAIL: no EVICT applies (load/policy?)" >&2; fail=1
fi
# At least one apply toward lfu under normal, and preferably lru under invert
if ! grep -qE 'EVICT .*→ lfu' "$LOG_AGENT"; then
  echo "WARN: no → lfu line (check write load timing)" >&2
fi
if ! grep -qE 'EVICT .*→ lru' "$LOG_AGENT"; then
  echo "WARN: no → lru line after invert (check timing)" >&2
fi

# Final EVICT query
EVICT_NOW=$(PYTHONPATH="$ROOT/tests" python3 -c "
import socket
from smoke_client import redis_call
s=socket.create_connection(('127.0.0.1', $PORT), 2)
print(redis_call(s, 'EVICT'))
s.close()
")
echo "demo-aura-native: final EVICT=$EVICT_NOW"

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "demo-aura-native: OK (Aura mutated policy under sandbox discipline; C ran kernels)"
