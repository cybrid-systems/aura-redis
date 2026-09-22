#!/usr/bin/env bash
# Inner CI steps: fetch Aura → build Aura → smoke-test aura-redis.
# Intended to run inside ghcr.io/cybrid-systems/dev:v1.0.7
# (see README / .github/workflows/ci.yml).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== ci-in-container: fetch Aura ==="
./scripts/fetch-aura.sh

echo "=== ci-in-container: build Aura (must succeed) ==="
./scripts/build-aura.sh

echo "=== ci-in-container: smoke-test aura-redis ==="
./scripts/smoke-test.sh

echo "=== ci-in-container: OK ==="
