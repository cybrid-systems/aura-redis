#!/usr/bin/env bash
# A11 smoke: assert sandbox profile behavior (off vs Restricted PARTIAL).
# Exit 0 when both faces match expectations on this Aura pin.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG="${AURA_DEV_IMAGE:-ghcr.io/cybrid-systems/dev:v1.0.7}"
AURA_BIN="${AURA_BIN:-/work/.deps/aura/build/aura}"
PROBE=/work/scripts/_sandbox_grant_probe.aura

run_probe() {
  local label="$1"
  shift
  sudo docker run --rm --entrypoint '' \
    -v "$ROOT:/work" -w /work \
    -e AURA_PIPELINE_STRICT=0 \
    -e AURA_PATH=/work/.deps/aura/lib \
    "$@" \
    "$IMG" \
    "$AURA_BIN" "$PROBE" 2>&1 \
    | tee "/tmp/aura-sandbox-probe-${label}.log" \
    | grep -E '^PROBE_' || true
}

echo "=== A11 smoke: PROFILE=off (expect grant network+mutate true) ==="
# shellcheck source=/dev/null
AURA_REDIS_SANDBOX_PROFILE=off source "$ROOT/scripts/sandbox-policy-profile.sh"
OFF_OUT=$(run_probe off -e AURA_SANDBOX=off)
echo "$OFF_OUT"
echo "$OFF_OUT" | grep -q 'PROBE_ENV_SANDBOX=off'
echo "$OFF_OUT" | grep -q 'PROBE_GRANT_NETWORK=true'
echo "$OFF_OUT" | grep -q 'PROBE_GRANT_MUTATE=true'
echo "PASS off profile: grants succeed under AURA_SANDBOX=off"

echo "=== A11 smoke: PROFILE=restricted (expect grant false = TA blocker) ==="
# shellcheck source=/dev/null
AURA_REDIS_SANDBOX_PROFILE=restricted source "$ROOT/scripts/sandbox-policy-profile.sh"
REST_OUT=$(run_probe restricted)
echo "$REST_OUT"
echo "$REST_OUT" | grep -q 'PROBE_ENV_SANDBOX=UNSET'
echo "$REST_OUT" | grep -q 'PROBE_GRANT_NETWORK=false'
echo "$REST_OUT" | grep -q 'PROBE_GRANT_MUTATE=false'
echo "PASS restricted profile: grant-effect refused without Tenant Admin (PARTIAL blocker documented)"

echo "=== A11 smoke summary ==="
echo "achieved_mode_for_policy_agent: off (Soft) — DENY_PLUGIN=1, no ffi"
echo "restricted_status: PARTIAL — needs Tenant Admin for grant-effect!; keep off for live TCP agent"
echo "PASS smoke-sandbox-profile"
