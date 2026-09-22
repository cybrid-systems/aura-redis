#!/usr/bin/env bash
# Clone cybrid-systems/aura at the SHA pinned in AURA_REF into .deps/aura
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REF="$(tr -d '[:space:]' < "$ROOT/AURA_REF")"
DEST="$ROOT/.deps/aura"
URL="${AURA_GIT_URL:-https://github.com/cybrid-systems/aura.git}"

if [[ -z "$REF" ]]; then
  echo "fetch-aura: empty AURA_REF" >&2
  exit 1
fi

mkdir -p "$ROOT/.deps"

fetch_sha() {
  local dest="$1"
  git -C "$dest" fetch --depth 1 origin "$REF"
  git -C "$dest" checkout --force FETCH_HEAD
  # Ensure detached HEAD matches REF when REF is a full SHA
  local got
  got="$(git -C "$dest" rev-parse HEAD)"
  if [[ "$REF" == "$got" || "$got" == "$REF"* || "$REF" == "$got"* ]]; then
    return 0
  fi
  # Fallback: deepen / full fetch of that object
  git -C "$dest" fetch --depth 1 origin "$REF"
  git -C "$dest" checkout --force "$REF"
}

if [[ -d "$DEST/.git" ]]; then
  echo "fetch-aura: updating existing checkout → $REF"
  fetch_sha "$DEST"
else
  echo "fetch-aura: cloning $URL @ $REF"
  rm -rf "$DEST"
  git init "$DEST"
  git -C "$DEST" remote add origin "$URL"
  fetch_sha "$DEST"
fi

echo "fetch-aura: OK $(git -C "$DEST" rev-parse HEAD)"
