#!/usr/bin/env bash
# Install the Aura nightly pinned by baseline/aura-baseline.json.
#
# The nightly tag is rebuilt in place: the archive under a fixed asset
# name is replaced, so the digest the baseline pins can name a build the
# release no longer publishes. The download is therefore verified against
# the digest the release publishes beside the archive, so neither an
# unverified binary is installed nor a stale pin blocks the install. When
# the published digest differs from the pinned one the pin is reported as
# stale; moving the pin is the pin workflow's job.
#
# The only stdout line is the directory that contains bin/aura.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
baseline="$root/baseline/aura-baseline.json"
[[ -f "$baseline" ]] || { echo "missing $baseline" >&2; exit 1; }

dest="$root/.deps/aura"
if [[ -x "$dest/build/aura" && -x "$dest/bin/aura" ]]; then
  printf '%s\n' "$dest"
  exit 0
fi

arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) arch="x86_64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) echo "unsupported architecture: $arch" >&2; exit 1 ;;
esac

key="linux-${arch}"
name="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["aura"]["assets"][sys.argv[2]]["name"])' "$baseline" "$key")"
pinned="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["aura"]["assets"][sys.argv[2]]["sha256"])' "$baseline" "$key")"
commit="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["aura"]["commit_sha"])' "$baseline")"
AURA_VERSION="${AURA_VERSION:-nightly}"
base="${AURA_DOWNLOAD_BASE:-https://github.com/cybrid-systems/aura/releases/download/${AURA_VERSION}}"

# The digest the release publishes beside the archive. Empty when the
# release cannot be reached.
published() {
  local tmp
  tmp="$(mktemp)" || return 0
  if curl -fsSL --retry 3 --retry-delay 1 -o "$tmp" "${base}/${name}.sha256" 2>/dev/null; then
    awk 'NF { print $1; exit }' "$tmp"
  fi
  rm -f "$tmp"
}

want="$(published)"
if [[ -z "$want" ]]; then
  # Offline: the pinned build is the only one that can still be installed.
  want="$pinned"
elif [[ "$want" != "$pinned" ]]; then
  echo "warning: the baseline pins ${pinned:0:12} (commit ${commit:0:9})" >&2
  echo "warning: the release now publishes ${want:0:12}: the pinned build is no longer published" >&2
  echo "warning: the pin must be moved to keep the baseline reproducible" >&2
  # A caller that gates on the pinned build wants the mismatch to fail,
  # not to install the newer build silently.
  if [[ -n "${AURA_REQUIRE_PINNED:-}" ]]; then
    echo "AURA_REQUIRE_PINNED: the release no longer publishes the pinned build" >&2
    exit 1
  fi
fi

if [[ ! -x "$dest/build/aura" ]]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  echo "install-aura: nightly ${commit:0:9} (${arch}) -> .deps/aura" >&2
  if ! curl -fsSL --retry 3 --retry-delay 1 -o "$tmp/$name" "$base/$name"; then
    echo "Aura download failed" >&2
    exit 1
  fi
  printf '%s  %s\n' "$want" "$tmp/$name" | sha256sum -c - >&2
  rm -rf "$dest"
  mkdir -p "$dest"
  tar -xzf "$tmp/$name" -C "$dest" --strip-components=1
  mkdir -p "$dest/build"
  ln -sfn ../bin/aura "$dest/build/aura"
fi

[[ -x "$dest/build/aura" ]] || { echo "install-aura: no build/aura" >&2; exit 1; }
printf '%s\n' "$dest"
"$dest/bin/aura" --version >&2
