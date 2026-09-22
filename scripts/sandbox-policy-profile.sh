#!/usr/bin/env bash
# Sandbox profile for Aura-native policy agent.
#
# Default Aura is Restricted (AURA_SANDBOX unset). The policy agent needs:
#   - TCP client → effect:network (or AURA_SANDBOX=off for demos)
#   - mutate / hot-strategy → in-workspace (no effect:ffi)
# It must NOT need effect:ffi — PLUGIN/.so is an escape hatch only.
#
# This script exports a disciplined env for demos/CI:
#   - AURA_SANDBOX=off          (dev Soft; grants network+mutate without TA)
#   - AURA_REDIS_DENY_PLUGIN=1  (C data plane refuses PLUGIN load)
#
# Production-shaped Restricted + explicit grants (when host supports TA):
#   unset AURA_SANDBOX   # Restricted
#   (security:grant-effect! "network" …)  ; inside Aura after bootstrap
#   refuse effect:ffi unless an operator intentionally enables PLUGIN
#
# Usage: source scripts/sandbox-policy-profile.sh
#        then run policy_agent / demo-aura-native.sh

export AURA_SANDBOX="${AURA_SANDBOX:-off}"
export AURA_PIPELINE_STRICT="${AURA_PIPELINE_STRICT:-0}"
export AURA_REDIS_DENY_PLUGIN="${AURA_REDIS_DENY_PLUGIN:-1}"
export AURA_REDIS_EVICT_SO=""   # do not auto-load plugins

echo "sandbox-policy-profile: AURA_SANDBOX=$AURA_SANDBOX DENY_PLUGIN=$AURA_REDIS_DENY_PLUGIN (no ffi/PLUGIN)"
