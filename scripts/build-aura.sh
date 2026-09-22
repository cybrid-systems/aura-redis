#!/usr/bin/env bash
# Build Aura inside .deps/aura (expects fetch-aura.sh already run)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AURA="$ROOT/.deps/aura"

if [[ ! -x "$AURA/build.py" ]]; then
  echo "build-aura: missing $AURA/build.py — run scripts/fetch-aura.sh first" >&2
  exit 1
fi

export AURA_BUILD_TYPE="${AURA_BUILD_TYPE:-RelWithDebInfo}"
export AURA_USE_MOLD="${AURA_USE_MOLD:-1}"
export AURA_LINK_JOBS="${AURA_LINK_JOBS:-2}"
export AURA_BUILD_JOBS="${AURA_BUILD_JOBS:-0}"
export AURA_CI="${AURA_CI:-1}"
export CCACHE_DISABLE="${CCACHE_DISABLE:-1}"
export AURA_PIPELINE_STRICT="${AURA_PIPELINE_STRICT:-0}"

cd "$AURA"
echo "build-aura: ./build.py build (AURA_BUILD_TYPE=$AURA_BUILD_TYPE)"
./build.py build

if [[ ! -x "$AURA/build/aura" ]]; then
  echo "build-aura: expected binary $AURA/build/aura missing after build" >&2
  exit 1
fi
echo "build-aura: OK $AURA/build/aura"
