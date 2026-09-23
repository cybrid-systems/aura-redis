#!/usr/bin/env bash
# Sandbox profile for Aura-native policy agent (A11 / SN5).
#
# Aura default (AURA_SANDBOX unset) = Restricted. The policy agent needs:
#   - TCP client → effect:network (bits=16)
#   - mutate / hot-strategy → effect:mutate (bits=8)
#   - MUST NOT need effect:ffi — PLUGIN/.so is an escape hatch only
#
# Usage:
#   source scripts/sandbox-policy-profile.sh              # default: off (dev Soft)
#   AURA_REDIS_SANDBOX_PROFILE=restricted source ...      # prod-shaped attempt
#   AURA_REDIS_SANDBOX_PROFILE=off source ...             # explicit Soft
#
# Profiles:
#   off (default today)
#     export AURA_SANDBOX=off
#     Soft ergonomics: grant-effect! network/mutate succeed without Tenant Admin.
#     Still DENY_PLUGIN=1 — adaptation remains Aura choose-fn, not .so.
#
#   restricted (prod-shaped target — PARTIAL on current Aura pin)
#     unset AURA_SANDBOX  → Restricted
#     Agent should call (security:grant-effect! "network" 16) and
#     (security:grant-effect! "mutate" 8) after bootstrap.
#     BLOCKER (2026-09-23 pin): grant-effect! returns #f without Tenant Admin
#     (TA) / explicit tenant principal. policy_agent TCP then fails to bind
#     std/socket. Until TA is available in this workspace, demos/CI keep
#     AURA_SANDBOX=off and document the Restricted path here + smoke script.
#
# Smoke: ./scripts/smoke-sandbox-profile.sh
# Docs:  docs/aura-native-control.md · docs/strong-narrative-plan.md (A11)

PROFILE="${AURA_REDIS_SANDBOX_PROFILE:-off}"

export AURA_PIPELINE_STRICT="${AURA_PIPELINE_STRICT:-0}"
export AURA_REDIS_DENY_PLUGIN="${AURA_REDIS_DENY_PLUGIN:-1}"
export AURA_REDIS_EVICT_SO=""   # do not auto-load plugins

case "$PROFILE" in
  restricted|Restricted|RESTRICTED)
    unset AURA_SANDBOX || true
    export AURA_REDIS_TRY_RESTRICTED_GRANTS=1
    export AURA_REDIS_SANDBOX_PROFILE=restricted
    echo "sandbox-policy-profile: PROFILE=restricted (AURA_SANDBOX unset)"
    echo "sandbox-policy-profile: PARTIAL — grant-effect needs Tenant Admin on this Aura pin;"
    echo "sandbox-policy-profile:          expect grant #f without TA; demos may still need off."
    echo "sandbox-policy-profile: DENY_PLUGIN=$AURA_REDIS_DENY_PLUGIN (no ffi/PLUGIN)"
    ;;
  off|Off|OFF|soft|Soft)
    export AURA_SANDBOX=off
    export AURA_REDIS_SANDBOX_PROFILE=off
    unset AURA_REDIS_TRY_RESTRICTED_GRANTS || true
    echo "sandbox-policy-profile: PROFILE=off AURA_SANDBOX=$AURA_SANDBOX DENY_PLUGIN=$AURA_REDIS_DENY_PLUGIN (no ffi/PLUGIN)"
    ;;
  *)
    echo "sandbox-policy-profile: unknown AURA_REDIS_SANDBOX_PROFILE=$PROFILE (use off|restricted)" >&2
    export AURA_SANDBOX="${AURA_SANDBOX:-off}"
    export AURA_REDIS_SANDBOX_PROFILE=off
    echo "sandbox-policy-profile: fallback AURA_SANDBOX=$AURA_SANDBOX DENY_PLUGIN=$AURA_REDIS_DENY_PLUGIN"
    ;;
esac
