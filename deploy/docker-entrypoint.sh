#!/usr/bin/env bash
# Seed /data/aura-redis.conf from the staging example when missing.
set -euo pipefail
CFG="${AURA_REDIS_CONFIG:-/data/aura-redis.conf}"
EXAMPLE="/etc/aura-redis/aura-redis.conf.example"
if [[ -n "$CFG" && ! -f "$CFG" && -f "$EXAMPLE" ]]; then
  mkdir -p "$(dirname "$CFG")" 2>/dev/null || true
  cp "$EXAMPLE" "$CFG" || true
  if [[ -n "${AURA_REDIS_REQUIREPASS:-}" ]]; then
    if grep -q '^requirepass ' "$CFG" 2>/dev/null; then
      sed -i "s|^requirepass .*|requirepass ${AURA_REDIS_REQUIREPASS}|" "$CFG" || true
    else
      printf 'requirepass %s\n' "${AURA_REDIS_REQUIREPASS}" >>"$CFG"
    fi
  fi
fi
export AURA_REDIS_DENY_PLUGIN="${AURA_REDIS_DENY_PLUGIN:-1}"
exec "$@"
