#!/usr/bin/env bash
# Aura FFI typed-ops suite (no TCP). Requires .deps/aura built.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
./scripts/build-native.sh
export AURA_SANDBOX=off AURA_PIPELINE_STRICT=0
export AURA_PATH="${AURA_PATH:-$ROOT/.deps/aura/lib}"
export AURA_REDIS_CORE_SO="$ROOT/native/build/libaura_redis_core.so"
AURA_BIN="${AURA_BIN:-$ROOT/.deps/aura/build/aura}"
OUT=""
run_local() {
  OUT="$("$AURA_BIN" tests/test_types_ffi.aura 2>&1)" || true
}
run_docker() {
  OUT="$(sudo docker run --rm -v "$ROOT:/work" -w /work \
    -e AURA_SANDBOX=off -e AURA_PIPELINE_STRICT=0 \
    -e AURA_PATH=/work/.deps/aura/lib \
    -e AURA_REDIS_CORE_SO=/work/native/build/libaura_redis_core.so \
    ghcr.io/cybrid-systems/dev:v1.0.7 \
    /work/.deps/aura/build/aura tests/test_types_ffi.aura 2>&1)" || true
}
if [[ -x "$AURA_BIN" ]] && "$AURA_BIN" -e '(display 1)' >/dev/null 2>&1; then
  run_local
else
  echo "run-aura-ffi-tests: host aura unavailable; using docker"
  run_docker
fi
printf '%s\n' "$OUT"
if echo "$OUT" | grep -q 'test_types_ffi: OK'; then
  exit 0
fi
echo "run-aura-ffi-tests: FAILED" >&2
exit 1
