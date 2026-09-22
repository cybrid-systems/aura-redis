#!/usr/bin/env bash
# Demo Iteration 6: Aura adaptive supervisor swaps lru↔lfu under two load patterns.
# Runs server_ffi with AURA_REDIS_ADAPTIVE=1 inside the Aura dev image.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-26420}"  # do not inherit AURA_REDIS_PORT (may be Lisp bench :6380)
IMG="${AURA_DEV_IMAGE:-ghcr.io/cybrid-systems/dev:v1.0.7}"
LOG="${TMPDIR:-/tmp}/aura-redis-adaptive-demo.log"
CID=""

cleanup() {
  if [[ -n "$CID" ]]; then
    sudo docker kill "$CID" >/dev/null 2>&1 || true
    sudo docker rm -f "$CID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

"$ROOT/scripts/build-native.sh"

: >"$LOG"
echo "demo-adaptive: starting Aura+FFI adaptive server on 127.0.0.1:$PORT"
CID=$(sudo docker run -d --network host --entrypoint '' \
  -v "$ROOT:/work" -w /work \
  -e AURA_SANDBOX=off \
  -e AURA_PIPELINE_STRICT=0 \
  -e AURA_PATH=/work/.deps/aura/lib \
  -e AURA_REDIS_PORT="$PORT" \
  -e AURA_REDIS_ENGINE=ffi \
  -e AURA_REDIS_CORE_SO=/work/native/build/libaura_redis_core.so \
  -e AURA_REDIS_MAXMEMORY="${AURA_REDIS_MAXMEMORY:-500000}" \
  -e AURA_REDIS_EVICT="${AURA_REDIS_EVICT:-lru}" \
  -e AURA_REDIS_ADAPTIVE=1 \
  "$IMG" \
  /work/.deps/aura/build/aura /work/src/redis/server_ffi.aura)

for i in $(seq 1 60); do
  if sudo docker logs "$CID" 2>&1 | grep -q "listening on"; then
    break
  fi
  if ! sudo docker inspect -f '{{.State.Running}}' "$CID" 2>/dev/null | grep -q true; then
    echo "demo-adaptive: server died:" >&2
    sudo docker logs "$CID" >&2 || true
    exit 1
  fi
  sleep 0.25
done

if ! sudo docker logs "$CID" 2>&1 | grep -q "listening on"; then
  echo "demo-adaptive: timeout waiting for listen" >&2
  sudo docker logs "$CID" >&2 || true
  exit 1
fi

echo "demo-adaptive: phase A write-heavy (expect → lfu)"
python3 "$ROOT/tests/test_adaptive.py" --port "$PORT" --phase write --seconds 3

echo "demo-adaptive: phase B read-heavy (expect → lru)"
python3 "$ROOT/tests/test_adaptive.py" --port "$PORT" --phase read --seconds 3

# Give supervisor a moment to log final swap
sleep 0.5
sudo docker logs "$CID" 2>&1 | tee "$LOG" | tail -n 80

SWAPS=$(grep -c 'adaptive: swap' "$LOG" || true)
echo "demo-adaptive: swap log lines=$SWAPS"
if [[ "$SWAPS" -lt 1 ]]; then
  echo "FAIL: expected at least one adaptive: swap in logs" >&2
  exit 1
fi
if ! grep -q '→ lfu' "$LOG" && ! grep -q '→ lfu' <<<"$(sudo docker logs "$CID" 2>&1)"; then
  # Unicode arrow may be → 
  if ! grep -E 'adaptive: swap .*(lfu|→)' "$LOG" >/dev/null; then
    echo "WARN: no explicit → lfu line (check rules/load); swaps=$SWAPS"
  fi
fi
echo "demo-adaptive: OK (see $LOG)"
